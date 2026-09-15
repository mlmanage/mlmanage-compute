#!/usr/bin/env bash
# MLManage backend end-to-end test suite — single entry point.
#
# Usage:
#   ./run.sh                 # full run: setup test image, port-forward, all suites, cleanup
#   MLM_BASE=http://host:8000 ./run.sh   # against an already-exposed API (skip port-forward)
#   ./run.sh --no-image      # skip building/uploading the test image (reuse existing)
#
# Env overrides: MLM_BASE, KUBECTL, MLM_TEST_IMAGE, MLM_ADMIN_USER, MLM_ADMIN_PASS
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
PY="${PYTHON:-python3}"

# --- locate kubectl (repo-local binary or PATH) ---
if [ -z "${KUBECTL:-}" ]; then
  if [ -x "$REPO/kubectl" ]; then KUBECTL="$REPO/kubectl"; else KUBECTL="kubectl"; fi
fi
export KUBECTL

: "${MLM_BASE:=http://localhost:8000}"
: "${MLM_ADMIN_USER:=admin}"
: "${MLM_ADMIN_PASS:=admin}"
# Czy użytkownik PODAŁ obraz testowy jawnie? Jeśli nie, NIE zgadujemy prefiksu rejestru —
# weźmiemy go z `pull_ref`, które zwraca samo API po uploadzie (patrz sekcja 2 niżej).
# Zaszyte "localhost:5000" było poprawne tylko dla addonu registry w minikube (env=dummy);
# w env=professional API zwraca "localhost:32000" (jedyny adres, który węzły k3s mają
# w registries.yaml jako insecure mirror), więc pody zadań leciały w ImagePullBackOff.
if [ -n "${MLM_TEST_IMAGE:-}" ]; then MLM_TEST_IMAGE_EXPLICIT=1; else MLM_TEST_IMAGE_EXPLICIT=0; fi
: "${MLM_TEST_IMAGE:=localhost:5000/${MLM_ADMIN_USER}/mybench:latest}"
export MLM_BASE MLM_ADMIN_USER MLM_ADMIN_PASS MLM_TEST_IMAGE
export MLM_STATE="$(mktemp)"
export MLM_RESULT="$(mktemp)"

PF_PID=""
DO_IMAGE=1
[ "${1:-}" = "--no-image" ] && DO_IMAGE=0

cleanup() {
  [ -n "$PF_PID" ] && kill "$PF_PID" 2>/dev/null
  rm -f "$MLM_STATE" "$MLM_RESULT" /tmp/mlm_bench.tar 2>/dev/null
}
trap cleanup EXIT

c_grn(){ printf '\033[32m%s\033[0m\n' "$1"; }
c_red(){ printf '\033[31m%s\033[0m\n' "$1"; }
c_hdr(){ printf '\n\033[1m==== %s ====\033[0m\n' "$1"; }

api_up(){ curl -s -m 5 "$MLM_BASE/health" 2>/dev/null | grep -q '"status":"ok"'; }

# --- 0. wait for the deployment to be fully ready (runs at end of deploy-all.sh) ---
# Timeout configurable; default 20 min because first boot does pip installs + image pulls.
: "${MLM_WAIT_TIMEOUT:=1200}"
c_hdr "Setup: waiting for cluster to be ready (timeout ${MLM_WAIT_TIMEOUT}s)"

wait_rollout() {  # ns deploy
  "$KUBECTL" rollout status -n "$1" "deployment/$2" --timeout="${MLM_WAIT_TIMEOUT}s" >/dev/null 2>&1
}

deploy_exists() {  # ns deploy
  "$KUBECTL" get deployment -n "$1" "$2" >/dev/null 2>&1
}

# Core backend deployments that must always be Ready before tests make sense.
CORE_OK=1
for d in postgres devops-api devops-controller; do
  printf "  waiting for deployment/%s ... " "$d"
  if wait_rollout devops-system "$d"; then c_grn "ready"; else c_red "NOT READY"; CORE_OK=0; fi
done

# GPU-only deployments: the VRAM mutating webhook is deployed by deploy-all.sh
# ONLY when a GPU is present (HAS_GPU=1). On CPU-only machines it is intentionally
# absent, so we wait for it only if it actually exists in the cluster.
for d in vram-webhook; do
  if deploy_exists devops-system "$d"; then
    printf "  waiting for deployment/%s ... " "$d"
    if wait_rollout devops-system "$d"; then c_grn "ready"; else c_red "NOT READY"; CORE_OK=0; fi
  else
    printf "  deployment/%s ... " "$d"; c_grn "absent (CPU-only deploy, skipping)"
  fi
done
if [ "$CORE_OK" != "1" ]; then
  c_red "FATAL: core deployments not ready within ${MLM_WAIT_TIMEOUT}s"
  "$KUBECTL" get pods -n devops-system 2>&1 || true
  exit 2
fi

# Wait for any leftover non-terminal task/helper pods to settle (best-effort, short).
"$KUBECTL" wait --for=condition=Ready pods --all -n devops-system --timeout=120s >/dev/null 2>&1 || true

# --- 1. ensure API reachable (auto port-forward if not) ---
c_hdr "Setup: API connectivity"
if api_up; then
  echo "API reachable at $MLM_BASE"
else
  echo "API not reachable; starting port-forward to svc/devops-api ..."
  PF_LOG="$(mktemp)"
  # Try up to 3 times; each attempt waits ~30s for the forward to come up.
  for attempt in 1 2 3; do
    "$KUBECTL" port-forward -n devops-system svc/devops-api 8000:8000 >"$PF_LOG" 2>&1 &
    PF_PID=$!
    for i in $(seq 1 15); do
      sleep 2
      api_up && break 2
      # if the pf process already died, stop waiting and retry
      kill -0 "$PF_PID" 2>/dev/null || break
    done
    echo "  port-forward attempt $attempt did not connect; retrying... ($(tail -1 "$PF_LOG" 2>/dev/null))"
    kill "$PF_PID" 2>/dev/null; PF_PID=""
  done
  rm -f "$PF_LOG"
  if ! api_up; then c_red "FATAL: API not reachable at $MLM_BASE"; exit 2; fi
  echo "port-forward established (pid $PF_PID)"
fi

# --- 2. ensure a custom test image exists in the registry ---
if [ "$DO_IMAGE" = "1" ]; then
  c_hdr "Setup: test image ($MLM_TEST_IMAGE)"
  TOK=$(curl -s -X POST "$MLM_BASE/login" -H "Content-Type: application/json" \
        -d "$(MLM_USER="$MLM_ADMIN_USER" MLM_PASS="$MLM_ADMIN_PASS" "$PY" -c 'import json,os; print(json.dumps({"username":os.environ["MLM_USER"],"password":os.environ["MLM_PASS"]}))')" \
        | "$PY" -c 'import sys,json;print(json.load(sys.stdin).get("access_token",""))' 2>/dev/null)
  if [ -z "$TOK" ]; then c_red "FATAL: cannot log in as admin to upload test image"; exit 2; fi
  # Rebuild and upload when Docker is available because a database record does not prove
  # that the registry still contains the image blob. Without Docker, use the existing record.
  if command -v docker >/dev/null 2>&1; then
    # Use a dedicated empty build context to avoid unrelated temporary files and sockets.
    BCTX="$(mktemp -d)"
    printf 'FROM busybox:latest\nCMD ["echo","benchmark"]\n' > "$BCTX/Dockerfile"
    BUILD_LOG="$(mktemp)"
    if ! docker build -t mybench:latest "$BCTX" >"$BUILD_LOG" 2>&1; then
      c_red "FATAL: docker build of the test image failed:"; tail -20 "$BUILD_LOG"
      rm -rf "$BCTX" "$BUILD_LOG"; exit 2
    fi
    if ! docker save mybench:latest -o /tmp/mlm_bench.tar 2>"$BUILD_LOG"; then
      c_red "FATAL: docker save of the test image failed:"; tail -20 "$BUILD_LOG"
      rm -rf "$BCTX" "$BUILD_LOG"; exit 2
    fi
    rm -rf "$BCTX" "$BUILD_LOG"
    UP=$(curl -s -X POST "$MLM_BASE/images?name=mybench&tag=latest" \
      -H "Authorization: Bearer $TOK" -F "file=@/tmp/mlm_bench.tar")
    if printf '%s' "$UP" | grep -q '"pull_ref"'; then
      echo "uploaded mybench image (ensured present in registry)"
    else
      c_red "FATAL: image upload did not confirm: ${UP:-<empty response>}"; exit 2
    fi
  else
    UP=$(curl -s -H "Authorization: Bearer $TOK" "$MLM_BASE/images")
    HAVE=$(printf '%s' "$UP" \
           | "$PY" -c 'import sys,json;print(sum(1 for i in json.load(sys.stdin).get("images",[]) if i["name"]=="mybench"))' 2>/dev/null)
    if [ "${HAVE:-0}" = "0" ]; then
      c_red "FATAL: docker not found and no mybench image record; every task-running test would fail on ImagePullBackOff. Upload it manually or run on a docker host."
      exit 2
    fi
    echo "docker absent; trusting existing mybench DB record (blob not verified)"
  fi

  # Use the API-provided pull_ref because it contains the registry prefix for this deployment.
  if [ "$MLM_TEST_IMAGE_EXPLICIT" != "1" ]; then
    REF=$(printf '%s' "$UP" | "$PY" -c '
import sys, json
try: d = json.load(sys.stdin)
except Exception: sys.exit(0)
if isinstance(d, dict) and "image" in d:
    print(d["image"].get("pull_ref") or "")
else:
    for i in (d.get("images") or []):
        if i.get("name") == "mybench":
            print(i.get("pull_ref") or ""); break
' 2>/dev/null)
    if [ -n "$REF" ]; then
      MLM_TEST_IMAGE="$REF"; export MLM_TEST_IMAGE
      echo "test image reference from API: $MLM_TEST_IMAGE"
    else
      c_red "WARN: could not read pull_ref from API; falling back to $MLM_TEST_IMAGE"
    fi
  fi
fi

# --- 3. run suites in order (suite 1 creates shared state) ---
RC=0
for mod in test_01_auth_users test_02_reservations test_03_tasks_results_queue test_04_isolation test_05_admin_resources test_06_audit_log; do
  c_hdr "$mod"
  ( cd "$HERE" && "$PY" "$mod.py" ) || RC=1
done

# --- 4. aggregate ---
c_hdr "SUMMARY"
TOTP=0; TOTF=0
while IFS=$'\t' read -r name p f; do
  printf "  %-24s %s passed, %s failed\n" "$name" "$p" "$f"
  TOTP=$((TOTP+p)); TOTF=$((TOTF+f))
done < "$MLM_RESULT"
echo "  ------------------------------------------"
if [ "$TOTF" = "0" ]; then
  c_grn "  TOTAL: $TOTP passed, 0 failed  ✅"
else
  c_red "  TOTAL: $TOTP passed, $TOTF failed  ❌"
fi
exit $RC
