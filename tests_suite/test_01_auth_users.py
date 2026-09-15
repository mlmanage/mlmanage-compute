#!/usr/bin/env python3
"""Suite 1: authentication, user management, RBAC."""
import sys, time
from lib import call, login, admin_token, check, summary, save_state

admin = admin_token()
check("admin login (admin/admin)", admin is not None)
if not admin:
    print("FATAL: cannot get admin token; is the API up and bootstrapped?")
    sys.exit(summary("auth_users"))

code, _ = call("POST", "/login", body={"username": "admin", "password": "wrong"})
check("login wrong password -> 401", code == 401, f"got {code}")
code, _ = call("POST", "/login", body={"username": "ghost_nobody", "password": "x"})
check("login nonexistent user -> 401", code == 401, f"got {code}")
code, _ = call("GET", "/me")
check("protected endpoint no token -> 401/403", code in (401, 403), f"got {code}")
code, _ = call("GET", "/me", token="garbage.token.value")
check("bad token -> 401", code == 401, f"got {code}")

code, r = call("GET", "/users", token=admin)
check("admin GET /users", code == 200)

uname = "tuser" + str(int(time.time()) % 100000)
code, r = call("POST", "/users", token=admin, body={"username": uname, "password": "pw", "role": "user"})
check("admin create user", code == 200, f"{code} {r}")
utok = login(uname, "pw")
check("new user can login", utok is not None)

code, _ = call("GET", "/users", token=utok)
check("regular user GET /users -> 403", code == 403, f"got {code}")
code, _ = call("POST", "/users", token=utok, body={"username": "x", "password": "y"})
check("regular user POST /users -> 403", code == 403, f"got {code}")

code, r = call("GET", "/me", token=utok)
check("new user /me has workspace quotas", code == 200 and r.get("disk_scratch_gb"), f"{r}")

code, r = call("PUT", f"/users/{uname}", token=admin, body={"email": "t@e.com", "team": "red", "priority": 7})
check("admin PUT /users profile", code == 200 and r.get("user", {}).get("team") == "red", f"{code} {r}")
code, _ = call("PUT", f"/users/{uname}", token=utok, body={"email": "x"})
check("regular user PUT /users -> 403", code == 403, f"got {code}")

code, r = call("GET", "/gpu/list")
check("GET /gpu/list (public)", code == 200 and "gpus" in r, f"{code}")

code, r = call("GET", "/health")
check("GET /health", code == 200 and r.get("status") == "ok", f"{code}")

# readonly role: can read, cannot write
rname = "viewer" + str(int(time.time()) % 100000)
call("POST", "/users", token=admin, body={"username": rname, "password": "pw", "role": "readonly"})
rotok = login(rname, "pw")
check("readonly user can login", rotok is not None)
code, _ = call("GET", "/tasks", token=rotok)
check("readonly GET /tasks -> 200", code == 200, f"got {code}")
code, _ = call("POST", "/tasks", token=rotok, body={"image": "x"})
check("readonly POST /tasks -> 403", code == 403, f"got {code}")

# quota enforcement: VRAM beyond quota rejected
lname = "limited" + str(int(time.time()) % 100000)
call("POST", "/users", token=admin, body={"username": lname, "password": "pw", "role": "user",
                                          "quota_gpu": 1, "quota_vram_gb": 4})
ltok = login(lname, "pw")
import os as _os
IMG = _os.environ.get("MLM_TEST_IMAGE", "localhost:5000/admin/mybench:latest")
code, _ = call("POST", "/tasks", token=ltok, body={"image": IMG, "vram_limit_gb": 8, "time_limit_seconds": 60})
check("VRAM over quota -> 403", code == 403, f"got {code}")
code, _ = call("POST", "/tasks", token=ltok, body={"image": IMG, "command": ["echo", "ok"], "vram_limit_gb": 4, "time_limit_seconds": 60})
check("VRAM within quota -> 200", code == 200, f"got {code}")

# PER-TYPE quotas: same VRAM number means different things on different GPU models
ptname = "ptype" + str(int(time.time()) % 100000)
call("POST", "/users", token=admin, body={"username": ptname, "password": "pw", "role": "user"})
code, r = call("PUT", f"/users/{ptname}", token=admin,
               body={"gpu_quota_by_type": {"NVIDIA-H100": 1, "default": 8},
                     "vram_quota_by_type": {"NVIDIA-H100": 80, "default": 16}})
check("set per-type GPU+VRAM quotas", code == 200 and r["user"]["gpu_quota_by_type"]["NVIDIA-H100"] == 1, f"{code} {r}")
pttok = login(ptname, "pw")
# VRAM 40GB: allowed on H100 (cap 80), rejected on a low-end type (default cap 16)
code, _ = call("POST", "/tasks", token=pttok, body={"image": IMG, "command": ["echo", "h"], "gpu_type": "NVIDIA-H100", "vram_limit_gb": 40, "time_limit_seconds": 60})
check("VRAM 40 on H100 (cap 80) -> 200", code == 200, f"got {code}")
code, _ = call("POST", "/tasks", token=pttok, body={"image": IMG, "gpu_type": "NVIDIA-GTX-1050Ti", "vram_limit_gb": 40, "time_limit_seconds": 60})
check("VRAM 40 on 1050Ti (default cap 16) -> 403", code == 403, f"got {code}")
# GPU count per type: H100 capped at 1; a different type still allowed concurrently
code, r1 = call("POST", "/tasks", token=pttok, body={"image": IMG, "command": ["sleep", "120"], "gpu_type": "NVIDIA-H100", "resources": {"limits": {"nvidia.com/gpu": 1}}, "time_limit_seconds": 120})
check("1st H100 GPU (cap 1) -> 200", code == 200, f"got {code}")
# Wait until the 1st task's pod actually exists (controller latency) so it counts toward usage.
import subprocess as _sp
from lib import KUBECTL as _K
t1 = r1.get("task") if isinstance(r1, dict) else None
if t1:
    for _ in range(20):
        ph = _sp.run([_K, "get", "pod", "-n", f"user-{ptname}", f"task-{t1}", "-o", "jsonpath={.status.phase}"],
                     capture_output=True, text=True).stdout.strip()
        if ph in ("Pending", "Running"):
            break
        time.sleep(2)
code, _ = call("POST", "/tasks", token=pttok, body={"image": IMG, "gpu_type": "NVIDIA-H100", "resources": {"limits": {"nvidia.com/gpu": 1}}, "time_limit_seconds": 120})
check("2nd H100 GPU over per-type cap -> 403", code == 403, f"got {code}")
code, _ = call("POST", "/tasks", token=pttok, body={"image": IMG, "command": ["echo", "a"], "gpu_type": "NVIDIA-A5000", "resources": {"limits": {"nvidia.com/gpu": 1}}, "time_limit_seconds": 120})
check("different GPU type still allowed (independent) -> 200", code == 200, f"got {code}")

save_state({"uname": uname})
sys.exit(summary("auth_users"))
