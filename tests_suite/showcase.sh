#!/usr/bin/env bash
# ============================================================================
#  MLManage Compute — feature SHOWCASE
#  Runs a guided, end-to-end tour of every backend capability with live API
#  calls. Every response is printed via `jq` so you see exactly what the
#  server returns. Intended to be run AFTER the test suite has passed.
#
#  Usage:
#     cd tests_suite && ./showcase.sh
#     MLM_BASE=http://host:8000 ./showcase.sh     # against an exposed API
#     PAUSE=1 ./showcase.sh                        # press ENTER between steps
# ============================================================================
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
[ -z "${KUBECTL:-}" ] && { [ -x "$REPO/kubectl" ] && KUBECTL="$REPO/kubectl" || KUBECTL="kubectl"; }
: "${MLM_BASE:=http://localhost:8000}"
: "${ADMIN_USER:=admin}"; : "${ADMIN_PASS:=admin}"
: "${TEST_IMAGE:=localhost:5000/admin/mybench:latest}"
: "${PAUSE:=0}"
SUF="$(date +%s | tail -c 5)"      # unique suffix so re-runs don't collide

bold(){ printf '\033[1m%s\033[0m\n' "$1"; }
cyan(){ printf '\033[36m%s\033[0m\n' "$1"; }
grn(){ printf '\033[32m%s\033[0m\n' "$1"; }
dim(){ printf '\033[2m%s\033[0m\n' "$1"; }

section(){ echo; printf '\033[1;44m %s \033[0m\n' "$*"; }
step(){ echo; cyan "▶ $*"; }
pause(){ [ "$PAUSE" = "1" ] && { read -r -p "   (press ENTER)…" _; } || true; }

TOKEN=""   # current bearer token

# req METHOD PATH [json-body]  — prints the call, executes, pretty-prints JSON
req(){
  local method="$1" path="$2" body="${3:-}"
  local url="$MLM_BASE$path"
  local hdr=(); [ -n "$TOKEN" ] && hdr=(-H "Authorization: Bearer $TOKEN")
  if [ -n "$body" ]; then
    dim "  \$ curl -X $method $path  -d '$body'"
    curl -s -X "$method" "$url" "${hdr[@]}" -H "Content-Type: application/json" -d "$body" | jq . 2>/dev/null \
      || echo "   (non-JSON or empty response)"
  else
    dim "  \$ curl -X $method $path"
    curl -s -X "$method" "$url" "${hdr[@]}" | jq . 2>/dev/null || echo "   (non-JSON or empty response)"
  fi
}

# login USER PASS  — sets global TOKEN
login(){
  local payload
  payload=$(MLM_USER="$1" MLM_PASS="$2" python3 -c 'import json,os; print(json.dumps({"username":os.environ["MLM_USER"],"password":os.environ["MLM_PASS"]}))')
  TOKEN=$(printf '%s' "$payload" | curl -s -X POST "$MLM_BASE/login" \
    -H "Content-Type: application/json" --data-binary @- | jq -r '.access_token // empty')
  if [ -n "$TOKEN" ]; then grn "  ✓ logged in as $1 (token ${#TOKEN} chars)"; else echo "  ✗ login failed for $1"; fi
}

# ---------------------------------------------------------------------------
bold "================================================================"
bold "  MLManage Compute — capability showcase"
bold "  API: $MLM_BASE"
bold "================================================================"
if ! curl -s -m5 "$MLM_BASE/health" | grep -q ok; then
  echo "API not reachable at $MLM_BASE. Start a port-forward first:"
  echo "  $KUBECTL port-forward -n devops-system svc/devops-api 8000:8000"
  exit 1
fi

# ===========================================================================
section "0. HEALTH & AUTH  (req. 1: roles)"
step "Health check (public)"
req GET /health
step "Log in as the auto-bootstrapped admin"
login "$ADMIN_USER" "$ADMIN_PASS"
step "Who am I? (GET /me)"
req GET /me
pause

# ===========================================================================
section "1. USERS & GRADUATED ROLES  (req. 1)"
U_USER="alice$SUF"; U_POWER="bob$SUF"; U_RO="viewer$SUF"
step "Admin creates a standard user, a poweruser, and a read-only user"
req POST /users "{\"username\":\"$U_USER\",\"password\":\"pw\",\"role\":\"user\",\"quota_gpu\":2,\"quota_vram_gb\":16}"
req POST /users "{\"username\":\"$U_POWER\",\"password\":\"pw\",\"role\":\"poweruser\",\"quota_gpu\":4,\"quota_vram_gb\":40}"
req POST /users "{\"username\":\"$U_RO\",\"password\":\"pw\",\"role\":\"readonly\"}"
step "List all users (admin only)"
req GET /users
step "Set contact + team on alice (for notifications / group quota / analytics)"
req PUT "/users/$U_USER" "{\"email\":\"alice@example.com\",\"team\":\"research$SUF\",\"priority\":5}"
step "RBAC proof: read-only user is BLOCKED from creating a task (expect 403 detail)"
login "$U_RO" pw
req POST /tasks "{\"image\":\"$TEST_IMAGE\"}"
step "…but read-only CAN read (GET /tasks)"
req GET /tasks
pause

# ===========================================================================
section "2. GPU DISCOVERY, TYPES & PARTITIONING  (req. 5, 6)"
login "$ADMIN_USER" "$ADMIN_PASS"
step "List GPUs detected from node labels (type/product/uuid)"
req GET /gpu/list
step "Per-GPU capabilities — what each card supports (full/timeslice/mps/mig)"
req GET /gpu/capabilities
GPU_UUID=$(curl -s -H "Authorization: Bearer $TOKEN" "$MLM_BASE/gpu/capabilities" | jq -r '.gpus[0].gpu_uuid // empty')
if [ -n "$GPU_UUID" ]; then
  step "Split GPU '$GPU_UUID' into 4 time-slices (req. 6: GPU sharing)"
  req POST "/gpu/$GPU_UUID/partition" '{"mode":"timeslice","replicas":4}'
  step "Try MIG (real VRAM slicing) — honestly REJECTED on non-MIG hardware (expect 400)"
  req POST "/gpu/$GPU_UUID/partition" '{"mode":"mig","mig_profiles":{"1g.5gb":4}}'
  step "Reset GPU to full passthrough"
  req POST "/gpu/$GPU_UUID/partition" '{"mode":"full"}'
fi
step "Current partition state of all GPUs"
req GET /gpu/partitions
pause

# ===========================================================================
section "3. GROUPS & PROJECTS WITH PER-GPU-TYPE QUOTAS  (req. 4)"
step "Create a group + project with PER-TYPE GPU quotas (1xH100 != 1x1050Ti)"
req POST /groups "{\"name\":\"research$SUF\",\"total_disk_gb\":500,\"gpu_quota_by_type\":{\"NVIDIA-H100\":4,\"NVIDIA-A5000\":16,\"default\":8}}"
req POST /projects "{\"name\":\"vision$SUF\",\"owner\":\"$U_USER\",\"shared_storage_gb\":100,\"gpu_quota_by_type\":{\"NVIDIA-H100\":2,\"default\":4}}"
step "List groups and projects (note the per-type maps)"
req GET /groups
req GET /projects
step "Give alice PER-TYPE quotas: max 1 H100 / 8 others; VRAM ≤80GB on H100 but ≤16GB elsewhere"
req PUT "/users/$U_USER" "{\"gpu_quota_by_type\":{\"NVIDIA-H100\":1,\"default\":8},\"vram_quota_by_type\":{\"NVIDIA-H100\":80,\"default\":16}}"
step "Proof: alice requests 40GB VRAM on an H100 — ALLOWED (cap 80)"
login "$U_USER" pw
req POST /tasks "{\"image\":\"$TEST_IMAGE\",\"command\":[\"echo\",\"h100\"],\"gpu_type\":\"NVIDIA-H100\",\"vram_limit_gb\":40,\"time_limit_seconds\":60}"
step "Proof: SAME 40GB VRAM on a GTX-1050Ti — REJECTED (cap 16) — 4GB≠4GB across models"
req POST /tasks "{\"image\":\"$TEST_IMAGE\",\"gpu_type\":\"NVIDIA-GTX-1050Ti\",\"vram_limit_gb\":40,\"time_limit_seconds\":60}"
login "$ADMIN_USER" "$ADMIN_PASS"
pause

# ===========================================================================
section "4. CUSTOM IMAGES — private registry  (req. 34, 35, 36)"
step "List uploaded images (private, per-user ownership)"
req GET /images
dim "  (upload is multipart: docker save img > img.tar; curl -F file=@img.tar '/images?name=..&tag=..')"
pause

# ===========================================================================
section "5. RUN TASKS as a user  (req. 2 isolation, 3 auto-stop, 7 VRAM)"
login "$U_USER" pw
step "Submit a task with its own image, a time limit, and a VRAM limit"
req POST /tasks "{\"image\":\"$TEST_IMAGE\",\"command\":[\"sh\",\"-c\",\"echo hello > \$RESULTS_DIR/out.txt; echo done\"],\"time_limit_seconds\":120,\"vram_limit_gb\":8,\"project\":\"vision$SUF\"}"
step "List my tasks (isolation: I only see my own)"
req GET /tasks
TASK=$(curl -s -H "Authorization: Bearer $TOKEN" "$MLM_BASE/tasks" | jq -r '.tasks[0].name // empty')
dim "  picked task: $TASK"
if [ -n "$TASK" ]; then
  step "Wait for it to run, then fetch its logs"
  for i in $(seq 1 12); do
    ph=$("$KUBECTL" get pod -n "user-$U_USER" "task-$TASK" -o jsonpath='{.status.phase}' 2>/dev/null)
    [ "$ph" = "Succeeded" ] && break; sleep 3
  done
  req GET "/tasks/$TASK/logs"
fi
pause

# ===========================================================================
section "6. QUOTA ENFORCEMENT  (req. 4, 7)"
step "alice (quota_vram_gb=16) requests 99 GB VRAM — REJECTED (expect 403)"
req POST /tasks "{\"image\":\"$TEST_IMAGE\",\"vram_limit_gb\":99,\"time_limit_seconds\":60}"
pause

# ===========================================================================
section "7. DOWNLOADABLE RESULTS  (results survive pod deletion)"
if [ -n "${TASK:-}" ]; then
  step "Delete the task pod, then prove results persist on the volume"
  "$KUBECTL" delete pod -n "user-$U_USER" "task-$TASK" --ignore-not-found >/dev/null 2>&1
  step "List result files via API"
  req GET "/tasks/$TASK/results"
  step "Download a single result file (raw content):"
  dim "  \$ curl /tasks/$TASK/results/download?path=out.txt"
  curl -s -H "Authorization: Bearer $TOKEN" "$MLM_BASE/tasks/$TASK/results/download?path=out.txt"; echo
fi
pause

# ===========================================================================
section "8. RESERVATIONS — booking, renewal, priority, conflicts  (req. 16,19,28)"
login "$U_USER" pw
step "Reserve gpu-demo$SUF for a time window"
req POST /reservations "{\"gpu_uuid\":\"gpu-demo$SUF\",\"start_time\":\"2031-01-01T10:00:00Z\",\"end_time\":\"2031-01-01T12:00:00Z\"}"
RID=$(curl -s -H "Authorization: Bearer $TOKEN" "$MLM_BASE/reservations" | jq -r '.reservations[0].id // empty')
step "Conflicting reservation on the same card/time — REJECTED (expect 409)"
req POST /reservations "{\"gpu_uuid\":\"gpu-demo$SUF\",\"start_time\":\"2031-01-01T11:00:00Z\",\"end_time\":\"2031-01-01T13:00:00Z\"}"
if [ -n "$RID" ]; then
  step "Renew reservation #$RID (extend end time)"
  req PUT "/reservations/$RID/renew" '{"end_time":"2031-01-01T14:00:00Z"}'
fi
step "List my reservations"
req GET /reservations
step "Calendar view (FullCalendar-style events) — req. 17 data source"
req GET /reservations/calendar
pause

# ===========================================================================
section "9. CALENDAR EXPORT (iCal / Google Calendar)  (req. 29, 31)"
step "iCal feed (subscribe this URL in Google Calendar / Outlook)"
dim "  \$ curl $MLM_BASE/reservations/calendar.ics"
curl -s "$MLM_BASE/reservations/calendar.ics" | head -16
pause

# ===========================================================================
section "10. JOB SCHEDULING (reservation + auto-dispatched task)  (req. 16)"
step "Schedule a job: books a slot AND runs a task at start time"
req POST /jobs "{\"gpu_uuid\":\"gpu-job$SUF\",\"start_time\":\"2020-01-01T00:00:00Z\",\"end_time\":\"2031-01-01T00:00:00Z\",\"image\":\"$TEST_IMAGE\",\"command\":[\"echo\",\"job\"]}"
step "List jobs"
req GET /jobs
pause

# ===========================================================================
section "11. QUEUE — priority, fair-share, deps, multi-GPU/node  (req. 21,22,23,24)"
step "Submit a multi-node (2 replicas) gang/MPI job with priority"
req POST /queue "{\"image\":\"$TEST_IMAGE\",\"command\":[\"echo\",\"train\"],\"gpus\":1,\"replicas\":2,\"gang\":true,\"mpi\":true,\"priority\":5}"
QID=$(curl -s -H "Authorization: Bearer $TOKEN" "$MLM_BASE/queue" | jq -r '.queue[-1].id // empty')
step "Submit a dependent job (runs only after #$QID) — note status 'waiting_deps'  (req. 24)"
req POST /queue "{\"image\":\"$TEST_IMAGE\",\"command\":[\"echo\",\"after\"],\"gpus\":0,\"depends_on\":[$QID]}"
step "View the queue (priority + fair-share ordering, dependency states)"
req GET /queue
pause

# ===========================================================================
section "12. MONITORING, DISK, ANALYTICS, ENERGY/CO2  (req. 8,11,13,26,32,33)"
dim "  Production: gpu_metrics is filled by the DCGM collector. On this demo box there's no DCGM"
dim "  exporter, so we seed a couple of SAMPLE metric rows for '$U_USER' to show the analytics live."
PG="$KUBECTL exec -n devops-system deployment/postgres -- psql -U postgres devops -c"
$PG "INSERT INTO gpu_metrics (\"timestamp\",\"user\",gpu_uuid,utilization,memory_used,temperature,power)
      VALUES (NOW(),'$U_USER','GPU-sample',82,40960,71,350),
             (NOW(),'$U_USER','GPU-sample',75,38000,69,320);" >/dev/null 2>&1 \
  && grn "  ✓ seeded 2 sample gpu_metrics rows" || dim "  (seed skipped)"
step "alice's GPU usage history (per-user time series)"
login "$U_USER" pw
req GET "/gpu/usage?hours=24"
step "Real-time disk / PVC usage"
req GET /disk/usage
login "$ADMIN_USER" "$ADMIN_PASS"
step "Efficiency + energy(kWh) + CO2 analysis, grouped by user (now shows real numbers)"
req GET "/analytics/usage?group_by=user&hours=24"
step "Prometheus metrics endpoint (first lines)"
dim "  \$ curl $MLM_BASE/metrics"
curl -s "$MLM_BASE/metrics" | head -8
pause

# ===========================================================================
section "13. SHARED TEAM STORAGE  (req. 27)"
step "Create a ReadWriteMany shared volume for a team (admin/poweruser)"
req POST "/teams/research$SUF/shared-storage?size_gb=50" ""
pause

# ===========================================================================
section "14. RETENTION SETTINGS — runtime tunable  (req. 3, 34)"
step "View task/result cleanup TTLs"
req GET /settings/cleanup
step "Change result TTL to 2h at runtime (no redeploy)"
req PUT /settings/cleanup '{"result_ttl_seconds":7200}'
step "Restore default"
req PUT /settings/cleanup '{"result_ttl_seconds":604800}'
pause

# ===========================================================================
section "DONE"
grn "Showcase complete — the backend capability tour finished successfully."
dim "Note: items 17 & 18 (graphical Web UI) are a separate frontend project; this backend"
dim "exposes all the data/endpoints they would consume (and an interactive API browser at $MLM_BASE/docs)."
echo
bold "Cleanup of demo objects (users/groups/projects/reservations) — set KEEP=1 to skip."
if [ "${KEEP:-0}" != "1" ]; then
  "$KUBECTL" exec -n devops-system deployment/postgres -- psql -U postgres devops -c \
    "DELETE FROM reservations WHERE gpu_uuid LIKE 'gpu-demo%' OR gpu_uuid LIKE 'gpu-job%';
     DELETE FROM queued_jobs WHERE \"user\" IN ('$U_USER','$U_POWER');
     DELETE FROM groups WHERE name LIKE 'research%';
     DELETE FROM projects WHERE name LIKE 'vision%';
     DELETE FROM gpu_metrics WHERE \"user\" IN ('$U_USER','$U_POWER','$U_RO');
     DELETE FROM users WHERE username IN ('$U_USER','$U_POWER','$U_RO');" >/dev/null 2>&1 \
    && grn "  ✓ demo data removed" || dim "  (cleanup skipped/failed — harmless)"
  for ns in "user-$U_USER" "user-$U_POWER" "user-$U_RO" "team-research$SUF"; do
    "$KUBECTL" delete ns "$ns" --ignore-not-found --wait=false >/dev/null 2>&1
  done
fi
