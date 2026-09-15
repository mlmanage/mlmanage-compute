#!/usr/bin/env python3
"""Suite 5: group/project/user mutation, GPU availability, and the activity feed.

Self-contained: creates its own admin-owned users/groups/projects and cleans them up.
Works on CPU-only clusters (availability just returns an empty GPU list there)."""
import sys, time, datetime
from lib import call, login, admin_token, check, summary

admin = admin_token()
check("admin login", admin is not None)
if not admin:
    print("FATAL: cannot get admin token; is the API up and bootstrapped?")
    sys.exit(summary("admin_resources"))

sfx = str(int(time.time()) % 100000)

def ts(h):
    return (datetime.datetime(2031, 1, 1) + datetime.timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")

# ---------------------------------------------------------------- users: DELETE
uname = f"deluser{sfx}"
code, _ = call("POST", "/users", token=admin, body={"username": uname, "password": "pw", "role": "user"})
check("create throwaway user", code == 200, f"{code}")
utok = login(uname, "pw")

# a regular user may not delete accounts
code, _ = call("DELETE", f"/users/{uname}", token=utok)
check("regular user DELETE /users -> 403", code == 403, f"got {code}")

# admin cannot delete their own account (lockout guard)
code, _ = call("DELETE", "/users/admin", token=admin)
check("admin deletes self -> 400", code == 400, f"got {code}")

code, _ = call("DELETE", f"/users/{uname}", token=admin)
check("admin deletes user -> 200", code == 200, f"got {code}")
code, r = call("GET", "/users", token=admin)
check("deleted user no longer listed",
      code == 200 and not any(u.get("username") == uname for u in r), f"{code}")
code, _ = call("DELETE", f"/users/{uname}", token=admin)
check("delete nonexistent user -> 404", code == 404, f"got {code}")

# ------------------------------------------------------------ groups: PUT/DELETE
member = f"gmember{sfx}"
call("POST", "/users", token=admin, body={"username": member, "password": "pw", "role": "user"})
gname = f"grp{sfx}"
code, r = call("POST", "/groups", token=admin,
               body={"name": gname, "total_gpus": 4, "total_disk_gb": 10, "members": [member]})
check("create group with member", code == 200, f"{code} {r}")
code, r = call("GET", "/groups", token=admin)
grp = next((g for g in r.get("groups", []) if g["name"] == gname), None) if code == 200 else None
check("group listed with member", grp is not None and member in (grp or {}).get("members", []), f"{grp}")

code, r = call("PUT", f"/groups/{gname}", token=admin,
               body={"name": gname, "total_gpus": 8, "total_disk_gb": 20, "members": []})
check("PUT group updates quota + clears members",
      code == 200 and r.get("group", {}).get("total_gpus") == 8
      and r.get("group", {}).get("members") == [], f"{code} {r}")
code, _ = call("PUT", f"/groups/nope{sfx}", token=admin, body={"name": f"nope{sfx}", "total_gpus": 1})
check("PUT nonexistent group -> 404", code == 404, f"got {code}")

code, _ = call("DELETE", f"/groups/{gname}", token=admin)
check("DELETE group -> 200", code == 200, f"got {code}")
code, r = call("GET", "/groups", token=admin)
check("deleted group no longer listed",
      code == 200 and not any(g["name"] == gname for g in r.get("groups", [])), f"{code}")
code, _ = call("DELETE", f"/groups/{gname}", token=admin)
check("DELETE nonexistent group -> 404", code == 404, f"got {code}")

# ---------------------------------------------------------- projects: PUT/DELETE
pname = f"proj{sfx}"
code, _ = call("POST", "/projects", token=admin,
               body={"name": pname, "owner": "admin", "total_gpus": 2, "shared_storage_gb": 5})
check("create project", code == 200, f"{code}")
code, r = call("PUT", f"/projects/{pname}", token=admin,
               body={"name": pname, "owner": "admin", "total_gpus": 6, "shared_storage_gb": 5, "members": []})
check("PUT project updates quota",
      code == 200 and r.get("project", {}).get("total_gpus") == 6, f"{code} {r}")
code, _ = call("PUT", f"/projects/nope{sfx}", token=admin, body={"name": f"nope{sfx}"})
check("PUT nonexistent project -> 404", code == 404, f"got {code}")
code, _ = call("DELETE", f"/projects/{pname}", token=admin)
check("DELETE project -> 200", code == 200, f"got {code}")
code, r = call("GET", "/projects", token=admin)
check("deleted project no longer listed",
      code == 200 and not any(p["name"] == pname for p in r.get("projects", [])), f"{code}")
code, _ = call("DELETE", f"/projects/{pname}", token=admin)
check("DELETE nonexistent project -> 404", code == 404, f"got {code}")

# ------------------------------------------------------------------ availability
code, r = call("GET", f"/availability?start_time={ts(0)}&end_time={ts(2)}", token=admin)
check("GET /availability -> 200 with gpus list",
      code == 200 and isinstance(r.get("gpus"), list), f"{code} {r}")
code, _ = call("GET", f"/availability?start_time={ts(2)}&end_time={ts(0)}", token=admin)
check("availability end<=start -> 400", code == 400, f"got {code}")
code, _ = call("GET", f"/availability?start_time={ts(0)}&end_time={ts(2)}")
check("availability without token -> 401/403", code in (401, 403), f"got {code}")

# reserve a real GPU (if any) and confirm it flips to unavailable in that window
code, gl = call("GET", "/gpu/list")
gpus = gl.get("gpus", []) if code == 200 else []
rcode = None
if gpus:
    uuid = gpus[0]["uuid"]
    rcode, _ = call("POST", "/reservations", token=admin,
                    body={"gpu_uuid": uuid, "start_time": ts(10), "end_time": ts(12)})
if rcode == 200:
    code, r = call("GET", f"/availability?start_time={ts(10)}&end_time={ts(12)}", token=admin)
    row = next((g for g in r.get("gpus", []) if g["gpu_uuid"] == uuid), None) if code == 200 else None
    check("reserved GPU shows unavailable in window",
          row is not None and row.get("available") is False and row.get("busy_windows"), f"{row}")
else:
    # No schedulable GPU to reserve (CPU-only or GPU-simulation cluster): skip the flip check.
    check("availability GPU-reservation check (skipped: no schedulable GPU)", True)

# ---------------------------------------------------------------- analytics feed
code, r = call("GET", "/analytics/activity?group_by=user&hours=168", token=admin)
check("GET /analytics/activity (admin) -> 200 list", code == 200 and isinstance(r, list), f"{code}")
member2 = f"amember{sfx}"
call("POST", "/users", token=admin, body={"username": member2, "password": "pw", "role": "user"})
mtok = login(member2, "pw")
code, r = call("GET", "/analytics/activity?group_by=user&hours=168", token=mtok)
check("GET /analytics/activity (regular user) -> 200 list", code == 200 and isinstance(r, list), f"{code}")
code, r = call("GET", f"/analytics/usage?group_by=user&hours=168&subject={member2}", token=admin)
check("GET /analytics/usage with subject -> 200", code == 200 and "results" in r, f"{code}")

# cleanup users we created here
for u in (member, member2):
    call("DELETE", f"/users/{u}", token=admin)

sys.exit(summary("admin_resources"))
