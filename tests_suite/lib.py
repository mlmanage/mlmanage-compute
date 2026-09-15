#!/usr/bin/env python3
"""Shared helpers for the MLManage backend test suite (stdlib-only)."""
import json, os, time, subprocess, urllib.request, urllib.error

BASE = os.environ.get("MLM_BASE", "http://localhost:8000")
KUBECTL = os.environ.get("KUBECTL", "kubectl")
TEST_IMAGE = os.environ.get("MLM_TEST_IMAGE", "localhost:5000/admin/mybench:latest")
ADMIN_USER = os.environ.get("MLM_ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("MLM_ADMIN_PASS", "admin")
STATE = os.environ.get("MLM_STATE", "/tmp/mlm_test_state.json")

_PASS, _FAIL = [], []


def call(method, path, token=None, body=None, raw=False):
    """HTTP request. Returns (status_code, parsed_body) or (None, 'EXC:..')."""
    url = BASE + path
    headers, data = {}, None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            content = r.read()
            return r.status, (content if raw else _parse(content))
    except urllib.error.HTTPError as e:
        content = e.read()
        return e.code, (content if raw else _parse(content))
    except Exception as e:
        return None, f"EXC:{e}"


def _parse(content):
    try:
        return json.loads(content or b"{}")
    except Exception:
        return content.decode(errors="replace")


def login(user, pw):
    code, r = call("POST", "/login", body={"username": user, "password": pw})
    return r.get("access_token") if code == 200 and isinstance(r, dict) else None


def admin_token():
    return login(ADMIN_USER, ADMIN_PASS)


def check(name, cond, detail=""):
    if cond:
        _PASS.append(name)
        print(f"  \033[32mPASS\033[0m  {name}")
    else:
        _FAIL.append((name, detail))
        print(f"  \033[31mFAIL\033[0m  {name}   {detail}")
    return cond


def kget(ns, kind, name, jsonpath="{.status.phase}"):
    return subprocess.run([KUBECTL, "get", kind, "-n", ns, name, "-o", f"jsonpath={jsonpath}"],
                          capture_output=True, text=True).stdout.strip()


def kdel_pod(ns, name):
    subprocess.run([KUBECTL, "delete", "pod", "-n", ns, name, "--ignore-not-found"], capture_output=True)


def wait_phase(ns, pod, target=("Running", "Succeeded"), tries=20, delay=3):
    for _ in range(tries):
        ph = kget(ns, "pod", pod)
        if ph in target:
            return ph
        time.sleep(delay)
    return kget(ns, "pod", pod)


def save_state(d):
    with open(STATE, "w") as f:
        json.dump(d, f)


def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def summary(title):
    print(f"\n[{title}] {len(_PASS)} passed, {len(_FAIL)} failed")
    for n, d in _FAIL:
        print(f"   FAILED: {n} :: {d}")
    # write per-module result for the runner to aggregate
    with open(os.environ.get("MLM_RESULT", "/tmp/mlm_result.txt"), "a") as f:
        f.write(f"{title}\t{len(_PASS)}\t{len(_FAIL)}\n")
    return 1 if _FAIL else 0
