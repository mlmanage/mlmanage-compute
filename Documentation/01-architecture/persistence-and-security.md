# Persistence and security

## Database

The API uses SQLAlchemy with the `DATABASE_URL` environment variable. Configure it with a credential sourced from a Kubernetes Secret rather than embedding a password in a manifest.

```text
postgresql://<user>:<password>@<host>:5432/<database>
```

Tables are created automatically at application startup by:

```python
Base.metadata.create_all(bind=engine)
```

### Tables

| Table | Model | Purpose |
|---|---|---|
| `users` | `UserDB` | User identity, application role, passwords, quotas, disk quota fields. |
| `gpu_metrics` | `GPUMetric` | Historical GPU utilization/memory/temp/power by user and GPU UUID. |
| `reservations` | `Reservation` | Active GPU reservation time ranges. |
| `tasks` | `TaskDB` | Task tracking in local dev mode (stores task state when not using Kubernetes CRs). |
| `jobs` | `JobDB` | Scheduled jobs that combine reservations with automatic task dispatch. |
| `images` | `ImageDB` | Custom images uploaded by users and pushed to the in-cluster registry. |
| `queued_jobs` | `QueuedJobDB` | Queue scheduler entries (priority, fair-share, dependencies, multi-GPU/node). |
| `audit_log` | `AuditLogDB` | Who did what: one record per state-changing request and per sign-in attempt, read by admins via `GET /audit-log`. |

## Authentication

Authentication uses FastAPI `HTTPBearer` and JWTs via PyJWT.

- Token subject: username in `sub`.
- Expiration: 24 hours from login.
- Algorithm: `HS256`.
- Secret: `SECRET_KEY` environment variable.

Password handling:

- `users.hashed_password` stores salted PBKDF2-HMAC-SHA256 values with a versioned string format.
- Bootstrap and API-created users are hashed before insertion.
- Plaintext database values are rejected; this project intentionally resets disposable development accounts rather than silently migrating them.
- Login audit logs contain username and boolean outcomes only, never submitted or stored password values.

## Application roles

The API uses a `role` string stored in PostgreSQL. `check_role([...])` enforces role lists on selected routes.

Known roles from code and CRDs:

- `admin` — full access.
- `poweruser` — create/manage own tasks, jobs, reservations, queue; team shared storage.
- `user` — create/manage own tasks, jobs, reservations, queue.
- `readonly` — **read-only**: may `GET` tasks/jobs/queue, but write actions (POST/DELETE/PUT) return 403.

All four roles are enforced by `check_role([...])` in the API (read endpoints include `readonly`; write endpoints do not).

### Quota enforcement

`POST /tasks` enforces the user's stored quotas (admins exempt):

- `vram_limit_gb` per task must not exceed `quota_vram_gb` (else 403).
- Active GPUs in use (sum across the user's running/pending task pods, including MIG slices) plus the requested GPU count must not exceed `quota_gpu` (else 403).

Quotas are **per GPU type** (model), because 1×H100 ≠ 1×GTX-1050Ti and 4GB VRAM on an H100 ≠ 4GB on a 1050Ti. Each task carries a `gpuType` (explicit `gpu_type`, else auto-detected from `gpu_uuid`); the controller stamps the pod with a `gpu-type` label so usage is counted per type.

Two **independent** quota dimensions are checked, each per type, on task creation:

- **GPU count** — how many GPU units of a given type.
- **VRAM** — max VRAM (GB) a single task may request on a given type.

A VRAM-only task and a GPU-count task are validated against their respective quotas independently (a user can run both concurrently).

Quotas are defined as JSON maps `{gpu_type: limit}` with a `"default"` key as fallback; if no map is set, the legacy scalar (`quota_gpu` / `quota_vram_gb` / `total_gpus`) is the default. Enforced at three scopes:

- **Per-user** — `users.gpu_quota_by_type` (count) and `users.vram_quota_by_type` (VRAM), fallback `quota_gpu` / `quota_vram_gb`. Set via `PUT /users/{username}`.
- **Per-group** — `groups.gpu_quota_by_type` (fallback `total_gpus`): sum of that type's GPUs across all `team` members. Managed via `POST/GET /groups`.
- **Per-project** — `projects.gpu_quota_by_type` (fallback `total_gpus`): sum of that type's GPUs on pods labeled with the project. Managed via `POST/GET /projects`.

Resolution order for a type's limit: `map[type]` → `map["default"]` → scalar fallback. A quota of `0` blocks all use of that type. Admins are exempt from all quota checks.

Example user quota:
```json
{"gpu_quota_by_type": {"NVIDIA-H100": 1, "default": 8},
 "vram_quota_by_type": {"NVIDIA-H100": 80, "default": 16}}
```
→ at most 1 H100 (but 8 of any other type), and a task may request up to 80 GB VRAM on an H100 but only 16 GB on other cards.

## Kubernetes RBAC

Kubernetes RBAC is configured separately from API application roles.

Important service accounts:

| ServiceAccount | Namespace | Purpose |
|---|---|---|
| `api-sa` | `devops-system` | API can create namespaces, create/list Task CRs, list nodes. |
| `devops-controller-sa` | `devops-system` | Kopf controller can manage Pods and Task status. |
| `disk-quota-sa` | `devops-system` | Referenced by quota controller Deployment, but matching Role/Binding should be verified. |

Cluster roles in `devops-backend/rbac/cluster-roles.yaml` define user-facing Kubernetes roles: `devops-admin`, `devops-poweruser`, `devops-user`, and `devops-readonly`.

## Secrets and certificate materials

Deployment manifests contain development credential defaults for PostgreSQL and JWT signing. Replace them with values supplied through Kubernetes Secrets before deploying outside an isolated development environment.

Webhook certificates are generated in a temporary directory and stored in a Kubernetes TLS Secret; the generation script removes local key material when it exits.

## Network and admission security

- Root legacy `manifests/networkpolicy.yaml` denies all ingress/egress and separately allows SSH ingress to Pods labeled `app=ubuntu`.
- Main backend manifests do not define a complete set of NetworkPolicies.
- The VRAM mutating webhook excludes the `devops-system` namespace and Pods with `app=vram-webhook`.
