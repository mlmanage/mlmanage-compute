#!/usr/bin/env python3
"""Suite 3: tasks, VRAM, results download, queue, analytics, disk, teams, image cleanup settings."""
import sys, time, subprocess
from lib import (call, login, admin_token, check, summary, TEST_IMAGE, KUBECTL,
                 kget, kdel_pod, wait_phase)

admin = admin_token()
if not admin:
    print("FATAL: no admin token")
    sys.exit(summary("tasks_results_queue"))

# tasks + RBAC
code, r = call("POST", "/tasks", token=admin, body={"image": TEST_IMAGE, "command": ["echo", "hi"], "time_limit_seconds": 120})
check("create task", code == 200 and r.get("task"), f"{code} {r}")
code, r = call("GET", "/tasks", token=admin)
check("list tasks", code == 200 and "tasks" in r, f"{code}")
code, _ = call("POST", "/tasks", body={"image": TEST_IMAGE})
check("create task no auth -> 401/403", code in (401, 403), f"got {code}")

# VRAM limit injection
code, r = call("POST", "/tasks", token=admin, body={"image": TEST_IMAGE, "command": ["sleep", "60"], "vram_limit_gb": 8, "time_limit_seconds": 120})
tv = r.get("task")
wait_phase("user-admin", f"task-{tv}", target=("Running", "Succeeded"))
out = subprocess.run([KUBECTL, "get", "pod", "-n", "user-admin", f"task-{tv}", "-o",
                      "jsonpath={.spec.containers[0].env[?(@.name=='VRAM_LIMIT_MB')].value}"],
                     capture_output=True, text=True).stdout.strip()
check("VRAM_LIMIT_MB injected = 8192", out == "8192", f"got '{out}'")
kdel_pod("user-admin", f"task-{tv}")

# results download (persist past pod deletion)
code, r = call("POST", "/tasks", token=admin, body={"image": TEST_IMAGE,
              "command": ["sh", "-c", "echo RESULT_OK=1 > $RESULTS_DIR/out.txt"], "time_limit_seconds": 120})
tr = r.get("task")
wait_phase("user-admin", f"task-{tr}", target=("Succeeded",))
kdel_pod("user-admin", f"task-{tr}")
code, r = call("GET", f"/tasks/{tr}/results", token=admin)
check("list results after pod deleted", code == 200 and any(f.get("path") == "out.txt" for f in r.get("files", [])), f"{code} {r}")
code, raw = call("GET", f"/tasks/{tr}/results/download?path=out.txt", token=admin, raw=True)
check("download result file", code == 200 and b"RESULT_OK=1" in raw, f"{code}")
code, _ = call("GET", f"/tasks/{tr}/results/download?path=../../etc/passwd", token=admin)
check("path traversal blocked -> 400", code == 400, f"got {code}")

# queue: priority / deps / dispatch
code, r = call("POST", "/queue", token=admin, body={"image": TEST_IMAGE, "command": ["echo", "q"], "gpus": 0, "replicas": 1, "priority": 2})
check("queue submit", code == 200 and r["job"]["status"] == "queued", f"{code} {r}")
qid = r["job"]["id"]
code, r = call("POST", "/queue", token=admin, body={"image": TEST_IMAGE, "command": ["echo", "d"], "gpus": 0, "depends_on": [qid]})
check("queue with deps -> waiting_deps", code == 200 and r["job"]["status"] == "waiting_deps", f"{code} {r}")
code, _ = call("POST", "/queue", token=admin, body={"image": TEST_IMAGE, "command": ["echo"], "depends_on": [999999]})
check("queue bad dependency -> 400", code == 400, f"got {code}")
code, r = call("GET", "/queue", token=admin)
check("queue list", code == 200 and "queue" in r, f"{code}")
time.sleep(22)
code, r = call("GET", "/queue", token=admin)
disp = [j for j in r.get("queue", []) if j["id"] == qid]
check("queue job dispatched (running/completed)", bool(disp) and disp[0]["status"] in ("running", "completed"), f"{disp}")

# analytics / disk / teams
code, r = call("GET", "/analytics/usage?group_by=team", token=admin)
check("analytics usage", code == 200 and "results" in r, f"{code}")
code, r = call("GET", "/disk/usage", token=admin)
check("disk usage (real-time)", code == 200 and "volumes" in r, f"{code}")
code, r = call("POST", "/teams/redteam/shared-storage?size_gb=5", token=admin)
check("team shared storage create", code == 200 and r.get("pvc"), f"{code} {r}")

# images
code, r = call("GET", "/images", token=admin)
check("images list", code == 200 and "images" in r, f"{code}")

# group/project quota enforcement
import time as _t
gname = "qg" + str(int(_t.time()) % 100000)
pname = "qp" + str(int(_t.time()) % 100000)
qmember = "qm" + str(int(_t.time()) % 100000)
code, _ = call("POST", "/groups", token=admin, body={"name": gname, "total_gpus": 0})
check("create group (quota 0)", code == 200, f"{code}")
code, _ = call("POST", "/projects", token=admin, body={"name": pname, "total_gpus": 0})
check("create project (quota 0)", code == 200, f"{code}")
call("POST", "/users", token=admin, body={"username": qmember, "password": "pw", "role": "user", "quota_gpu": 10, "quota_vram_gb": 40})
call("PUT", f"/users/{qmember}", token=admin, body={"team": gname})
qtok = login(qmember, "pw")
code, _ = call("POST", "/tasks", token=qtok, body={"image": TEST_IMAGE, "resources": {"limits": {"nvidia.com/gpu": 1}}, "time_limit_seconds": 60})
check("group GPU quota 0 blocks GPU task -> 403", code == 403, f"got {code}")
# raise group, but project still 0
call("POST", "/groups", token=admin, body={"name": gname, "total_gpus": 4})
code, _ = call("POST", "/tasks", token=qtok, body={"image": TEST_IMAGE, "resources": {"limits": {"nvidia.com/gpu": 1}}, "project": pname, "time_limit_seconds": 60})
check("project GPU quota 0 blocks GPU task -> 403", code == 403, f"got {code}")
# cleanup
for tn in (subprocess.run([KUBECTL, "get", "tasks", "-n", f"user-{qmember}", "--no-headers", "-o", "custom-columns=:metadata.name"], capture_output=True, text=True).stdout.split()):
    subprocess.run([KUBECTL, "delete", "task", "-n", f"user-{qmember}", tn], capture_output=True)

# cleanup settings (tasks + results TTL)
code, r = call("PUT", "/settings/cleanup", token=admin, body={"result_ttl_seconds": 120})
check("set result_ttl (valid) ok", code == 200 and r.get("result_ttl_seconds") == 120, f"{code} {r}")
code, _ = call("PUT", "/settings/cleanup", token=admin, body={"result_ttl_seconds": 5})
check("result_ttl < 60 -> 400", code == 400, f"got {code}")
call("PUT", "/settings/cleanup", token=admin, body={"result_ttl_seconds": 604800})  # reset

# GPU partitioning (hardware-agnostic; validates against detected capability)
code, r = call("GET", "/gpu/capabilities", token=admin)
gpus = r.get("gpus", []) if code == 200 else []
check("gpu capabilities", code == 200 and "gpus" in r, f"{code}")
if gpus:
    g = gpus[0]; uuid = g["gpu_uuid"]; modes = g.get("supported_modes", [])
    check("capabilities report supported_modes", "full" in modes, f"{modes}")
    code, r = call("POST", f"/gpu/{uuid}/partition", token=admin, body={"mode": "full"})
    check("set full partition", code == 200 and r.get("partition", {}).get("mode") == "full", f"{code} {r}")
    # MIG must be rejected on non-MIG hardware (honest 400)
    if "mig" not in modes:
        code, _ = call("POST", f"/gpu/{uuid}/partition", token=admin,
                       body={"mode": "mig", "mig_profiles": {"1g.5gb": 7}})
        check("MIG rejected on non-MIG GPU -> 400", code == 400, f"got {code}")
    # timeslice requires replicas
    code, _ = call("POST", f"/gpu/{uuid}/partition", token=admin, body={"mode": "timeslice"})
    check("timeslice without replicas -> 400", code == 400, f"got {code}")
    # non-admin cannot partition
    code, _ = call("POST", f"/gpu/{uuid}/partition", body={"mode": "full"})
    check("partition without auth -> 401/403", code in (401, 403), f"got {code}")
    # reset to full to avoid disturbing the node
    call("POST", f"/gpu/{uuid}/partition", token=admin, body={"mode": "full"})
else:
    print("  (no GPUs detected; skipping partition assertions)")
code, _ = call("POST", "/gpu/nonexistent-uuid/partition", token=admin, body={"mode": "full"})
check("partition nonexistent GPU -> 404", code == 404, f"got {code}")

# Slice binding (hardware-agnostic checks)
import datetime as _dt
_s = (_dt.datetime(2032, 1, 1)).strftime("%Y-%m-%dT%H:%M:%SZ")
_e = (_dt.datetime(2032, 1, 1, 2)).strftime("%Y-%m-%dT%H:%M:%SZ")
code, _ = call("POST", "/reservations", token=admin,
               body={"gpu_uuid": "unpartitioned-gpu-x", "start_time": _s, "end_time": _e, "gpu_partition": "1g.5gb"})
check("slice reservation on unpartitioned GPU -> 400", code == 400, f"got {code}")
# task with gpu_partition -> CR carries migProfile -> pod requests nvidia.com/mig-<profile>
code, r = call("POST", "/tasks", token=admin,
               body={"image": TEST_IMAGE, "command": ["echo", "mig"], "gpu_partition": "1g.5gb", "time_limit_seconds": 60})
check("task with gpu_partition created", code == 200 and r.get("task"), f"{code} {r}")
if code == 200:
    tnm = r["task"]
    time.sleep(3)
    prof = kget("user-admin", "task", tnm, "{.spec.migProfile}")
    check("Task CR carries migProfile=1g.5gb", prof == "1g.5gb", f"got '{prof}'")
    res_lim = subprocess.run([KUBECTL, "get", "pod", "-n", "user-admin", f"task-{tnm}",
                              "-o", "jsonpath={.spec.containers[0].resources.limits}"],
                             capture_output=True, text=True).stdout
    check("pod requests nvidia.com/mig-1g.5gb", "nvidia.com/mig-1g.5gb" in res_lim, f"got '{res_lim}'")
    subprocess.run([KUBECTL, "delete", "task", "-n", "user-admin", tnm, "--ignore-not-found"], capture_output=True)

sys.exit(summary("tasks_results_queue"))
