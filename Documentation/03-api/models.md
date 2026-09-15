# API models

Source: `devops-backend/api/main.py`.

## SQLAlchemy database models

### `UserDB` -> `users`

| Column | Type | Notes |
|---|---|---|
| `id` | String primary key | UUID by default. Bootstrap example uses `'1'`. |
| `username` | String unique indexed | Login identity. |
| `role` | String | Application role. |
| `hashed_password` | String | Salted `pbkdf2_sha256$<iterations>$<salt>$<digest>` value; plaintext values are rejected. |
| `quota_cpu` | String | Example: `4`. |
| `quota_memory` | String | Example: `8Gi`. |
| `quota_gpu` | Integer | Number of GPUs. |
| `quota_vram_gb` | Integer | VRAM quota in GB. |
| `disk_home_gb` | Integer | Optional disk quota field. |
| `disk_scratch_gb` | Integer | Optional disk quota field. |
| `disk_project_gb` | Integer | Optional disk quota field. |
| `email` | String nullable | For e-mail notifications. |
| `slack_id` | String nullable | Optional Slack identifier. |
| `team` | String nullable | Team (analytics, shared storage). |
| `priority` | Integer | Base user priority (fair-share). |
| `gpu_quota_by_type` | Text (JSON) | Per-GPU-type count quota, e.g. `{"NVIDIA-H100":1,"default":8}`. |
| `vram_quota_by_type` | Text (JSON) | Per-GPU-type VRAM cap (GB), e.g. `{"NVIDIA-H100":80,"default":16}`. |

### `GPUMetric` -> `gpu_metrics`

| Column | Type | Notes |
|---|---|---|
| `id` | Integer primary key | Indexed. |
| `timestamp` | DateTime | Defaults to UTC now. |
| `user` | String indexed | Username or user identifier. |
| `gpu_uuid` | String | GPU identity. |
| `utilization` | Float | GPU utilization. |
| `memory_used` | Float | Memory used. |
| `temperature` | Float | GPU temperature. |
| `power` | Float | Power usage. |

### `Reservation` -> `reservations`

| Column | Type | Notes |
|---|---|---|
| `id` | Integer primary key | Indexed. |
| `user_id` | String indexed | Stores `current_user.id`, not username. |
| `gpu_uuid` | String indexed | GPU being reserved. |
| `start_time` | DateTime | Reservation start. |
| `end_time` | DateTime | Reservation end. |
| `status` | String | `active`, `cancelled`, `completed`, `preempted`. |
| `priority` | Integer | Reservation priority (higher preempts lower). |
| `gpu_partition` | String nullable | MIG profile for a slice reservation (e.g. `1g.5gb`); empty = whole GPU. |
| `slice_index` | Integer nullable | Which slice instance (0..N-1) was assigned. |
| `notified_start` | Boolean | Start-notification sent. |
| `notified_end` | Boolean | End-notification sent. |

### `TaskDB` -> `tasks`

| Column | Type | Notes |
|---|---|---|
| `id` | Integer primary key | Indexed. |
| `name` | String unique indexed | Task identifier. |
| `user` | String indexed | Username. |
| `image` | String | Container image. |
| `command` | Text | JSON-encoded command array. |
| `resources` | Text | JSON-encoded resources dict. |
| `gpu_uuid` | String nullable | GPU assignment. |
| `time_limit_seconds` | Integer nullable | Time limit. |
| `vram_limit_gb` | Integer nullable | Requested VRAM limit. |
| `project` | String nullable | Project used for quota/storage context. |
| `gpu_partition` | String nullable | Requested MIG profile. |
| `status` | String | Defaults to `submitted`. |
| `created_at` | DateTime | Creation timestamp. |

Used only in local dev mode to track tasks in PostgreSQL instead of Kubernetes CRs.

### `JobDB` -> `jobs`

| Column | Type | Notes |
|---|---|---|
| `id` | Integer primary key | Indexed. |
| `reservation_id` | Integer indexed | Associated reservation. |
| `task_name` | String nullable | Task name after dispatch. |
| `user_id` | String indexed | User ID. |
| `username` | String indexed | Username for task dispatch. |
| `gpu_uuid` | String indexed | GPU assignment. |
| `start_time` | DateTime | Job start time. |
| `end_time` | DateTime | Job end time. |
| `image` | String | Container image. |
| `command` | Text | JSON-encoded command array. |
| `resources` | Text | JSON-encoded resources dict. |
| `time_limit_seconds` | Integer | Calculated from time range. |
| `vram_limit_gb` | Integer nullable | Passed to the dispatched task. |
| `project` | String nullable | Passed to the dispatched task. |
| `gpu_partition` | String nullable | MIG profile reserved and passed to the task. |
| `priority` | Integer | Reservation/job priority. |
| `status` | String | `scheduled`, `submitted`, `cancelled`, `dispatch_failed`. |
| `created_at` | DateTime | Creation timestamp. |

Jobs combine reservations with automatic task submission at the scheduled start time.

### `ImageDB` -> `images`

| Column | Type | Notes |
|---|---|---|
| `id` | Integer primary key | Indexed. |
| `user` | String indexed | Owner username. |
| `name` | String indexed | User-supplied image name. |
| `tag` | String | Defaults to `latest`. |
| `repository` | String | Repo in the registry, e.g. `alice/benchmark`. |
| `pull_ref` | String | Reference for tasks, e.g. `localhost:5000/alice/benchmark:latest`. |
| `size_bytes` | Integer nullable | Uploaded tarball size. |
| `visibility` | String | Discovery scope: `user`, `group`, `project`, or `everyone`. |
| `project` | String nullable | Project target for project-visible records. |
| `group` | String nullable | Group target for group-visible records. |
| `created_at` | DateTime | Upload timestamp. |

Tracks images uploaded via `POST /images` and pushed to the in-cluster registry. Visibility determines which records appear in `GET /images`; it is not registry pull authorization.

### `QueuedJobDB` -> `queued_jobs`

| Column | Type | Notes |
|---|---|---|
| `id` | Integer primary key | Indexed. |
| `user` / `team` | String indexed | Owner and team. |
| `image` / `command` / `resources` | String / Text | Workload definition (command/resources JSON-encoded). |
| `gpu_uuid` | String nullable | Optional pinned GPU. |
| `gpus` | Integer | GPUs per pod (multi-GPU). |
| `replicas` | Integer | Pods / nodes (multi-node). |
| `gang` | Boolean | Gang scheduling (all-or-nothing). |
| `mpi` | Boolean | MPI-style coordination. |
| `vram_limit_gb` / `project` | Integer / String | Optional. |
| `priority` | Integer | Job priority. |
| `time_limit_seconds` | Integer nullable | Per-pod time limit. |
| `depends_on` | Text | JSON list of queue ids this depends on. |
| `status` | String | `queued`, `waiting_deps`, `running`, `completed`, `failed`, `cancelled`. |
| `task_names` | Text | JSON list of dispatched task names (per replica). |
| `created_at` / `started_at` / `finished_at` | DateTime | Lifecycle timestamps. |

Backs the queue scheduler (priority + fair-share, dependencies, backfill, multi-GPU/node gang/MPI).

### `GPUPartitionDB` -> `gpu_partitions`

| Column | Type | Notes |
|---|---|---|
| `id` | Integer primary key | Indexed. |
| `node` | String indexed | Node hosting the GPU. |
| `gpu_uuid` | String unique indexed | GPU identity. |
| `product` | String nullable | Card model (e.g. NVIDIA A100). |
| `mode` | String | `full` / `timeslice` / `mps` / `mig`. |
| `replicas` | Integer | Parts for timeslice/mps (or total MIG slices). |
| `mig_profiles` | Text nullable | JSON, e.g. `{"1g.5gb":4}` for MIG. |
| `applied` | Boolean | Whether applied on the cluster. |
| `updated_at` | DateTime | Last change. |

Desired per-GPU partition state, set via `POST /gpu/{uuid}/partition`.

### `GroupDB` -> `groups`

| Column | Type | Notes |
|---|---|---|
| `id` | Integer primary key | Indexed. |
| `name` | String unique indexed | Group name (matches users' `team`). |
| `total_gpus` | Integer | Total GPU quota (scalar fallback / `default`). |
| `gpu_quota_by_type` | Text (JSON) | Per-GPU-type count quota for the group. |
| `total_disk_gb` | Integer | Total disk quota (informational). |
| `created_at` | DateTime | Creation timestamp. |

### `ProjectDB` -> `projects`

| Column | Type | Notes |
|---|---|---|
| `id` | Integer primary key | Indexed. |
| `name` | String unique indexed | Project name (tasks reference it via `project`). |
| `owner` | String nullable | Project owner. |
| `total_gpus` | Integer | Total GPU quota (scalar fallback / `default`). |
| `gpu_quota_by_type` | Text (JSON) | Per-GPU-type count quota for the project. |
| `shared_storage_gb` | Integer | Shared storage size (informational). |
| `created_at` | DateTime | Creation timestamp. |

Group/project GPU quotas are enforced by `enforce_quota` on task creation. Managed via `POST/GET /groups` and `POST/GET /projects`.

### `AuditLogDB` -> `audit_log`

| Column | Type | Notes |
|---|---|---|
| `id` | Integer primary key | Indexed. |
| `at` | DateTime indexed | When it happened (UTC). |
| `actor` | String indexed | Username, or `anonymous` when the request carried no valid token. |
| `actor_role` | String nullable | Role at the time the record was written. |
| `action` | String indexed | Derived from the route template, e.g. `users.delete`, `auth.login`. |
| `target` | String indexed nullable | Path parameters of the request (the object acted on). |
| `method` | String | HTTP method. |
| `path` | String | Concrete request path, identifiers included. |
| `status_code` | Integer indexed | Response status. |
| `outcome` | String indexed | `success` / `denied` / `rejected` / `error`. |
| `detail` | Text (JSON) | Query string plus fields the endpoint supplied; secret-looking keys stored as `***`. |
| `client_ip` | String nullable | `X-Forwarded-For` first hop, else the socket peer. |
| `user_agent` | String nullable | Truncated to 255 characters. |

Written by the `audit_middleware` HTTP middleware for every state-changing request and every sign-in attempt; read by admins through `GET /audit-log`. Endpoints add their own fields via `audit_note(...)` instead of the middleware buffering request bodies. Purged by the cleanup reaper according to `AUDIT_TTL_SECONDS`.

## Pydantic request models

### `UserCreate`

```json
{
  "username": "alice",
  "password": "<new-user-password>",
  "role": "user",
  "quota_cpu": "2",
  "quota_memory": "4Gi",
  "quota_gpu": 1,
  "quota_vram_gb": 4
}
```

### `ReservationCreate`

```json
{
  "gpu_uuid": "gpu-1",
  "start_time": "<start-time>",
  "end_time": "<end-time>",
  "priority": 0,
  "gpu_partition": "1g.5gb"
}
```

`gpu_partition` is optional — when set, reserves one MIG slice of that profile (capacity-limited; returns the assigned `slice_index`). Omit for a whole-GPU reservation.

### `TaskCreate`

```json
{
  "image": "alpine:latest",
  "command": ["echo", "hello"],
  "resources": {"limits": {"cpu": "1", "memory": "1Gi"}},
  "time_limit_seconds": 60,
  "gpu_uuid": "optional-gpu-uuid",
  "vram_limit_gb": 12,
  "project": "research",
  "gpu_partition": "1g.5gb"
}
```

`vram_limit_gb` and `project` are optional. If `vram_limit_gb` is omitted, the API uses `DEFAULT_VRAM_LIMIT_GB` (default 4).

The API maps this into a Kubernetes `Task` CR (or a PostgreSQL row in local dev mode):

```json
{
  "apiVersion": "devops.local/v1",
  "kind": "Task",
  "metadata": {"name": "task-<random>"},
  "spec": {
    "user": "<current username>",
    "image": "...",
    "command": [],
    "resources": {},
    "timeLimitSeconds": 60,
    "gpuUUID": "...",
    "vramLimitGB": 12,
    "project": "research"
  }
}
```

### `JobCreate`

```json
{
  "gpu_uuid": "gpu-1",
  "start_time": "<start-time>",
  "end_time": "<end-time>",
  "image": "nvidia/cuda:11.0-base",
  "command": ["nvidia-smi"],
  "resources": {"limits": {"nvidia.com/gpu": 1}},
  "vram_limit_gb": 12,
  "project": "research",
  "gpu_partition": "1g.5gb",
  "priority": 5
}
```

A job atomically creates its reservation and job record, then dispatches a task at the start time. The `time_limit_seconds` is calculated from the time range. `vram_limit_gb`, `project`, and `gpu_partition` are forwarded to the dispatched task; `priority` and `gpu_partition` are also applied to the reservation.

### `ReservationRenew`

```json
{
  "end_time": "<end-time>"
}
```

Request body for `PUT /reservations/{id}/renew`. The new end time must be later than the current end and must not overlap another active reservation on the same GPU.

### `CleanupSettings`

```json
{
  "cleanup_interval_seconds": 30,
  "standalone_task_ttl_seconds": 7200,
  "result_ttl_seconds": 604800,
  "results_helper_idle_ttl_seconds": 1800
}
```

Request body for `PUT /settings/cleanup` (admin). Fields are optional and omitted values remain unchanged. Constraints: cleanup interval >= 5 seconds, standalone task TTL >= 1 second, and result/helper idle TTLs >= 60 seconds.

### Image upload (not a JSON body)

`POST /images` takes `name`, `tag`, `visibility`, `project`, and `group` as **query parameters** and the image as a **multipart file** field `file` (a `docker save` tarball) — it is not a JSON request body. `visibility` defaults to `user`; project/group values are required or inferred according to the selected scope.
