#!/usr/bin/env python3
"""Suite 6: ślad audytowy (GET /audit-log) — kto co zrobił, także próby nieudane.

Sprawdza, że API zapisuje działania zmieniające stan i próby logowania, że czyta je TYLKO
administrator, że hasła nie trafiają do zapisu i że filtry/stronicowanie działają.

Self-contained: zakłada własne konto testowe i usuwa je na końcu. Nie wymaga GPU."""
import sys, time
from lib import call, login, admin_token, check, summary

admin = admin_token()
check("admin login", admin is not None)
if not admin:
    print("FATAL: cannot get admin token; is the API up and bootstrapped?")
    sys.exit(summary("audit_log"))

sfx = str(int(time.time()) % 100000)
uname = f"audituser{sfx}"
UPASS = "correct-horse"
WRONG = "definitely-not-the-password"


def entries(**params):
    """GET /audit-log jako admin, z filtrami. Zwraca (kod, lista wpisów, cała odpowiedź)."""
    query = "&".join(f"{k}={v}" for k, v in params.items() if v not in (None, ""))
    code, body = call("GET", f"/audit-log?{query}" if query else "/audit-log", token=admin)
    listing = body.get("entries", []) if isinstance(body, dict) else []
    return code, listing, body if isinstance(body, dict) else {}


def find(listing, **fields):
    """Pierwszy wpis, którego wszystkie podane pola się zgadzają."""
    for entry in listing:
        if all(entry.get(key) == value for key, value in fields.items()):
            return entry
    return None


# ------------------------------------------------------------------ dostęp
code, _, meta = entries(hours=1)
check("admin GET /audit-log -> 200", code == 200, f"got {code}")
check("response reports whether recording is on",
      isinstance(meta.get("enabled"), bool), f"{meta.get('enabled')!r}")
if meta.get("enabled") is False:
    print("NOTE: AUDIT_ENABLED=false on this backend — nothing is recorded, skipping content checks")
    sys.exit(summary("audit_log"))

# ------------------------------------------------------- akcja: utworzenie konta
code, _ = call("POST", "/users", token=admin,
               body={"username": uname, "password": UPASS, "role": "user", "quota_gpu": 2})
check("create audited user", code == 200, f"{code}")

code, listing, _ = entries(hours=1, action="users.create", search=uname)
created = find(listing, action="users.create", target=uname)
check("account creation is recorded", created is not None, f"{listing[:2]}")
check("recorded action names the administrator who did it",
      (created or {}).get("actor") == "admin" and (created or {}).get("outcome") == "success",
      f"{created}")
check("recorded detail keeps what was set",
      isinstance((created or {}).get("detail"), dict)
      and (created or {}).get("detail", {}).get("role") == "user",
      f"{(created or {}).get('detail')}")

# Hasło konta NIE MOŻE dać się odczytać z audytu — ani jawnie, ani w polu detail.
code, all_listing, _ = entries(hours=1, limit=500)
check("no password value anywhere in the audit log",
      UPASS not in str(all_listing) and WRONG not in str(all_listing),
      "a password reached the audit log")

# ------------------------------------------------- próba logowania: udana i nieudana
utok = login(uname, UPASS)
check("audited user can log in", utok is not None)
check("failed login is rejected", login(uname, WRONG) is None)

code, listing, _ = entries(hours=1, action="auth.login", actor=uname)
check("both sign-in attempts are recorded", len(listing) >= 2, f"{len(listing)} entries")
ok_entry = find(listing, outcome="success")
bad_entry = find(listing, outcome="denied")
check("successful sign-in recorded as success", ok_entry is not None, f"{listing[:2]}")
check("failed sign-in recorded as not permitted",
      bad_entry is not None and bad_entry.get("status_code") == 401, f"{bad_entry}")
check("failed sign-in says the account existed and the password did not match",
      (bad_entry or {}).get("detail", {}).get("user_exists") is True
      and (bad_entry or {}).get("detail", {}).get("password_correct") is False,
      f"{(bad_entry or {}).get('detail')}")

# ------------------------------------------------------ akcja odrzucona: brak uprawnień
code, _ = call("DELETE", "/users/admin", token=utok)
check("regular user DELETE /users -> 403", code == 403, f"got {code}")
code, listing, _ = entries(hours=1, actor=uname, outcome="denied")
denied = find(listing, action="users.delete")
check("attempt without permission is recorded", denied is not None, f"{listing[:3]}")
check("denied attempt records the target it went after",
      (denied or {}).get("target") == "admin" and (denied or {}).get("status_code") == 403,
      f"{denied}")

# ----------------------------------------------------------- audyt tylko dla admina
code, body = call("GET", "/audit-log", token=utok)
check("regular user GET /audit-log -> 403", code == 403, f"got {code} {body}")

# --------------------------------------------------------------- filtry i stronicowanie
code, listing, meta = entries(hours=1, actor=uname, limit=500)
check("actor filter returns only that person's actions",
      code == 200 and listing and all(e.get("actor") == uname for e in listing),
      f"{ {e.get('actor') for e in listing} }")
check("filter lists offer the people and actions seen in the window",
      uname in meta.get("actors", []) and "auth.login" in meta.get("actions", []),
      f"actors={meta.get('actors')} actions={meta.get('actions')}")

code, page1, meta1 = entries(hours=1, actor=uname, limit=1, offset=0)
code, page2, meta2 = entries(hours=1, actor=uname, limit=1, offset=1)
check("paging returns one entry per page", len(page1) == 1 and len(page2) == 1,
      f"{len(page1)} / {len(page2)}")
check("paging walks distinct entries and reports the full count",
      page1[0]["id"] != page2[0]["id"] and meta1.get("total", 0) >= 2,
      f"{meta1.get('total')}")

# Odczyty (GET) domyślnie nie są zapisywane — inaczej odświeżanie konsoli zalałoby audyt.
if meta.get("includes_reads") is False:
    call("GET", "/users", token=admin)
    code, listing, _ = entries(hours=1, limit=500)
    check("read-only requests are not recorded by default",
          not any(e.get("action", "").endswith(".read") for e in listing),
          f"{[e.get('action') for e in listing if e.get('action','').endswith('.read')][:3]}")

# ------------------------------------------------------------------------ porządki
code, _ = call("DELETE", f"/users/{uname}", token=admin)
check("delete audited user", code == 200, f"{code}")
code, listing, _ = entries(hours=1, action="users.delete", search=uname)
check("account deletion is recorded with who did it",
      find(listing, target=uname, actor="admin", outcome="success") is not None, f"{listing[:2]}")

sys.exit(summary("audit_log"))
