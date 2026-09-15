#!/usr/bin/env python3
"""Suite 2: reservations, renewal, cancellation, priority/preemption, iCal — incl. edge cases."""
import sys, time, datetime, urllib.request
from lib import call, login, admin_token, check, summary, load_state, BASE

admin = admin_token()
st = load_state()
uname = st.get("uname")
utok = login(uname, "pw") if uname else None
if not (admin and utok):
    print("FATAL: missing tokens (run suite 1 first)")
    sys.exit(summary("reservations"))

# unique GPU ids per run to avoid collisions with leftover data
sfx = str(int(time.time()) % 100000)
gA, gB, gP = f"gpuA{sfx}", f"gpuB{sfx}", f"gpuP{sfx}"

def ts(h):
    return (datetime.datetime(2030, 1, 1) + datetime.timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")

# basic
code, r = call("POST", "/reservations", token=utok, body={"gpu_uuid": gA, "start_time": ts(0), "end_time": ts(2)})
check("create reservation", code == 200 and r.get("id"), f"{code} {r}")
rid = r.get("id")
code, _ = call("POST", "/reservations", token=utok, body={"gpu_uuid": gA, "start_time": ts(1), "end_time": ts(3)})
check("overlapping same GPU -> 409", code == 409, f"got {code}")
code, _ = call("POST", "/reservations", token=utok, body={"gpu_uuid": gB, "start_time": ts(0), "end_time": ts(2)})
check("different GPU same time -> ok", code == 200, f"got {code}")

# renewal
code, _ = call("PUT", f"/reservations/{rid}/renew", token=utok, body={"end_time": ts(4)})
check("renew extends end", code == 200, f"got {code}")
code, _ = call("PUT", f"/reservations/{rid}/renew", token=utok, body={"end_time": ts(1)})
check("renew to earlier time -> 400", code == 400, f"got {code}")
code, _ = call("PUT", "/reservations/999999/renew", token=utok, body={"end_time": ts(9)})
check("renew nonexistent -> 404", code == 404, f"got {code}")

# ownership
u2 = "other" + sfx
call("POST", "/users", token=admin, body={"username": u2, "password": "pw", "role": "user"})
u2tok = login(u2, "pw")
code, _ = call("PUT", f"/reservations/{rid}/renew", token=u2tok, body={"end_time": ts(9)})
check("renew someone else's reservation -> 403", code == 403, f"got {code}")
code, _ = call("DELETE", f"/reservations/{rid}", token=u2tok)
check("cancel someone else's reservation -> 403", code == 403, f"got {code}")

# cancellation edge cases
code, _ = call("DELETE", f"/reservations/{rid}", token=utok)
check("owner cancels own reservation", code == 200, f"got {code}")
code, _ = call("DELETE", f"/reservations/{rid}", token=utok)
check("cancel already-cancelled -> 200 (no crash)", code == 200, f"got {code}")
code, r = call("POST", "/reservations", token=utok, body={"gpu_uuid": gA, "start_time": ts(0), "end_time": ts(2)})
check("slot freed after cancel -> rebook", code == 200, f"got {code}")
rid_re = r.get("id")
code, _ = call("DELETE", "/reservations/999999", token=utok)
check("cancel nonexistent -> 404", code == 404, f"got {code}")
code, _ = call("DELETE", f"/reservations/{rid_re}", token=admin)
check("admin cancels any reservation", code == 200, f"got {code}")

# priority / preemption
code, _ = call("POST", "/reservations", token=utok, body={"gpu_uuid": gP, "start_time": ts(0), "end_time": ts(2), "priority": 1})
check("low-priority reservation", code == 200, f"got {code}")
code, _ = call("POST", "/reservations", token=admin, body={"gpu_uuid": gP, "start_time": ts(0), "end_time": ts(2), "priority": 5})
check("high-priority preempts low -> 200", code == 200, f"got {code}")
code, _ = call("POST", "/reservations", token=utok, body={"gpu_uuid": gP, "start_time": ts(0), "end_time": ts(2), "priority": 5})
check("equal-priority conflict -> 409", code == 409, f"got {code}")

# calendar + iCal
code, r = call("GET", "/reservations/calendar")
check("calendar (public)", code == 200 and isinstance(r, list), f"{code}")
try:
    with urllib.request.urlopen(BASE + "/reservations/calendar.ics", timeout=10) as resp:
        ics = resp.read().decode()
    check("iCal feed valid", ics.startswith("BEGIN:VCALENDAR") and "END:VCALENDAR" in ics, ics[:40])
except Exception as e:
    check("iCal feed valid", False, str(e))

sys.exit(summary("reservations"))
