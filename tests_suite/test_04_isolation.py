#!/usr/bin/env python3
"""Suite 4: per-user isolation (description.txt core) + input validation edge cases."""
import sys, time, subprocess
from lib import call, login, admin_token, check, summary, load_state, TEST_IMAGE, KUBECTL

admin = admin_token()
st = load_state()
uname = st.get("uname")
utok = login(uname, "pw") if uname else None
if not (admin and utok):
    print("FATAL: missing tokens (run suite 1 first)")
    sys.exit(summary("isolation"))

# isolation: user task lands only in their namespace
code, r = call("POST", "/tasks", token=utok, body={"image": TEST_IMAGE, "command": ["sleep", "60"], "time_limit_seconds": 120})
tname = r.get("task")
check("user can create own task", code == 200 and tname, f"{code} {r}")
time.sleep(5)
own = subprocess.run([KUBECTL, "get", "pod", "-n", f"user-{uname}", f"task-{tname}", "-o", "jsonpath={.metadata.name}"],
                     capture_output=True, text=True).stdout.strip()
check("task pod is in user's OWN namespace", own == f"task-{tname}", f"got '{own}'")
elsewhere = subprocess.run([KUBECTL, "get", "pod", "-n", "user-admin", f"task-{tname}"],
                           capture_output=True, text=True).returncode
check("task pod NOT in another user's namespace", elsewhere != 0, "found in user-admin!")

# isolation: cannot read others' logs/results
code, r = call("POST", "/tasks", token=admin, body={"image": TEST_IMAGE, "command": ["sleep", "60"], "time_limit_seconds": 120})
atask = r.get("task")
time.sleep(5)
code, _ = call("GET", f"/tasks/{atask}/logs", token=utok)
check("user reading admin's task logs -> 404 (isolated)", code == 404, f"got {code}")
code, _ = call("GET", f"/tasks/{atask}/results", token=utok)
check("user reading admin's task results -> 404 (isolated)", code == 404, f"got {code}")

# namespace ownership label
lbl = subprocess.run([KUBECTL, "get", "ns", f"user-{uname}", "-o", r"jsonpath={.metadata.labels.devops\.dev/user}"],
                     capture_output=True, text=True).stdout.strip()
check("user namespace labeled with owner", lbl == uname, f"got '{lbl}'")

# auth on logs
code, _ = call("GET", f"/tasks/{tname}/logs")
check("logs without auth -> 401/403", code in (401, 403), f"got {code}")

# input validation
code, _ = call("POST", "/queue", token=utok, body={"image": TEST_IMAGE, "gpus": -1, "replicas": 1})
check("queue negative gpus handled (no 500)", code in (200, 400, 422), f"got {code}")
code, _ = call("POST", "/jobs", token=utok, body={"gpu_uuid": "g", "start_time": "2030-01-01T02:00:00Z",
              "end_time": "2030-01-01T01:00:00Z", "image": TEST_IMAGE})
check("job end < start -> 400", code == 400, f"got {code}")

# cleanup
for t in (tname, atask):
    call("DELETE", f"/tasks/{t}", token=admin)

sys.exit(summary("isolation"))
