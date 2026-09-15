# System architecture

## High-level component map

```text
Client / Frontend / curl
        |
        | HTTP + JWT
        v
+--------------------------+        SQL         +------------------+
| FastAPI devops-api       |------------------->| PostgreSQL       |
| devops-backend/api/main.py|                    | devops database  |
+------------+-------------+                    +------------------+
             |
             | Kubernetes API: create Task CRs, list nodes, create namespaces
             v
+--------------------------+          watches/updates          +------------------+
| Kubernetes API Server    |<----------------------------------| Kopf Controller  |
| CRDs: Task/User/etc.     |---------------------------------->| task_controller  |
+------------+-------------+          creates/deletes Pods     +------------------+
             |
             | Pod CREATE admission
             v
+--------------------------+       mutates Pods        +-------------------------+
| Mutating webhook config  |-------------------------->| VRAM webhook service    |
| vram-limit-webhook       |                           | injects VRAM_LIMIT_MB   |
+------------+-------------+                           +-------------------------+
             |
             v
+--------------------------+             +-------------------------+
| user-<username> namespace|             | gpu-operator namespace  |
| workload task Pods       |             | NVIDIA GPU Operator     |
+--------------------------+             +-------------------------+

+--------------------------+             +-------------------------+
| monitoring namespace     |<------------| Prometheus/Grafana Helm |
| PrometheusRule alerts    |             | DCGM/GPU metrics stack  |
+--------------------------+             +-------------------------+

+--------------------------+
| Disk quota controller    |
| Polls PVCs, emits metric |
+--------------------------+
```

## Main components

### FastAPI API

Source: `devops-backend/api/main.py`

Responsibilities:

- Defines SQLAlchemy database tables.
- Creates tables at startup with `Base.metadata.create_all(bind=engine)`.
- Authenticates users with JWT.
- Checks application roles for selected endpoints.
- Creates user namespaces.
- Lists GPUs through Kubernetes node labels/capacity (or uses `LOCAL_DEV_GPUS` in dev mode).
- Creates reservation rows in PostgreSQL.
- Creates `Task` custom resources in user namespaces (or stores in PostgreSQL in local dev mode).
- Schedules jobs that combine reservations with automatic task dispatch.
- Runs background scheduler thread that dispatches scheduled jobs at their start time.
- Runs background cleanup reaper thread (deletes expired job tasks, TTL-expired standalone tasks, terminal/orphaned pods, and expired result data).
- Runs background queue scheduler (priority + fair-share, dependencies, backfill, multi-GPU/node gang/MPI dispatch).
- Runs background notification loop (e-mail/Slack reservation reminders) and image-cleanup loop.
- Serves downloadable task results from the scratch PVC via per-user helper pods.
- Lists, deletes, and tracks tasks.
- Accepts custom image uploads (tarball) and pushes them to the in-cluster registry via `crane`.
- Streams task pod logs and proxies an interactive shell into task pods (WebSocket exec).
- Exposes Prometheus metrics bytes at `/metrics`.
- Provides health check at `/health`.
- Supports CORS with configurable origins.
- Exposes admin runtime settings for cleanup TTLs (`/settings/cleanup`).

### PostgreSQL

Manifest: `devops-backend/api/postgres-deployment.yaml`

Responsibilities:

- Stores `users`, `reservations`, `gpu_metrics`, `tasks`, `jobs`, `images`, and `queued_jobs` tables.
- Uses a `10Gi` PVC named `postgres-pvc`.
- The manifest contains an insecure development database credential that must be replaced before production use.
- `tasks` table is used only in local dev mode for storing task state.
- `jobs` table stores scheduled jobs that combine reservations with automatic task dispatch.

### Task custom resource and controller

Sources:

- CRD: `devops-backend/crd/task-crd.yaml`
- Controller: `devops-backend/controllers/task_controller.py`

Responsibilities:

- API creates `Task` objects in `user-<username>` namespaces.
- Kopf controller watches `tasks.devops.local/v1`.
- On create, it creates a Kubernetes Pod from the task spec.
- On timer, it enforces `timeLimitSeconds` by deleting the Pod.
- On delete, it removes the associated Pod.

### GPU stack

Sources:

- `devops-backend/scripts/deploy-all.sh`
- `devops-backend/gpu-operator/time-slicing-config.yaml`
- `devops-backend/gpu-operator/webhook.py`

Responsibilities:

- Installs NVIDIA GPU Operator with Helm.
- Enables DCGM/DCGM exporter.
- Applies the device-plugin `default` profile: no sharing, so every GPU is published whole until the partition API divides a specific card.
- Adds a mutating webhook that injects `VRAM_LIMIT_MB=4096` into Pods.

### Monitoring stack

Sources:

- `devops-backend/monitoring/prometheus-values.yaml`
- `devops-backend/monitoring/gpu-alerts.yaml`
- `devops-backend/monitoring/gpu-dashboard.json`

Responsibilities:

- Installs kube-prometheus-stack with custom values.
- Configures Grafana Prometheus datasource.
- Adds GPU alert rules for temperature, power, and low free memory.

### Disk quota controller

Sources:

- `devops-backend/quotas/disk-quota-controller.py`
- `devops-backend/quotas/disk-quota-controller.yaml`

Responsibilities:

- Runs in `devops-system`.
- Polls user PVCs every 300 seconds.
- Exposes `user_disk_usage_percent{user,volume_type}` on port 9090.
- Logs and posts alerts when usage exceeds 80%.

Current caveat: the temporary measurement Pod does not mount the target PVC; see [`../00-overview/current-state-and-gaps.md`](../00-overview/current-state-and-gaps.md).
