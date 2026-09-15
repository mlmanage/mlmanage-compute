# MLManage Compute – AI Training Platform on Kubernetes

[![Kubernetes](https://img.shields.io/badge/kubernetes-1.20+-blue.svg)](https://kubernetes.io/)
[![GPU Operator](https://img.shields.io/badge/NVIDIA-GPU%20Operator-green.svg)](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/overview.html)

**MLManage Compute** is a backend platform for managing AI/ML training workloads on Kubernetes. It provides user management, GPU reservations, job scheduling, resource isolation, and monitoring for teams sharing GPU clusters.

See the [project documentation](Documentation/README.md) for architecture, deployment, API, operations, and development guides.

---

## Features

- **User roles** – admin, poweruser, standard, read‑only with RBAC
- **GPU management** – hardware detection, per‑GPU partitioning (time‑slicing / MPS / MIG, validated against each card's capability), per‑task VRAM limits, and **per‑GPU‑type quotas** (count *and* VRAM) for users/groups/projects — so 1×H100 ≠ 1×1050Ti and quotas can't be gamed. Containers get bare‑metal GPU access (NVIDIA runtime, real CUDA/NVLink).
- **Job scheduling** – create tasks with custom Docker images, resource limits, and timeouts; automatic termination; or schedule jobs (reservation + auto‑dispatched task) via `/jobs`
- **GPU reservations** – calendar‑based booking with conflict validation, renewal (`/reservations/{id}/renew`), cancellation, and expiry reminders
- **Resource isolation** – each user gets a dedicated namespace and dedicated workspace volumes (home/scratch/project) auto‑created on user creation
- **Custom images** – upload your own container image (`docker save` tarball) to the in‑cluster registry and run tasks with it
- **Connect to pods** – stream task logs and open an interactive shell into a running task via the API (WebSocket exec)
- **Task results** – tasks write to a persistent volume; download outputs (single file or `.tar`) via the API after the pod is gone
- **Job queue** – priority + fair‑share scheduling, backfill, dependency management, and multi‑GPU / multi‑node (gang / MPI) parallel jobs
- **Reservation priority** – higher‑priority reservations preempt lower‑priority conflicting ones
- **Notifications** – e‑mail / Slack alerts before a reservation starts or ends (optional, env‑configured)
- **Calendar export/import** – iCal feed for Google Calendar subscription + iCal import
- **Analytics** – resource‑efficiency, energy (kWh) and CO₂ estimates per user / team / project
- **Team shared storage** – ReadWriteMany volumes per team with role‑based access
- **Image lifecycle** – versioning/tagging plus automatic cleanup of unused/old images
- **Automatic cleanup** – task pods removed after their time limit; job tasks removed when the reservation ends; standalone tasks + result data after configurable TTLs (runtime‑tunable via API)
- **Monitoring** – Prometheus + Grafana with GPU metrics (utilization, temperature, power, memory) collected per user into the database; real‑time disk usage via API
- **Storage** – persistent home, scratch, project volumes with soft quota alerts
- **Audit trail** – who did what, when, to what, and how it ended: every state‑changing request and every sign‑in attempt (including refused ones) is recorded in the database and readable by admins via `GET /audit-log` or Administration → Activity log. Passwords and tokens are never stored — secret‑looking fields are replaced with `***`; only boolean diagnostics such as `password_correct: false` are kept, so a failed sign‑in still explains itself. Reads (`GET`) are not recorded by default, and entries older than the retention window are purged automatically (`AUDIT_*` env vars).
- **REST API** – fully documented, JWT‑authenticated, ready for frontend integration

---

## Prerequisites

### For the target machine (with NVIDIA GPUs)
- Ubuntu 22.04 / 24.04 (or any Linux with NVIDIA drivers)
- **NVIDIA GPU** (one or more, e.g., L40, RTX A5000, RTX 5090)
- 16+ GB RAM (more recommended)
- 50+ GB free disk space
- Internet access to pull Docker images

### For development / testing (without GPU)
- A machine with Docker and a local Kubernetes cluster such as Minikube
- 8+ GB RAM, 30 GB disk

### Common requirements (all setups)
- [kubectl](https://kubernetes.io/docs/tasks/tools/)
- [Helm](https://helm.sh/docs/intro/install/)
- [Docker](https://docs.docker.com/engine/install/) (or containerd)
- [Minikube](https://minikube.sigs.k8s.io/docs/start/) (if you need a local cluster)

---

## Quick installation

Clone the repository:

```bash
git clone https://github.com/mlmanage/mlmanage-compute.git
cd mlmanage-compute
```

The recommended installation uses the provided scripts:

1. On a GPU host, run `./gpu-setup.sh` to install or update NVIDIA drivers. The script reboots the host.
2. After reboot, run `./other-setup.sh` to install the container and Kubernetes tooling.
3. Run the deployment script from its directory because it resolves manifests through relative paths:

   ```bash
   cd devops-backend/scripts
   ./deploy-all.sh
   ```

Review each script before running it on a managed host because it installs system packages and may update drivers or restart services.

### Option A: Full installation (on a machine with NVIDIA GPU)

1. **Install NVIDIA drivers and container toolkit**  
   Follow the official guide: [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)  
   After installation, verify:

   ```bash
   nvidia-smi
   docker run --rm --gpus all nvidia/cuda:11.0-base nvidia-smi
   ```

2. **Start Minikube with GPU support** (or use any existing Kubernetes cluster)

   ```bash
   minikube start --driver=docker --cpus=4 --memory=8192 --gpus=all
   ```

3. **Run the deploy script**

   ```bash
   cd devops-backend/scripts
   ./deploy-all.sh
   ```

### Option B: Installation on a GPU-less development machine

The scripts **auto-detect the absence of a GPU** (`nvidia-smi`) and skip every GPU step — NVIDIA Container Toolkit, `--gpus=all`, the GPU Operator, the VRAM webhook, MIG and time-slicing. Just run them normally:

```bash
./other-setup.sh          # installs Docker + minikube/kubectl/helm; skips GPU bits on a GPU-less host
cd devops-backend/scripts
./deploy-all.sh           # deploys API/DB/controller/CRDs; skips GPU Operator/webhook (CPU-only)
```

You can force the mode with `HAS_GPU=0` (CPU-only) or `HAS_GPU=1` (GPU).

> **Docker permissions:** `other-setup.sh` adds you to the `docker` group, but your **current shell** won't pick that up until you refresh it. If `deploy-all.sh`/`docker` complains about needing root, run `newgrp docker` (or log out and back in) first, then re-run. Verify with `docker ps` (should work without `sudo`).

> The script creates namespaces, CRDs, the controller, API, PostgreSQL, Prometheus stack, and optional GPU components. Wait until all pods are `Running` (use `kubectl get pods -n devops-system -w`).

### Option C: Preview GPU inventory WITHOUT a real GPU

Simulation publishes NFD-style labels so `/gpu/list`, `/gpu/capabilities`, and the frontend resource inventory can display representative cards. It deliberately does **not** patch node capacity or allocatable resources: only a real Kubernetes device plugin can make GPU workloads schedulable.

```bash
cd devops-backend/scripts
HAS_GPU=1 GPU_SIM=1 ./deploy-all.sh
# or update labels only:
GPU_COUNT=2 GPU_PRODUCT=NVIDIA-A100 ./simulate-gpu.sh
./simulate-gpu.sh --teardown
```

Simulated entries are returned with `"simulated": true`, cannot be partitioned, and are excluded from frontend workload/reservation selectors. Use `LOCAL_DEV_MODE=true` for synthetic end-to-end workload contracts, or install a real GPU device plugin for scheduling tests.

For real GPUs the webhook defaults to `failurePolicy: Fail`. An operator may explicitly set `VRAM_WEBHOOK_FAILURE_POLICY=Ignore` when availability is preferred over guaranteed VRAM mutation.

---

## First use: bootstrap administrator

When the database has no users, the API can bootstrap an administrator and create the corresponding namespace and workspace volumes. Configure the credentials before the first API startup; do not use the built-in development values outside an isolated environment.

```bash
curl -X POST http://localhost:8000/login \
  -H "Content-Type: application/json" \
  -d '{"username":"<bootstrap-username>","password":"<bootstrap-password>"}'
```

Save the returned `access_token` for subsequent calls.

Set `BOOTSTRAP_ADMIN_USERNAME` and `BOOTSTRAP_ADMIN_PASSWORD` through secret management, or disable automatic bootstrap with `BOOTSTRAP_ADMIN=false`. Bootstrap runs only while the `users` table is empty and does not overwrite existing users. Passwords are stored as salted PBKDF2-HMAC-SHA256 hashes.

---

## Using the API

All endpoints are available at `http://localhost:8000` (after port‑forwarding).  
To expose the API:

```bash
kubectl port-forward -n devops-system svc/devops-api 8000:8000 &
```

### Endpoint summary

| Method | Endpoint | Description | Required role |
|--------|----------|-------------|---------------|
| GET | `/health` | Health check (returns `{"status":"ok"}`) | – |
| POST | `/login` (JSON body) | Obtain JWT | – |
| GET | `/me` | Get the current authenticated user | any authenticated |
| GET | `/users` | List all users | admin |
| POST | `/users` | Create a new user (also creates namespace + workspace PVCs) | admin |
| PUT | `/users/{username}` | Update user profile (email, slack, team, priority) | admin |
| DELETE | `/users/{username}` | Delete a user account (job records retained; cannot delete self or the last admin) | admin |
| POST / GET | `/groups` | Create/list groups with a total GPU quota (`members` sets `users.team`) | admin (GET: any) |
| PUT | `/groups/{name}` | Update a group's quota/membership | admin |
| DELETE | `/groups/{name}` | Delete a group (memberships cleared; users/jobs/storage kept) | admin |
| POST / GET | `/projects` | Create/list projects with a total GPU quota | admin/poweruser (GET: any) |
| PUT | `/projects/{name}` | Update a project (owner, quotas) | admin/poweruser |
| DELETE | `/projects/{name}` | Delete a project (historical jobs keep the name) | admin/poweruser |
| GET | `/gpu/list` | List available GPU cards | any |
| GET | `/gpu/capabilities` | Detected GPUs + supported partition modes (full/timeslice/mps/mig) | user, poweruser, admin |
| GET | `/gpu/partitions` | Current partition state of each GPU | user, poweruser, admin |
| POST | `/gpu/{uuid}/partition` | Set/change a GPU's partitioning (time-slice/MPS/MIG) | admin |
| POST | `/reservations` | Reserve a GPU time slot (supports `priority`; preempts lower-priority) | user, poweruser, admin |
| GET | `/reservations` | List your reservations (all if admin) | user, poweruser, admin |
| PUT | `/reservations/{id}/renew` | Extend a reservation's end time | user, poweruser, admin |
| DELETE | `/reservations/{id}` | Cancel a reservation (also stops its job/task) | user, poweruser, admin |
| GET | `/availability` | Per-GPU availability in a time window (`?start_time=&end_time=`) for reservation planning | user, poweruser, admin |
| GET | `/reservations/calendar` | Get all active reservations | any |
| GET | `/reservations/calendar.ics` | iCal feed (subscribe e.g. in Google Calendar; `?token=` to scope) | – |
| POST | `/reservations/import.ics` | Import reservations from an iCal file | user, poweruser, admin |
| POST | `/images` | Upload a custom image tarball (`docker save`) to the cluster registry | user, poweruser, admin |
| GET | `/images` | List your uploaded images (all if admin) | user, poweruser, admin |
| DELETE | `/images/{id}` | Delete an uploaded image record | user, poweruser, admin |
| POST | `/tasks` | Submit a training job (supports `vram_limit_gb`, `project`; `image` may be an uploaded image) | user, poweruser, admin |
| GET | `/tasks` | List your tasks (all if admin) | user, poweruser, admin |
| DELETE | `/tasks/{task_name}` | Delete/cancel a task | user, poweruser, admin |
| GET | `/tasks/{task_name}/logs` | Get (or stream with `?follow=true`) a task pod's logs | user, poweruser, admin |
| WS | `/tasks/{task_name}/exec` | Interactive shell into the task pod (WebSocket; `?token=&command=`) | user, poweruser, admin |
| GET | `/tasks/{task_name}/results` | List a task's result files | user, poweruser, admin |
| GET | `/tasks/{task_name}/results/download` | Download a result file (`?path=`) or whole dir as `.tar` | user, poweruser, admin |
| DELETE | `/tasks/{task_name}/results` | Delete a task's results | user, poweruser, admin |
| POST | `/jobs` | Schedule a job (reservation + auto-dispatched task) | user, poweruser, admin |
| GET | `/jobs` | List your jobs (all if admin) | user, poweruser, admin |
| DELETE | `/jobs/{id}` | Cancel a scheduled job | user, poweruser, admin |
| POST | `/queue` | Submit a job to the queue (priority, deps, multi-GPU/node, gang/MPI) | user, poweruser, admin |
| GET | `/queue` | View the queue (all if admin) | user, poweruser, admin |
| DELETE | `/queue/{id}` | Cancel a queued job | user, poweruser, admin |
| POST | `/teams/{team}/shared-storage` | Create a shared (RWX) team volume | admin, poweruser |
| GET | `/disk/usage` | Real-time disk/PVC usage for your namespaces | any authenticated |
| GET | `/analytics/usage` | Efficiency + energy/CO₂ analysis (`?group_by=user\|team\|project`, admin `?subject=`) | any authenticated |
| GET | `/analytics/activity` | Per-workload activity feed (jobs) for the usage view (`?group_by=`, `?hours=`, admin `?subject=`) | any authenticated |
| GET | `/gpu/usage` | Your GPU usage history | any authenticated |
| GET | `/gpu/usage/{username}` | GPU usage history for a user | admin |
| GET | `/settings/cleanup` | Get retention settings (tasks + results) | admin |
| PUT | `/settings/cleanup` | Update retention settings at runtime | admin |
| GET | `/audit-log` | Audit trail: who did what (`?hours=&actor=&action=&outcome=&target=&search=&limit=&offset=`) | admin |
| GET | `/metrics` | Prometheus metrics | – |

### Examples (replace `$TOKEN` with actual JWT)

**Create a normal user**

```bash
curl -X POST http://localhost:8000/users \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"username":"alice", "password":"<new-user-password>", "role":"user"}'
```

**List GPU devices**

```bash
curl http://localhost:8000/gpu/list
```

**Reserve a GPU for two hours** (UTC time)

```bash
curl -X POST http://localhost:8000/reservations \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"gpu_uuid":"gpu-1", "start_time":"<start-time>", "end_time":"<end-time>"}'
```

**Submit a simple CPU job**

```bash
curl -X POST http://localhost:8000/tasks \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"image":"alpine:latest", "command":["echo","Hello world"], "time_limit_seconds":60}'
```

**Submit a GPU job** (requires actual GPU in the cluster)

```bash
curl -X POST http://localhost:8000/tasks \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "image":"nvidia/cuda:11.0-base",
    "command":["nvidia-smi"],
    "resources":{"limits":{"nvidia.com/gpu":1}},
    "time_limit_seconds":120
  }'
```

**Submit a GPU job with a custom VRAM limit** (per-task, not hardcoded)

```bash
curl -X POST http://localhost:8000/tasks \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "image":"nvidia/cuda:11.0-base",
    "command":["nvidia-smi"],
    "resources":{"limits":{"nvidia.com/gpu":1}},
    "time_limit_seconds":120,
    "vram_limit_gb":12,
    "project":"research"
  }'
```

The controller sets `VRAM_LIMIT_MB` on the pod from `vram_limit_gb` (here `12 → 12288`). If omitted, a default (4 GB) is used.

**Renew a reservation** (extend its end time; rejected on conflict)

```bash
curl -X PUT http://localhost:8000/reservations/1/renew \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"end_time":"<end-time>"}'
```

**Schedule a job** (creates a reservation and auto-dispatches the task at start time)

```bash
curl -X POST http://localhost:8000/jobs \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "gpu_uuid":"gpu-1",
    "start_time":"<start-time>",
    "end_time":"<end-time>",
    "image":"alpine:latest",
    "command":["echo","scheduled"]
  }'
```

**Check your GPU usage history**

```bash
curl -H "Authorization: Bearer $TOKEN" "http://localhost:8000/gpu/usage?hours=24"
```

**Check job logs**

```bash
kubectl logs -n user-<username> <pod-name>
```

---

## Using your own image (custom benchmark)

You can upload your own container image and run a task with it — no need to publish it to a public registry. The image is pushed to an in-cluster registry and pulled by the task pod.

**1. Save your image to a tarball**

```bash
docker build -t mybench:latest .
docker save mybench:latest -o mybench.tar
```

**2. Upload it via the API** (`name` and `tag` are query params; the file is multipart)

```bash
curl -X POST "http://localhost:8000/images?name=mybench&tag=latest" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@mybench.tar"
```

Response includes the `pull_ref` to use in tasks:

```json
{"message":"Image uploaded","image":{"name":"mybench","tag":"latest",
 "repository":"<user>/mybench","pull_ref":"localhost:5000/<user>/mybench:latest"}}
```

**3. List / delete your images**

```bash
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/images
curl -X DELETE -H "Authorization: Bearer $TOKEN" http://localhost:8000/images/<id>
```

**4. Run a task with your uploaded image** (use the `pull_ref` as `image`)

```bash
curl -X POST http://localhost:8000/tasks \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"image":"localhost:5000/<user>/mybench:latest","time_limit_seconds":300}'
```

> Image size limit defaults to 8 GB (`MAX_IMAGE_UPLOAD_MB`). Registry addresses depend on the selected deployment mode and are configured by the setup scripts.

---

## Connecting to a running task

**Get / stream logs**

```bash
# one-shot
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/tasks/<task-name>/logs
# live stream
curl -N -H "Authorization: Bearer $TOKEN" "http://localhost:8000/tasks/<task-name>/logs?follow=true"
```

**Interactive shell (WebSocket)**

Open an interactive shell inside the task pod. The JWT is passed as a query parameter; stdin is sent as text messages, stdout/stderr come back as text messages.

```python
import asyncio, websockets

async def main():
    uri = "ws://localhost:8000/tasks/<task-name>/exec?token=<JWT>&command=/bin/sh"
    async with websockets.connect(uri) as ws:
        await ws.send("nvidia-smi\n")
        while True:
            print(await ws.recv(), end="")

asyncio.run(main())
```

---

## Downloading task results

Tasks write their outputs to the directory given by the `RESULTS_DIR` environment variable (injected automatically), which is backed by the user's persistent `scratch` volume. Results therefore **survive pod deletion** and can be downloaded via the API.

In your task command, write outputs to `$RESULTS_DIR`:

```bash
curl -X POST http://localhost:8000/tasks \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"image":"localhost:5000/<user>/mybench:latest",
       "command":["sh","-c","./run.sh > $RESULTS_DIR/output.txt"],
       "time_limit_seconds":600}'
```

List, then download (single file or the whole directory as a `.tar`):

```bash
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/tasks/<task>/results
curl -H "Authorization: Bearer $TOKEN" "http://localhost:8000/tasks/<task>/results/download?path=output.txt" -o output.txt
curl -H "Authorization: Bearer $TOKEN" "http://localhost:8000/tasks/<task>/results/download" -o results.tar
```

Result data is auto-deleted after `result_ttl_seconds` (default **7 days**), tunable via `/settings/cleanup`.

---

## Job queue (priority, dependencies, multi-GPU / multi-node)

For HPC-style scheduling use the queue instead of submitting tasks directly. The scheduler orders jobs by priority + fair-share, supports dependencies, backfill, and parallel multi-GPU / multi-node (gang / MPI) jobs.

```bash
# 2-node job, 1 GPU per node, high priority, gang-scheduled
curl -X POST http://localhost:8000/queue \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"image":"localhost:5000/<user>/train:latest","command":["python","train.py"],
       "gpus":1,"replicas":2,"gang":true,"mpi":true,"priority":5}'

# a job that runs only after job 12 finishes
curl -X POST http://localhost:8000/queue \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"image":"alpine","command":["echo","post"],"depends_on":[12]}'

# view the queue
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/queue
```

Each replica pod receives `JOB_RANK`, `JOB_WORLD_SIZE`, `JOB_GROUP`, `JOB_MPI` env vars for coordination.

---

## Notifications, calendar & analytics

```bash
# subscribe to reservations in Google Calendar (or any iCal client)
http://<host>:8000/reservations/calendar.ics?token=<JWT>

# import reservations from an iCal file
curl -X POST -H "Authorization: Bearer $TOKEN" -F "file=@schedule.ics" \
  http://localhost:8000/reservations/import.ics

# efficiency + energy/CO2 analysis grouped by team
curl -H "Authorization: Bearer $TOKEN" "http://localhost:8000/analytics/usage?group_by=team&hours=24"

# real-time disk usage
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/disk/usage
```

E-mail/Slack reservation reminders are sent automatically when SMTP/Slack are configured (see environment variables). Set a user's contact + team with `PUT /users/{username}`.

---

## Task retention / cleanup

Tasks and their pods are cleaned up automatically:

- **Task pods** are deleted when their `time_limit_seconds` is exceeded (enforced by the controller).
- **Job tasks** are deleted when the job's reservation `end_time` passes (the job is marked `completed`).
- **Standalone tasks** (not tied to a job) are deleted after a TTL (default **1 day**).
- **Terminal/orphaned task pods** are swept automatically (self-healing).
- **Result data** (per-task directory on the scratch volume) is deleted after `result_ttl_seconds` (default **7 days**).

Admins can view and change all retention timings at runtime (no redeploy):

```bash
# view current settings
curl -H "Authorization: Bearer $ADMIN_TOKEN" http://localhost:8000/settings/cleanup
# -> {"cleanup_interval_seconds":60,"standalone_task_ttl_seconds":86400,"result_ttl_seconds":604800}

# change: standalone-task TTL to 2h, result TTL to 1h, reaper interval to 30s
curl -X PUT http://localhost:8000/settings/cleanup \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"standalone_task_ttl_seconds":7200,"result_ttl_seconds":3600,"cleanup_interval_seconds":30}'
```

---

## Monitoring with Grafana

Access Grafana dashboard:

```bash
kubectl port-forward -n monitoring svc/monitoring-grafana 3000:80 &
```

Login: `admin`  
Password: retrieve it with:

```bash
kubectl get secret -n monitoring monitoring-grafana -o jsonpath="{.data.admin-password}" | base64 -d ; echo
```

The Prometheus datasource is provisioned automatically by kube-prometheus-stack's Grafana
datasource sidecar (`name: Prometheus`, `uid: prometheus`, default), pointing at
`http://monitoring-kube-prometheus-prometheus.monitoring:9090` — the same endpoint the API uses
via `PROMETHEUS_URL`. Do not disable `grafana.sidecar.datasources.enabled` and do not re-declare
the datasource in `grafana.additionalDataSources`; either one leaves Grafana with no datasource
and every panel empty. See `Documentation/04-kubernetes/gpu-and-monitoring.md` for the check
commands.

Inside Grafana you will find:
- The **GPU Monitoring** dashboard (uid `mlmanage-gpu`) with four `timeseries` panels: GPU
  utilization, VRAM usage, temperature, power consumption — in the `GPU` dashboard folder fed
  from the `gpu-dashboard` ConfigMap
- Alert rules (e.g., high GPU temperature) — `devops-backend/monitoring/gpu-alerts.yaml`

The dashboard is provisioned from `devops-backend/monitoring/gpu-dashboard.json`: `deploy-all.sh`
puts it in the `gpu-dashboard` ConfigMap labelled `grafana_dashboard=1`, and Grafana's dashboards
sidecar loads it — so editing that JSON and re-running the deploy (or re-creating the ConfigMap) is
how you change it. The file must stay a **top-level dashboard object** (no `{"dashboard": ...}`
export wrapper), must not use the `graph` panel type (removed in Grafana 11), and pins each panel to
datasource uid `prometheus`.

> **Caveat:** GPU panels only show data when a DCGM exporter is actually scraped. Verify with
> `DCGM_FI_DEV_GPU_UTIL` in Prometheus; if it returns no series, the GPU Operator's
> `nvidia-dcgm-exporter` is not running and GPU panels/alerts stay empty regardless of Grafana.

---

## Advanced Configuration

### Per-GPU-type quotas (count + VRAM)

Quotas are defined **per GPU model**, because 1×H100 ≠ 1×GTX‑1050Ti (and 40 GB VRAM means something very different on each). Set per-type maps on a user (and `gpu_quota_by_type` on groups/projects); a `"default"` key covers any unlisted type, and the old scalar quotas remain the fallback.

```bash
# alice: at most 1 H100 (8 of anything else); a task may use ≤80 GB VRAM on H100, ≤16 GB elsewhere
curl -X PUT http://localhost:8000/users/alice \
  -H "Authorization: Bearer $ADMIN" -H "Content-Type: application/json" \
  -d '{"gpu_quota_by_type":{"NVIDIA-H100":1,"default":8},
       "vram_quota_by_type":{"NVIDIA-H100":80,"default":16}}'

# group/project total GPU quota per type
curl -X POST http://localhost:8000/groups -H "Authorization: Bearer $ADMIN" \
  -H "Content-Type: application/json" \
  -d '{"name":"research","gpu_quota_by_type":{"NVIDIA-H100":4,"NVIDIA-A5000":16}}'
```

A task picks its type via `gpu_type` (or it's auto-detected from `gpu_uuid`). The GPU-count and VRAM quotas are **independent** — a VRAM-heavy task and a GPU-count task are checked separately, so both can run at once. Exceeding a per-type limit returns `403` naming the type.

### Disk quotas and workspaces

When a user is created via `POST /users`, three dedicated workspace PVCs are created automatically in their namespace:

| PVC | Default size | Env var |
|-----|--------------|---------|
| `<user>-home` | 10 Gi | `DEFAULT_DISK_HOME_GB` |
| `<user>-scratch` | 20 Gi | `DEFAULT_DISK_SCRATCH_GB` |
| `<user>-project` | 10 Gi | `DEFAULT_DISK_PROJECT_GB` |

Each PVC is labeled `devops.dev/volume-type=<home|scratch|project>` so the disk‑quota controller can classify usage. The namespace is labeled `devops.dev/user-namespace=true` so the controller discovers it.

Soft quotas (alerts at 80% usage) are enabled by default. Hard quotas are the PVC sizes above; change the defaults via the env vars on the API deployment, or resize an individual PVC.

To record a user’s hard disk limit in the database:

```sql
UPDATE users SET disk_home_gb = 50 WHERE username = 'alice';
```

### GPU partitioning (time‑slicing / MPS / MIG)

The API can partition each GPU **individually and at runtime**, validated against what the card actually supports — it never fakes an unsupported mode.

**Every GPU starts undivided** (`full`, 1 share): a fresh deploy publishes each card as one whole device, and a card stays that way until someone divides *that* card. Dividing one card never changes another — not even a sibling on the same node.

```bash
# what can each GPU do?
curl -H "Authorization: Bearer $ADMIN" http://localhost:8000/gpu/capabilities

# split a specific GPU into 8 time-sliced shares
curl -X POST -H "Authorization: Bearer $ADMIN" -H "Content-Type: application/json" \
  -d '{"mode":"timeslice","replicas":8}' http://localhost:8000/gpu/<uuid>/partition

# real VRAM slicing via MIG (only on MIG-capable GPUs, e.g. A100/H100)
curl -X POST -H "Authorization: Bearer $ADMIN" -H "Content-Type: application/json" \
  -d '{"mode":"mig","mig_profiles":{"1g.5gb":4}}' http://localhost:8000/gpu/<uuid>/partition

# full bare-metal passthrough
curl -X POST -H "Authorization: Bearer $ADMIN" -H "Content-Type: application/json" \
  -d '{"mode":"full"}' http://localhost:8000/gpu/<uuid>/partition
```

**What each mode means and where it works (hardware truth, not hidden):**

| Mode | Isolation | VRAM partition | Works on |
|------|-----------|----------------|----------|
| `full` | whole GPU, bare‑metal (real CUDA/NVLink) | n/a | every GPU |
| `timeslice` | soft (time‑shared, no memory isolation) | no | every GPU; **per‑card** (dividing one card leaves the others whole) |
| `mps` | soft (concurrent, best‑effort caps) | partial | every GPU; per‑card |
| `mig` | **hardware‑isolated slices** | **yes** (e.g. 4×5GB) | **only MIG‑capable**: A100 / A30 / H100 / H200. **Not** A1000/RTX/T4/V100 |

Notes:
- Requesting `mig` on a non‑MIG card returns **HTTP 400** with the list of supported modes — by design.
- `timeslice`/`mps` need **`replicas` ≥ 2**: one share is an undivided card, which is `full`. A request for 1 share returns **HTTP 400** instead of recording a sharing mode for a whole card.
- **Different splits on different GPUs of the same node are supported** (GPU0 into 4, GPU1 into 8, GPU2 whole): the device‑plugin profile addresses specific device indices. What a single node *cannot* do is mix `timeslice` and `mps` — the plugin takes one mechanism per node, and the API says so with **HTTP 409** rather than silently rejoining cards.
- Containers always get **bare‑metal GPU access** via the NVIDIA container runtime — no virtualization layer.

`gpu-operator/time-slicing-config.yaml` ships one profile, `default`, with **no sharing at all** — that is the undivided starting state applied to every GPU node at deploy. Calling the partition endpoint adds a `mlm-<node>` profile next to it (built from all partition records of that node) and points the node at it with `nvidia.com/device-plugin.config=mlm-<node>`; re‑running the deploy leaves those profiles and labels alone, so divided cards stay divided.

#### Reserving and using a specific slice

Once a GPU is MIG‑partitioned, users can **reserve an individual slice** and **run tasks bound to it**:

```bash
# reserve one 1g.5gb slice of a specific GPU (returns the assigned slice_index)
curl -X POST http://localhost:8000/reservations \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"gpu_uuid":"<uuid>","start_time":"...","end_time":"...","gpu_partition":"1g.5gb"}'

# run a task pinned to that slice profile (pod requests nvidia.com/mig-1g.5gb)
curl -X POST http://localhost:8000/tasks \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"image":"...","command":["python","train.py"],"gpu_partition":"1g.5gb"}'
```

- Slice reservations are **capacity‑limited**: only as many concurrent reservations as the GPU has instances of that profile; the next one gets `409`.
- A whole‑GPU reservation conflicts with any slice reservation on that GPU (and vice‑versa).
- Reserving a slice of a GPU that isn't partitioned into that profile → `400`.
- **Concurrent mixed use works**: e.g. reserve a `1g.5gb` slice of GPU‑A *and* time‑slice/whole GPU‑B at the same time.
- A task with `gpu_partition` makes its pod request `nvidia.com/mig-<profile>` instead of `nvidia.com/gpu`, so Kubernetes schedules it onto a matching MIG device.

### Dummy vs professional environment

`other-setup.sh` asks whether this is a **dummy** (local demo) or **professional** (real server) install — or set `MLM_ENV=dummy|professional` to skip the prompt. **Both are fully automatic** — pick one and the cluster + registry are set up for you:

- **dummy** → installs & starts **minikube** (single‑node) + the registry addon. Good for demos/dev. *Not* suitable for production: single‑node (no multi‑node/MPI), and MIG reconfiguration is unreliable inside the docker‑driver node.
- **professional** → if no cluster is reachable, installs **k3s** and deploys an **in‑cluster image registry** (`registry:2`, NodePort `localhost:32000`); otherwise it uses the configured cluster. On k3s it also auto‑configures containerd to trust `localhost:32000` as an insecure (HTTP) registry, so every node can pull. This is fully automatic — the analogue of minikube's `registry` addon in dummy mode.

  Because control‑plane images must be available to every node, `deploy-all.sh` builds and pushes versioned API/controller images to a registry. **If you don't set `IMAGE_REGISTRY`, it defaults to the in‑cluster registry `localhost:32000`** — so a fresh professional install needs *no* registry configuration. To publish to your own registry instead, set `IMAGE_REGISTRY=…` (or set `BUILD_IMAGES=0` with explicit pre‑published `API_IMAGE`/`CONTROLLER_IMAGE`). The user‑image registry (`REGISTRY_PUSH_HOST`/`REGISTRY_PULL_PREFIX`) is configured separately.

Zero‑config professional install (uses the in‑cluster registry automatically):

```bash
MLM_ENV=professional ./deploy-all.sh
```

Publishing to an external registry instead:

```bash
MLM_ENV=professional IMAGE_REGISTRY=registry.example.com/mlmanage IMAGE_TAG=v1 ./deploy-all.sh
```

Example pre-published images:

```bash
MLM_ENV=professional BUILD_IMAGES=0 \
  API_IMAGE=registry.example.com/mlmanage/devops-api:v1 \
  CONTROLLER_IMAGE=registry.example.com/mlmanage/devops-controller:v1 \
  ./deploy-all.sh
```

Deployment fails early instead of applying manifests that reference unavailable local images.

### Customising the API

The API source is `devops-backend/api/main.py`. After editing, run `devops-backend/scripts/deploy-all.sh` from its own directory to rebuild the ConfigMap and restart the deployment.

### Environment variables (API deployment)

The API reads the following environment variables. Configure them in `devops-backend/api/api-deployment.yaml` or through the deployment environment:

| Variable | Default | Purpose |
|----------|---------|---------|
| `DATABASE_URL` | Development value supplied by the manifest | PostgreSQL connection string; source credentials from a Kubernetes Secret |
| `SECRET_KEY` | Insecure development value | JWT signing key; supply a unique value through a Kubernetes Secret |
| `CORS_ORIGINS` | `*` | Comma‑separated allowed origins (for the frontend) |
| `DEFAULT_VRAM_LIMIT_GB` | `4` | VRAM limit applied to tasks that don't set `vram_limit_gb` |
| `DEFAULT_DISK_HOME_GB` / `DEFAULT_DISK_SCRATCH_GB` / `DEFAULT_DISK_PROJECT_GB` | `10` / `20` / `10` | Sizes of the auto‑created workspace PVCs |
| `WORKSPACE_STORAGE_CLASS` | Unset (cluster default) | Optional StorageClass for workspace PVCs |
| `PROMETHEUS_URL` | `http://monitoring-kube-prometheus-prometheus.monitoring:9090` | Prometheus endpoint the GPU‑usage collector scrapes |
| `GPU_METRICS_INTERVAL` | `60` | Seconds between GPU‑metric collection runs |
| `GPU_METRICS_ENABLED` | `true` | Toggle the background GPU‑usage collector |
| `REGISTRY_PUSH_HOST` | `registry.kube-system.svc.cluster.local:80` | Registry address the API pushes uploaded images to |
| `REGISTRY_PULL_PREFIX` | `localhost:5000` | Prefix used in `pull_ref` for task pods (node registry proxy) |
| `CRANE_PATH` | `crane` | Path to the `crane` binary used to push image tarballs |
| `MAX_IMAGE_UPLOAD_MB` | `8192` | Max uploaded image tarball size (MB) |
| `CLEANUP_ENABLED` | `true` | Toggle the background cleanup reaper |
| `CLEANUP_INTERVAL` | `60` | Default reaper interval (seconds); runtime‑tunable via `/settings/cleanup` |
| `STANDALONE_TASK_TTL_SECONDS` | `86400` | Default TTL for tasks not tied to a job; runtime‑tunable |
| `RESULT_TTL_SECONDS` | `604800` | Default TTL for task result data (7 days); runtime‑tunable |
| `BOOTSTRAP_ADMIN` | `true` | Auto‑create the first admin (+ namespace/workspaces) when the DB has no users |
| `BOOTSTRAP_ADMIN_USERNAME` | Insecure development value | Username for the bootstrapped admin; override through a Secret |
| `BOOTSTRAP_ADMIN_PASSWORD` | Insecure development value | Password for the bootstrapped admin; override through a Secret |
| `RESULTS_DIR` | `/results` | Mount path where tasks write outputs (persisted on scratch PVC) |
| `RESULTS_PVC_SUFFIX` | `scratch` | Which workspace PVC holds results (`<user>-scratch`) |
| `RESULTS_READER_IMAGE` | `busybox:latest` | Image for the results-helper pod |
| `MAX_RESULT_DOWNLOAD_MB` | `1024` | Max single result download size (MB) |
| `QUEUE_ENABLED` / `QUEUE_INTERVAL` | `true` / `15` | Toggle + interval of the queue scheduler |
| `NOTIFY_ENABLED` / `NOTIFY_INTERVAL` / `NOTIFY_LEAD_SECONDS` | `true` / `60` / `600` | Reservation notification loop settings |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_FROM` | empty / `587` / … | E‑mail (SMTP) for notifications; no‑op if unset |
| `SLACK_WEBHOOK_URL` | empty | Slack webhook for notifications; no‑op if unset |
| `CO2_KG_PER_KWH` | `0.4` | Grid carbon intensity for CO₂ estimates |
| `IMAGE_CLEANUP_ENABLED` / `IMAGE_CLEANUP_INTERVAL` | `true` / `3600` | Toggle + interval of unused‑image cleanup |
| `IMAGE_MAX_VERSIONS_PER_REPO` | `5` | Keep newest N versions per image repo |
| `IMAGE_UNUSED_TTL_SECONDS` | `604800` | Delete images unused for this long (7 days) |

> **GPU usage collector:** a background thread periodically queries DCGM metrics from Prometheus, maps each GPU to the user running a task on its node, and writes per‑user rows into the `gpu_metrics` table (exposed via `/gpu/usage`). It only writes rows when a DCGM exporter is publishing metrics.

> **Cleanup reaper:** a background thread deletes expired job tasks and TTL‑expired standalone tasks. The TTL and interval defaults come from the env vars above but can be changed at runtime via `GET`/`PUT /settings/cleanup` without a redeploy.

---

## Uninstalling

To completely remove the system:

```bash
helm uninstall gpu-operator -n gpu-operator
helm uninstall monitoring -n monitoring
kubectl delete namespace devops-system gpu-operator monitoring
```

If you used Minikube, you can delete the cluster:

```bash
minikube delete
```

---

## Troubleshooting

### Pods in `CrashLoopBackOff` or `Error`

Check logs:

```bash
kubectl logs -n devops-system <pod-name>
```

Common issues:
- A required ConfigMap or Secret is missing.
- PostgreSQL is unavailable during API startup.
- Webhook certificate problems; regenerate them with `devops-backend/scripts/generate-webhook-certs.sh`.

### GPU not discovered

- Verify that `nvidia-smi` works on the host.
- Check that `kubectl get nodes -o json | jq '.items[].status.capacity'` shows `nvidia.com/gpu`.
- Ensure GPU Operator pods are running: `kubectl get pods -n gpu-operator`.

### API returns 404 for `/tasks`

- The namespace `user-<username>` must exist. It is created automatically when you add a user via `/users`. If you inserted the user manually, create the namespace:

  ```bash
  kubectl create namespace user-admin
  kubectl label namespace user-admin devops.dev/user=admin
  kubectl label namespace user-admin devops.dev/user-namespace=true
  ```

  > Users created through `POST /users` get a namespace and workspace PVCs automatically. Manual namespace creation is needed only for users inserted directly into the database.

### Cannot log in with admin user

- Confirm the user exists with `SELECT username, role FROM users`. Do not write a plaintext value into `hashed_password`; plaintext records are rejected. For a disposable environment, clear the users and restart with the intended `BOOTSTRAP_ADMIN_*` values. For production, use a dedicated password-reset flow.

### Webhook blocks pod creation

- Check the webhook logs:

  ```bash
  kubectl logs -n devops-system deployment/vram-webhook
  ```

- If you see TLS errors, regenerate certificates using the provided script.


