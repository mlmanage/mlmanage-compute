# API endpoints

Base URL after port-forward:

```text
http://localhost:8000
```

Port-forward:

```bash
kubectl port-forward -n devops-system svc/devops-api 8000:8000
```

## Endpoint summary

| Method | Path | Auth in code | Purpose |
|---|---|---|---|
| `GET` | `/health` | No | Health check endpoint. |
| `POST` | `/login` | No | Accept JSON credentials and return a JWT access token. |
| `GET` | `/me` | Required | Get current user information. |
| `GET` | `/users` | Admin | List all users. |
| `POST` | `/users` | Admin | Create user and attempt namespace creation. |
| `PUT` | `/users/{username}` | Admin | Update profile (email, slack_id, team, priority). |
| `DELETE` | `/users/{username}` | Admin | Delete a user (job records retained; cannot delete self or last admin). |
| `POST` | `/groups` | Admin | Create/upsert a group quota; optional `members` set `users.team`. |
| `GET` | `/groups` | `user`, `poweruser`, `admin`, `readonly` | List groups with quotas and members. |
| `PUT` | `/groups/{name}` | Admin | Update a group's quota and membership. |
| `DELETE` | `/groups/{name}` | Admin | Delete a group (memberships cleared; users/jobs/storage kept). |
| `POST` | `/projects` | `admin`, `poweruser` | Create/upsert a project quota. |
| `GET` | `/projects` | `user`, `poweruser`, `admin`, `readonly` | List projects with quotas. |
| `PUT` | `/projects/{name}` | `admin`, `poweruser` | Update a project (owner, quotas). |
| `DELETE` | `/projects/{name}` | `admin`, `poweruser` | Delete a project (historical jobs keep the name). |
| `GET` | `/gpu/list` | No | List GPUs from Kubernetes node labels/capacity. |
| `POST` | `/images` | `user`, `poweruser`, `admin` | Upload a custom image tarball to the cluster registry. |
| `GET` | `/images` | `user`, `poweruser`, `admin`, `readonly` | List image records visible to the caller. |
| `PUT` | `/images/{id}` | `user`, `poweruser`, `admin` | Update an owned image's discovery visibility (admin may update any). |
| `DELETE` | `/images/{id}` | `user`, `poweruser`, `admin` | Delete an uploaded image record. |
| `POST` | `/reservations` | `user`, `poweruser`, `admin` | Create reservation (supports `priority`; preempts lower priority). |
| `GET` | `/reservations` | Required | List caller's reservations (all if admin). |
| `PUT` | `/reservations/{id}/renew` | `user`, `poweruser`, `admin` | Extend a reservation's end time (conflict-checked). |
| `DELETE` | `/reservations/{id}` | `user`, `poweruser`, `admin` | Cancel a reservation (also stops linked job/task). |
| `GET` | `/availability` | `user`, `poweruser`, `admin`, `readonly` | Per-GPU availability in a window (`?start_time=&end_time=`). |
| `GET` | `/reservations/calendar` | No | List active reservations as calendar events. |
| `GET` | `/reservations/calendar.ics` | No (optional `?token=`) | iCal feed (Google Calendar subscription). |
| `POST` | `/reservations/import.ics` | `user`, `poweruser`, `admin` | Import reservations from an iCal file. |
| `POST` | `/tasks` | `user`, `poweruser`, `admin` | Create `Task` CR (supports `vram_limit_gb`, `project`). |
| `GET` | `/tasks` | `user`, `poweruser`, `admin`, `readonly` | List tasks for current user (or all if admin). |
| `DELETE` | `/tasks/{task_name}` | `user`, `poweruser`, `admin` | Delete/cancel a task. |
| `GET` | `/tasks/{task_name}/logs` | `user`, `poweruser`, `admin`, `readonly` | Get (or stream with `?follow=true`) task pod logs. |
| `WS` | `/tasks/{task_name}/exec` | via `?token=` | Interactive shell into the task pod (WebSocket). |
| `GET` | `/tasks/{task_name}/results` | `user`, `poweruser`, `admin`, `readonly` | List a task's result files. |
| `GET` | `/tasks/{task_name}/results/download` | `user`, `poweruser`, `admin`, `readonly` | Download a result file (`?path=`) or whole dir as `.tar`. |
| `DELETE` | `/tasks/{task_name}/results` | `user`, `poweruser`, `admin` | Delete a task's results. |
| `POST` | `/jobs` | `user`, `poweruser`, `admin` | Schedule a job and persist/dispatch `vram_limit_gb`, `project`, `gpu_partition`, and `priority`. |
| `GET` | `/jobs` | `user`, `poweruser`, `admin`, `readonly` | List jobs for current user (or all if admin). |
| `DELETE` | `/jobs/{job_id}` | `user`, `poweruser`, `admin` | Cancel a scheduled job. |
| `POST` | `/queue` | `user`, `poweruser`, `admin` | Submit a queued job (priority, deps, multi-GPU/node, gang/MPI). |
| `GET` | `/queue` | `user`, `poweruser`, `admin`, `readonly` | View the queue (all if admin). |
| `DELETE` | `/queue/{id}` | `user`, `poweruser`, `admin` | Cancel a queued job. |
| `POST` | `/teams/{team}/shared-storage` | `admin`, `poweruser` | Create a shared (RWX) team volume. |
| `GET` | `/disk/usage` | Required | Real-time disk/PVC usage. |
| `GET` | `/analytics/usage` | Required | Efficiency + energy/CO₂ analysis (`?group_by=`, admin `?subject=`). |
| `GET` | `/analytics/activity` | Required | Per-workload activity feed from jobs (`?group_by=`, `?hours=`, admin `?subject=`). |
| `GET` | `/gpu/usage` | Required | Caller's GPU usage history. |
| `GET` | `/gpu/usage/{username}` | Admin | Return recent GPU metric rows for a username. |
| `GET` | `/settings/cleanup` | Admin | Get retention settings (tasks + results). |
| `PUT` | `/settings/cleanup` | Admin | Update retention settings at runtime. |
| `GET` | `/audit-log` | Admin | Recorded actions: who did what, to what, with what result (`?hours=`, `?actor=`, `?action=`, `?outcome=`, `?target=`, `?search=`, `?limit=`, `?offset=`). |
| `GET` | `/metrics` | No | Return Prometheus metrics bytes. |

## Health check

```bash
curl http://localhost:8000/health
```

Response:

```json
{"status":"ok"}
```

## Login

```bash
curl -X POST http://localhost:8000/login \
  -H "Content-Type: application/json" \
  -d '{"username":"<username>","password":"<password>"}'
```

Response:

```json
{"access_token":"<jwt>"}
```

## Get current user

```bash
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/me
```

Response:

```json
{
  "id": "1",
  "username": "admin",
  "role": "admin",
  "quota_cpu": "4",
  "quota_memory": "8Gi",
  "quota_gpu": 4,
  "quota_vram_gb": 16,
  "disk_home_gb": null,
  "disk_scratch_gb": null,
  "disk_project_gb": null
}
```

## List users

```bash
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/users
```

Response: Array of user objects.

## Create user

```bash
curl -X POST http://localhost:8000/users \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"username":"alice","password":"<new-user-password>","role":"user"}'
```

Default quotas if omitted:

```json
{
  "quota_cpu": "2",
  "quota_memory": "4Gi",
  "quota_gpu": 1,
  "quota_vram_gb": 4
}
```

## List GPUs

```bash
curl http://localhost:8000/gpu/list
```

Response shape:

```json
{
  "gpus": [
    {"uuid":"gpu-node-0","product":"unknown","node":"gpu-worker-1","simulated":false,
     "shares":1,"sharing_strategy":null}
  ]
}
```

`shares` is how many schedulable units **this** card publishes and `sharing_strategy` how it is divided (`timeslice`/`mps`, `null` when whole). Both describe the card alone: an undivided card reports `shares: 1` even when a sibling on the same node is divided, and every card reports `1` until someone divides it. They come from that card's partition record, not from the node-wide `nvidia.com/gpu.replicas` / `nvidia.com/gpu.sharing-strategy` labels, which GPU Feature Discovery sets for the whole node (those are read only for nodes MLManage does not configure).

On errors, the endpoint returns an empty list plus an `error` string. Inventory-only labels created by `simulate-gpu.sh` return `simulated: true`; those entries are visible for UI/API development but are not schedulable or partitionable.

## Upload a custom image

Upload your own container image as a `docker save` tarball. The API streams it to disk and pushes it to the in-cluster registry using `crane`. `name`, `tag`, and optional discovery scope fields are query parameters; the tarball is sent as multipart form field `file`.

Allowed `visibility` values are `user` (default), `project`, `group`, and `everyone`. Project scope requires `project=<existing-project>` and project ownership unless the caller is an admin. Group scope accepts `group=<existing-group>`; when omitted it defaults to the caller's `team`, and non-admin callers must belong to that group. Invalid targets return HTTP 400 and unauthorized scope targets return HTTP 403.

```bash
docker save mybench:latest -o mybench.tar
curl -X POST "http://localhost:8000/images?name=mybench&tag=latest" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@mybench.tar"
```

Response:

```json
{
  "message": "Image uploaded",
  "image": {
    "id": 1, "user": "alice", "name": "mybench", "tag": "latest",
    "repository": "alice/mybench",
    "pull_ref": "localhost:5000/alice/mybench:latest",
    "size_bytes": 2242560, "visibility": "user",
    "project": null, "group": null, "created_at": "<created-at>"
  }
}
```

Use the returned `pull_ref` as the `image` field when submitting a task. Re-uploading the same `name`/`tag` overwrites the previous record. Max tarball size is `MAX_IMAGE_UPLOAD_MB` (default 8192 MB → HTTP 413 if exceeded). Image names must be lowercase alphanumeric with `-`, `_`, `.`.

## List, scope, and delete images

```bash
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/images
curl -X PUT http://localhost:8000/images/1 \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"visibility":"everyone"}'
curl -X DELETE -H "Authorization: Bearer $TOKEN" http://localhost:8000/images/1
```

`GET /images` returns `{"images": [...]}` filtered by `user`, `group`, `project`, or `everyone` discovery visibility. Group publication requires group membership; project publication requires project ownership (admins may target valid scopes). Visibility controls API discovery, not authorization to pull or submit an independently known registry reference. `DELETE` removes the database record; the blob remains in the registry.

## Create reservation

```bash
curl -X POST http://localhost:8000/reservations \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"gpu_uuid":"gpu-1","start_time":"<start-time>","end_time":"<end-time>"}'
```

Conflict response:

```text
HTTP 409 Time slot conflict
```

## List reservations

```bash
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/reservations
```

Returns the caller's reservations (or all reservations if the caller is an admin):

```json
{
  "reservations": [
    {"id": 1, "user": "alice", "gpu_uuid": "gpu-1",
     "start_time": "<start-time>", "end_time": "<end-time>",
     "status": "active"}
  ]
}
```

## Renew reservation

Extends an active reservation's end time. Only the owner (or an admin) may renew. The new end must be later than the current end, and the extended window must not overlap another active reservation on the same GPU.

```bash
curl -X PUT http://localhost:8000/reservations/1/renew \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"end_time":"<end-time>"}'
```

Response:

```json
{"message": "Reservation renewed", "id": 1, "end_time": "<end-time>"}
```

Error responses: `400` (new end not after current end), `403` (not your reservation), `404` (not found), `409` (overlaps another reservation).

## Cancel reservation

```bash
curl -X DELETE -H "Authorization: Bearer $TOKEN" http://localhost:8000/reservations/1
```

Sets the reservation status to `cancelled`; it then disappears from the calendar.

## Calendar

```bash
curl http://localhost:8000/reservations/calendar
curl "http://localhost:8000/reservations/calendar?gpu_uuid=gpu-1"
```

Response items:

```json
{
  "id": 1,
  "title": "User: alice",
  "user": "alice",
  "start": "<start-time>",
  "end": "<end-time>",
  "resourceId": "gpu-1",
  "status": "active"
}
```

## Submit task

CPU example:

```bash
curl -X POST http://localhost:8000/tasks \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"image":"alpine:latest","command":["echo","Hello world"],"time_limit_seconds":60}'
```

GPU example with a per-task VRAM limit and project:

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

The API converts `time_limit_seconds` to CRD field `timeLimitSeconds`, and `vram_limit_gb` to `vramLimitGB`. The controller then sets `VRAM_LIMIT_MB` on the pod from `vramLimitGB` (e.g. `12 → 12288`). If `vram_limit_gb` is omitted, the API uses `DEFAULT_VRAM_LIMIT_GB` (default 4). The optional `project` field is exposed to the pod as `PROJECT_NAME`.

## List tasks

```bash
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/tasks
```

Response:

```json
{
  "tasks": [
    {
      "name": "task-abc123",
      "user": "alice",
      "image": "alpine:latest",
      "command": ["echo", "hello"],
      "resources": {},
      "gpu_uuid": null,
      "time_limit_seconds": 60,
      "status": "Running",
      "created_at": "<created-at>"
    }
  ]
}
```

Admins see all tasks across all namespaces. Regular users see only their own tasks.

## Delete task

```bash
curl -X DELETE -H "Authorization: Bearer $TOKEN" http://localhost:8000/tasks/task-abc123
```

Response:

```json
{"message": "Task deleted", "task": "task-abc123"}
```

In local dev mode, this marks the task as `cancelled` instead of deleting the Kubernetes CR.

## Schedule job

Jobs combine reservations and tasks. A job creates a reservation and automatically submits a task at the start time.

```bash
curl -X POST http://localhost:8000/jobs \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "gpu_uuid": "gpu-1",
    "start_time": "<start-time>",
    "end_time": "<end-time>",
    "image": "nvidia/cuda:11.0-base",
    "command": ["nvidia-smi"],
    "resources": {"limits": {"nvidia.com/gpu": 1}}
  }'
```

Response:

```json
{
  "message": "Job scheduled",
  "job": {
    "id": 1,
    "reservation_id": 5,
    "task_name": "task-xyz789",
    "user": "alice",
    "gpu_uuid": "gpu-1",
    "start_time": "<start-time>",
    "end_time": "<end-time>",
    "image": "nvidia/cuda:11.0-base",
    "command": ["nvidia-smi"],
    "resources": {"limits": {"nvidia.com/gpu": 1}},
    "time_limit_seconds": 7200,
    "status": "scheduled",
    "created_at": "<created-at>"
  }
}
```

If the start time is already passed, the task is submitted immediately and status becomes `submitted`.

## List jobs

```bash
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/jobs
```

Response: Array of job objects (same structure as job creation response).

## Cancel job

```bash
curl -X DELETE -H "Authorization: Bearer $TOKEN" http://localhost:8000/jobs/1
```

Response:

```json
{"message": "Job cancelled", "job": 1}
```

Only `scheduled` jobs can be cancelled. This also cancels the associated reservation.

## GPU usage

Caller's own history (authenticated):

```bash
curl -H "Authorization: Bearer $TOKEN" "http://localhost:8000/gpu/usage?hours=24"
```

Response items:

```json
{"timestamp":"...","gpu_uuid":"GPU-abc","utilization":73.0,"memory_used":2048.0,"temperature":61.0,"power":120.0}
```

Per-username history (admin only):

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  "http://localhost:8000/gpu/usage/alice?hours=24"
```

```json
{"timestamp":"...","utilization":55.0,"memory":1000.0}
```

The `gpu_metrics` table is populated by a background collector in the API that scrapes DCGM metrics from Prometheus and attributes each GPU to the user running a task on its node. Rows appear only when a DCGM exporter is publishing metrics. See [`../04-kubernetes/gpu-and-monitoring.md`](../04-kubernetes/gpu-and-monitoring.md).

## Task logs

One-shot:

```bash
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/tasks/<task-name>/logs
```

Response: `{"task": "...", "pod": "task-...", "logs": "..."}`.

Live stream (`text/plain`):

```bash
curl -N -H "Authorization: Bearer $TOKEN" "http://localhost:8000/tasks/<task-name>/logs?follow=true&tail_lines=200"
```

Resolves the task's pod (`task-<task-name>` in `user-<username>`; admins can reach any user namespace).

## Task exec (interactive shell, WebSocket)

Opens an interactive shell into the task pod's `main` container. The JWT is passed as the `token` query parameter (WebSockets can't send an Authorization header easily); stdin is sent as text messages, stdout/stderr returned as text messages.

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

Close codes: `4401` invalid token, `4403` insufficient role, `4404` pod not found.

## Cleanup settings

Admin-only. View and change task-retention timings at runtime (no redeploy). The reaper reads these live.

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" http://localhost:8000/settings/cleanup
```

```json
{"enabled": true, "cleanup_interval_seconds": 60, "standalone_task_ttl_seconds": 86400, "result_ttl_seconds": 604800}
```

```bash
curl -X PUT http://localhost:8000/settings/cleanup \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"standalone_task_ttl_seconds":7200,"result_ttl_seconds":3600,"cleanup_interval_seconds":30}'
```

Validation: `cleanup_interval_seconds` >= 5, `standalone_task_ttl_seconds` >= 1, `result_ttl_seconds` >= 60. Any field may be omitted to leave it unchanged. `result_ttl_seconds` controls when task result data is auto-deleted.

## Audit log

Admin-only. One record per state-changing request (`POST`/`PUT`/`PATCH`/`DELETE`) plus every sign-in attempt, successful or not. Written by middleware after the response status is known, so failures and permission denials are recorded too; a failed write only logs a warning and never affects the request.

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  "http://localhost:8000/audit-log?hours=24&outcome=denied&limit=50"
```

```json
{
  "entries": [
    {"id": 1, "at": "<timestamp>", "actor": "alice", "actor_role": "user",
     "action": "users.delete", "target": "admin", "method": "DELETE", "path": "/users/admin",
     "status_code": 403, "outcome": "denied", "detail": {}, "client_ip": "203.0.113.10",
     "user_agent": "ExampleClient/1.0"}
  ],
  "total": 1, "hours": 24, "limit": 50, "offset": 0,
  "actors": ["admin", "alice"], "actions": ["auth.login", "users.delete"],
  "enabled": true, "includes_reads": false, "retention_seconds": 31536000
}
```

Fields:

- `action` — derived from the route template, not the concrete URL, so it is stable to filter on: `users.create`, `users.delete`, `groups.update`, `gpu.partition.create`, plus the named ones `auth.login`, `job.submit`, `job.schedule`, `task.submit`, `image.upload`.
- `outcome` — `success` (<400), `denied` (401/403), `rejected` (other 4xx), `error` (5xx).
- `target` — the path parameters of the request, e.g. the account or group the action was aimed at.
- `detail` — redacted JSON supplied by the endpoint itself (changed fields, quotas, the image submitted) plus the query string. Any field whose name looks like a secret (`pass`, `token`, `secret`, `authorization`, `api_key`, `credential`) is stored as `***` — strings, numbers and whole nested objects or lists alike, since a secret can sit under an inner key that does not itself match — so **no password or token ever reaches the log**. The single exception is a **boolean** (or `null`) value: a flag cannot carry a secret, which is what keeps the failed-login diagnostic `password_correct: false` readable instead of `"***"`. Request bodies are not captured wholesale — that would buffer multi-gigabyte image uploads.
- `enabled` / `includes_reads` — so a reader can tell "nothing happened" apart from "recording is off" or "reads are not recorded".

Filters: `hours` (1…8784, default 168), `actor`, `action`, `outcome` (exact match), `target`, `search` (substring across actor/action/target/path/detail), `limit` (≤1000, default 200), `offset`. `actors` and `actions` list everything present in the selected window, for building filter controls.

Retention is `AUDIT_TTL_SECONDS` (default 365 days) and is enforced by the same reaper thread as task cleanup.

Not covered: WebSocket connections (log streaming) bypass HTTP middleware and are therefore not recorded, and read-only requests are skipped unless `AUDIT_LOG_READS=true`.

## Task results

Tasks write outputs to `$RESULTS_DIR` (injected env var), backed by the user's persistent `scratch` PVC, so results survive pod deletion.

```bash
# list result files
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/tasks/<task>/results
# -> {"task":"...","results_dir":"/results/<task>","files":[{"size":"20","path":"output.txt"}]}

# download a single file
curl -H "Authorization: Bearer $TOKEN" "http://localhost:8000/tasks/<task>/results/download?path=output.txt" -o output.txt

# download the whole directory as a tar
curl -H "Authorization: Bearer $TOKEN" "http://localhost:8000/tasks/<task>/results/download" -o results.tar

# delete a task's results
curl -X DELETE -H "Authorization: Bearer $TOKEN" http://localhost:8000/tasks/<task>/results
```

Implemented via a per-user `results-helper` pod that mounts the scratch PVC. `path` is sandboxed (no `..` or absolute paths). Result data auto-expires after `result_ttl_seconds` (see cleanup settings).

## Queue (priority / dependencies / multi-GPU / multi-node)

```bash
curl -X POST http://localhost:8000/queue \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"image":"localhost:5000/alice/train:latest","command":["python","train.py"],
       "gpus":1,"replicas":2,"gang":true,"mpi":true,"priority":5,"depends_on":[]}'
```

Fields: `gpus` (GPU per pod), `replicas` (pods / nodes), `gang` (all-or-nothing), `mpi`, `priority`, `depends_on` (list of queue ids), `vram_limit_gb`, `project`, `time_limit_seconds`. Status flow: `queued`/`waiting_deps` → `running` → `completed`/`failed`/`cancelled`. Scheduler orders by priority + fair-share (minus current usage) and backfills jobs that fit free GPUs. Each replica pod gets `JOB_RANK`, `JOB_WORLD_SIZE`, `JOB_GROUP`, `JOB_MPI` env vars.

```bash
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/queue          # view queue
curl -X DELETE -H "Authorization: Bearer $TOKEN" http://localhost:8000/queue/<id>   # cancel
```

## Calendar export / import (iCal)

```bash
# subscribe in Google Calendar / any iCal client
http://localhost:8000/reservations/calendar.ics?token=<JWT>
# import from a file
curl -X POST -H "Authorization: Bearer $TOKEN" -F "file=@schedule.ics" http://localhost:8000/reservations/import.ics
```

## Analytics (efficiency + energy/CO₂)

```bash
curl -H "Authorization: Bearer $TOKEN" "http://localhost:8000/analytics/usage?group_by=team&hours=24"
```

Returns per-group `avg_utilization_percent`, `energy_kwh` (from sampled GPU power), `co2_kg` (energy × `CO2_KG_PER_KWH`), and an efficiency note. `group_by` = `user` | `team` | `project`.

## Disk usage (real-time)

```bash
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/disk/usage
```

Lists PVCs (home/scratch/project/shared) with type, requested size, and phase for the caller's namespaces (all user namespaces for admin).

## Team shared storage

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" "http://localhost:8000/teams/ml/shared-storage?size_gb=100"
```

Creates a `team-<team>` namespace and a ReadWriteMany PVC `<team>-shared`. Admin/poweruser only.

## Update user

```bash
curl -X PUT http://localhost:8000/users/alice \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"email":"alice@example.com","team":"ml","priority":5}'
```

## Metrics

```bash
curl http://localhost:8000/metrics
```
