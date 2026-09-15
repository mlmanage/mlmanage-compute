# Environment variables

The API supports several environment variables for configuration.

## Database configuration

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | Development value is supplied by the deployment manifest | PostgreSQL connection string. Source credentials from a Kubernetes Secret. |

Example:

```bash
DATABASE_URL=postgresql://<user>:<password>@<host>:5432/<database>
```

## Authentication and security

| Variable | Default | Purpose |
|---|---|---|
| `SECRET_KEY` | Insecure development value | JWT signing key. Supply a unique value through secret management before deployment. |

## VRAM limits

| Variable | Default | Purpose |
|---|---|---|
| `DEFAULT_VRAM_LIMIT_GB` | `4` | VRAM limit (GB) applied to tasks that don't set `vram_limit_gb`. |

The API writes this into the Task CR's `vramLimitGB`. The controller converts it to `VRAM_LIMIT_MB` (GB × 1024) on the pod. The VRAM webhook only injects its own default (`DEFAULT_VRAM_LIMIT_MB`, default `4096`) when the pod has no `VRAM_LIMIT_MB` already.

## Dedicated workspaces

| Variable | Default | Purpose |
|---|---|---|
| `DEFAULT_DISK_HOME_GB` | `10` | Size of the `<user>-home` PVC. |
| `DEFAULT_DISK_SCRATCH_GB` | `20` | Size of the `<user>-scratch` PVC. |
| `DEFAULT_DISK_PROJECT_GB` | `10` | Size of the `<user>-project` PVC. |
| `WORKSPACE_STORAGE_CLASS` | *unset* → cluster default | StorageClass for the workspace PVCs. When unset the API omits `storageClassName`, so Kubernetes applies the cluster's **default** class (minikube: `standard`, k3s: `local-path`). Set it only when the cluster has no default class. |

On `POST /users`, the API creates one PVC per workspace in the user's namespace, labeled `devops.dev/volume-type=<home|scratch|project>`.

## GPU metrics collector

| Variable | Default | Purpose |
|---|---|---|
| `GPU_METRICS_ENABLED` | `true` | Toggle the background GPU-usage collector thread. |
| `GPU_METRICS_INTERVAL` | `60` | Seconds between collection runs. |
| `PROMETHEUS_URL` | `http://monitoring-kube-prometheus-prometheus.monitoring:9090` | Prometheus endpoint scraped for DCGM metrics. |

The collector queries DCGM metrics, maps each GPU to the user running a task on its node, and writes per-user rows into `gpu_metrics`.

## GPU partitioning

| Variable | Default | Purpose |
|---|---|---|
| `GPU_OPERATOR_NS` | `gpu-operator` | Namespace of the NVIDIA GPU Operator / device-plugin ConfigMap. |
| `TIME_SLICING_CONFIGMAP` | `time-slicing-config` | ConfigMap the API edits to set per-node time-slicing. |

The partition API (`/gpu/capabilities`, `/gpu/partitions`, `POST /gpu/{uuid}/partition`) detects each card's
capability and applies `full` / `timeslice` / `mps` / `mig`. MIG is offered only on MIG-capable hardware. See
[`../04-kubernetes/gpu-and-monitoring.md`](../04-kubernetes/gpu-and-monitoring.md).

## Environment type (dummy vs professional)

| Variable | Default | Purpose |
|---|---|---|
| `MLM_ENV` | prompt (`dummy`) | `dummy` = install/start minikube + registry addon (single-node demo). `professional` = auto-install k3s if no cluster reachable (else use existing) + auto-deploy in-cluster `registry:2` (NodePort 32000). Both fully automatic; `deploy-all.sh` honors it and sets the API's registry env for professional. |
| `HAS_GPU` | auto-detect | Whether the host has a real NVIDIA GPU. When `0`, setup skips NVIDIA tooling, GPU Operator, VRAM webhook, MIG, and time-slicing. |
| `GPU_SIM` | `0` (auto `1` only when GPU is forced without a driver) | Publish inventory-only simulated GPU labels. Simulation never patches capacity/allocatable and cannot run or partition GPU workloads. |
| `VRAM_WEBHOOK_FAILURE_POLICY` | `Fail` | Admission policy for real-GPU deployments. Set `Ignore` only when explicitly accepting workloads without VRAM mutation during webhook outages. |
| `BUILD_IMAGES` | `1` | Build control-plane images during deploy. Professional deployments may set `0` and provide pre-published image references. |
| `IMAGE_REGISTRY` | `localhost:32000` (in-cluster registry) when `MLM_ENV=professional` and `BUILD_IMAGES=1` | Registry the API/controller images are built and pushed to. Defaults to the in-cluster `registry:2` that `other-setup.sh` deploys (and which k3s is auto-configured to pull from over HTTP), so a professional install needs no registry configuration. Override to publish to an external registry. |
| `API_IMAGE` / `CONTROLLER_IMAGE` | local names in dummy mode | Explicit pre-published image references when `BUILD_IMAGES=0`, or optional overrides for registry builds. |
| `IMAGE_TAG` | `local` | Tag used for registry-built control-plane images. |
| `IMAGE_PULL_POLICY` | `IfNotPresent` dummy, `Always` registry build | Pull policy rendered into API/controller deployments. |
| `REGISTRY_PUSH_HOST` / `REGISTRY_PULL_PREFIX` | minikube defaults | Registry used for user-uploaded workload images; separate from the control-plane `IMAGE_REGISTRY`. |

`other-setup.sh` prompts for this if unset; `deploy-all.sh` reads it to decide whether to manage minikube.

## Custom image registry

| Variable | Default | Purpose |
|---|---|---|
| `REGISTRY_PUSH_HOST` | `registry.kube-system.svc.cluster.local:80` | Registry address the API pushes uploaded images to (in-cluster). |
| `REGISTRY_PULL_PREFIX` | `localhost:5000` | Prefix used in the returned `pull_ref` for task pods (node registry proxy). |
| `CRANE_PATH` | `crane` | Path to the `crane` binary used to push image tarballs. |
| `MAX_IMAGE_UPLOAD_MB` | `8192` | Max uploaded image tarball size in MB (HTTP 413 if exceeded). |

`POST /images` loads the uploaded tarball and pushes it to `REGISTRY_PUSH_HOST`; tasks pull via `REGISTRY_PULL_PREFIX/<user>/<name>:<tag>`. The registry is provided by the minikube `registry` addon (enabled in `other-setup.sh`).

## Task cleanup / retention

| Variable | Default | Purpose |
|---|---|---|
| `CLEANUP_ENABLED` | `true` | Toggle the background cleanup reaper thread. |
| `CLEANUP_INTERVAL` | `60` | Default reaper interval (seconds). |
| `STANDALONE_TASK_TTL_SECONDS` | `86400` | Default TTL for tasks not tied to a job (1 day). |
| `RESULTS_HELPER_IDLE_TTL_SECONDS` | `1800` | Delete idle Kubernetes result-helper pods after this many seconds (minimum runtime setting: 60). |

These env values are only the **defaults at boot**. They can be changed at runtime (no redeploy) via the admin API `GET`/`PUT /settings/cleanup`; the reaper reads the live values each iteration.

## Admin bootstrap

| Variable | Default | Purpose |
|---|---|---|
| `BOOTSTRAP_ADMIN` | `true` | Auto-create the first admin (+ namespace and workspace PVCs) when the `users` table is empty. |
| `BOOTSTRAP_ADMIN_USERNAME` | Insecure development value | Username for the bootstrapped admin; override through a Secret. |
| `BOOTSTRAP_ADMIN_PASSWORD` | Insecure development value | Password for the bootstrapped admin; override through a Secret. |

Runs on API startup, idempotent (only when no users exist). See [`bootstrap-admin.md`](bootstrap-admin.md).

## Task results

| Variable | Default | Purpose |
|---|---|---|
| `RESULTS_DIR` | `/results` | Mount path where tasks write outputs (persisted on the scratch PVC). |
| `RESULTS_PVC_SUFFIX` | `scratch` | Which workspace PVC holds results (`<user>-scratch`). |
| `RESULTS_READER_IMAGE` | `busybox:latest` | Image for the per-user results-helper pod. `deploy-all.sh` (professional) mirrors busybox into the in-cluster registry and points this at it, so nodes need no Docker Hub access. |
| `RESULTS_HELPER_BUDGET_SECONDS` | `40` | **Total** wall-clock budget for preparing the results-helper (deleting a stale pod + waiting for readiness). Must stay below the client's HTTP timeout (the test suite uses 60 s) so a stuck helper returns a diagnosable `503` instead of the client timing out. |
| `RESULTS_HELPER_READY_TRIES` | `20` | Readiness polls (2 s apart), additionally capped by the budget above. |
| `RESULTS_HELPER_EXEC_RETRIES` | `3` | Retries for a transient exec/stream failure. |
| `RESULTS_HELPER_EXEC_TIMEOUT` | `5` | Timeout (s) for a single exec stream read. |
| `MAX_RESULT_DOWNLOAD_MB` | `1024` | Max single result download size (MB). **Enforced** — exceeding it returns HTTP 413 instead of buffering the whole file in the API's memory. |
| `RESULT_TTL_SECONDS` | `604800` | TTL for result data (7 days); runtime-tunable via `/settings/cleanup`. |

## Job queue

| Variable | Default | Purpose |
|---|---|---|
| `QUEUE_ENABLED` | `true` | Toggle the queue scheduler thread. |
| `QUEUE_INTERVAL` | `15` | Seconds between scheduler runs. |

## Audit log

| Variable | Default | Purpose |
|---|---|---|
| `AUDIT_ENABLED` | `true` | Record who did what (`GET /audit-log`, admin-only). Off means nothing is recorded at all. |
| `AUDIT_LOG_READS` | `false` | Also record read-only requests (`GET`/`HEAD`/`OPTIONS`). Off by default: the console polls, so this multiplies the volume by two orders of magnitude. |
| `AUDIT_TTL_SECONDS` | `31536000` | Delete records older than this (365 days); `0` keeps them forever. Enforced by the cleanup reaper. |
| `AUDIT_DETAIL_MAX_CHARS` | `4000` | Cap on the stored `detail` JSON per record. |

## Notifications

| Variable | Default | Purpose |
|---|---|---|
| `NOTIFY_ENABLED` | `true` | Toggle reservation notification loop. |
| `NOTIFY_INTERVAL` | `60` | Seconds between notification checks. |
| `NOTIFY_LEAD_SECONDS` | `600` | Notify this long before start/end. |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_FROM` | empty / `587` / … | E-mail config; no-op if `SMTP_HOST` unset. |
| `SLACK_WEBHOOK_URL` | empty | Slack webhook; no-op if unset. |

## Analytics / energy

| Variable | Default | Purpose |
|---|---|---|
| `CO2_KG_PER_KWH` | `0.4` | Grid carbon intensity for CO₂ estimates in `/analytics/usage`. |

## Image cleanup

| Variable | Default | Purpose |
|---|---|---|
| `IMAGE_CLEANUP_ENABLED` | `true` | Toggle unused-image cleanup loop. |
| `IMAGE_CLEANUP_INTERVAL` | `3600` | Seconds between cleanup runs. |
| `IMAGE_MAX_VERSIONS_PER_REPO` | `5` | Keep newest N versions per repo. |
| `IMAGE_UNUSED_TTL_SECONDS` | `604800` | Delete images unused for this long (7 days). |

## Local development mode

| Variable | Default | Purpose |
|---|---|---|
| `LOCAL_DEV_MODE` | `false` | Enable local development mode without Kubernetes. |
| `LOCAL_DEV_GPUS` | None | JSON array of fake GPU objects for local testing. |

### `LOCAL_DEV_MODE`

When enabled, the API:

- Stores tasks in the SQL `tasks` table instead of creating Kubernetes CRs.
- Accepts image uploads and stores registry records without pushing to a registry.
- Returns deterministic synthetic task logs, exec echo responses, and result files/archives.
- Returns configured workspace sizes as synthetic disk records and acknowledges team-storage creation without provisioning a PVC.
- Uses the GPU list from `LOCAL_DEV_GPUS` if Kubernetes discovery fails.
- Does not require Kubernetes API access for these fallback paths.

These fallbacks validate frontend/API contracts and authorization only. They do **not** validate real image pushes, pod execution, persistent result storage, PVC provisioning, GPU partition application, or other Kubernetes/GPU behavior.

Accepted values: `1`, `true`, `yes` (case-insensitive).

Example:

```bash
LOCAL_DEV_MODE=true
```

### `LOCAL_DEV_GPUS`

JSON array of GPU objects returned by `/gpu/list` when Kubernetes GPU discovery fails and local dev mode is active.

Example:

```bash
LOCAL_DEV_GPUS='[{"uuid":"fake-gpu-0","product":"Fake GPU","node":"localhost"}]'
```

This is used as a fallback only when both conditions are met:
1. Kubernetes GPU listing throws an exception
2. `LOCAL_DEV_GPUS` is set and valid JSON

## CORS configuration

| Variable | Default | Purpose |
|---|---|---|
| `CORS_ORIGINS` | `*` | Comma-separated list of allowed CORS origins. |

Examples:

```bash
# Allow all origins (default)
CORS_ORIGINS=*

# Allow specific origins
CORS_ORIGINS=http://localhost:3000,https://frontend.example.com

# Allow multiple origins with spaces
CORS_ORIGINS="http://localhost:3000, https://app.example.com"
```

Empty origins are filtered out automatically.

## Deployment configuration

In the API deployment manifest (`devops-backend/api/api-deployment.yaml`), environment variables are set in the Pod spec:

```yaml
env:
- name: DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: api-secrets
      key: database-url
- name: SECRET_KEY
  valueFrom:
    secretKeyRef:
      name: api-secrets
      key: jwt-secret-key
```

To add local dev mode:

```yaml
env:
- name: LOCAL_DEV_MODE
  value: "true"
- name: LOCAL_DEV_GPUS
  value: '[{"uuid":"dev-gpu-0","product":"Development GPU","node":"local-development"}]'
- name: CORS_ORIGINS
  value: "http://localhost:3000"
```

## Security recommendations

1. **Never commit secrets to manifests.** Use Kubernetes Secrets and mount as environment variables:

```yaml
env:
- name: SECRET_KEY
  valueFrom:
    secretKeyRef:
      name: api-secrets
      key: jwt-secret-key
```

2. **Rotate `SECRET_KEY` regularly.** Changing it invalidates all existing JWTs.

3. **Use strong database passwords.** Do not use the manifest's development credential outside an isolated environment.

4. **Restrict CORS origins in production.** `*` allows all domains.

## Verifying configuration

After deployment, verify non-secret configuration without printing credentials:

```bash
kubectl exec -n devops-system deployment/devops-api -- env | grep -E 'LOCAL_DEV_MODE|CORS_ORIGINS'
kubectl get secret -n devops-system api-secrets
```

Check API logs for configuration warnings:

```bash
kubectl logs -n devops-system deployment/devops-api
```
