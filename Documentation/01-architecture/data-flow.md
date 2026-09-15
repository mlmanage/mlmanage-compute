# Data flow

## Login flow

```text
Client -> POST /login with JSON {username, password}
API -> PostgreSQL: SELECT users WHERE username=<u>
API -> verify supplied password against salted PBKDF2-HMAC-SHA256 hash
API -> audit username + user_found/password_correct booleans (never password values)
API -> return JWT {sub: username, exp: now + 24h}
Client -> sends Authorization: Bearer <token> for protected endpoints
```

Notes:

- `SECRET_KEY` must be supplied from secret management for any non-development deployment.
- Passwords are hashed before storage; plaintext database values are rejected.

## User creation flow

```text
Admin client -> POST /users with JWT
API -> role check: current_user.role == admin
API -> PostgreSQL: insert users row
API -> Kubernetes API: create namespace user-<username>
API -> namespace labels: devops.dev/user=<username>
API -> response: User <username> created
```

Important behavior:

- Namespace creation failures are logged as warnings and do not necessarily abort user creation.
- The first admin is created automatically on startup (with its namespace and workspaces) when the `users` table is empty; see [`../02-setup-and-deployment/bootstrap-admin.md`](../02-setup-and-deployment/bootstrap-admin.md). Only a *manual* SQL insert skips namespace creation.

## GPU listing flow

```text
Client -> GET /gpu/list
API -> Kubernetes API: list nodes with label nvidia.com/gpu.present=true
API -> read node label nvidia.com/gpu.product
API -> read node capacity nvidia.com/gpu
API -> infer GPU UUID labels nvidia.com/gpu.uuid.<i> or fallback gpu-<node>-<i>
API -> return list
```

This endpoint is currently public in code.

## Reservation flow

```text
User client -> POST /reservations with JWT
API -> role check: user, poweruser, or admin
API -> PostgreSQL: query active reservations for overlapping gpu_uuid interval
API -> if overlap: HTTP 409 Time slot conflict
API -> insert reservations row
API -> return reservation id
```

Calendar flow:

```text
Client -> GET /reservations/calendar[?gpu_uuid=...]
API -> PostgreSQL: active reservations, optionally filtered by gpu_uuid
API -> return calendar event objects
```

Notes:

- Reservation data is in SQL, not Kubernetes CRs.
- Calendar endpoint is currently public in code.

## Task submission and execution flow

```text
User client -> POST /tasks with JWT
API -> role check: user, poweruser, or admin
API -> if LOCAL_DEV_MODE:
         create TaskDB row in PostgreSQL
       else:
         create Task custom resource in namespace user-<username>
Kopf controller -> sees Task create event (only in Kubernetes mode)
Kopf controller -> create Pod in same namespace
Admission webhook -> patches Pod with VRAM_LIMIT_MB=4096
Kubernetes -> schedules/runs Pod
Kopf timer -> every 30s checks timeLimitSeconds
Kopf timer -> deletes Pod and sets Task status if time limit exceeded
```

Task custom resource fields used by the API/controller:

- `spec.user`
- `spec.image`
- `spec.command`
- `spec.resources`
- `spec.timeLimitSeconds`
- `spec.gpuUUID`
- Optional `spec.project`
- Optional `spec.imagePullSecrets` behavior in controller code, although not present in API schema

## Task management flows

### List tasks

```text
User client -> GET /tasks with JWT
API -> role check: user, poweruser, or admin
API -> if LOCAL_DEV_MODE:
         query TaskDB from PostgreSQL (filtered by user if not admin)
       else:
         list Task CRs from Kubernetes (cluster-wide if admin, namespace if not)
API -> return task list with status
```

### Delete task

```text
User client -> DELETE /tasks/{task_name} with JWT
API -> role check: user, poweruser, or admin
API -> if LOCAL_DEV_MODE:
         update TaskDB status to 'cancelled'
       else:
         delete Task CR (controller will clean up Pod)
API -> return success
```

## Job scheduling and execution flow

```text
User client -> POST /jobs with JWT
API -> role check: user, poweruser, or admin
API -> validate time range (end > start)
API -> check reservation conflict for GPU in time range
API -> create Reservation row in PostgreSQL
API -> create JobDB row with status='scheduled'
API -> if start_time <= now:
         dispatch task immediately
         update job status to 'submitted'
       else:
         background scheduler will dispatch later
API -> return job details

Background scheduler (runs every 10s):
  -> query jobs with status='scheduled' and start_time <= now
  -> for each due job:
       dispatch_task_for_user()
       update job status to 'submitted' or 'dispatch_failed'
```

### List jobs

```text
User client -> GET /jobs with JWT
API -> query JobDB (filtered by username if not admin)
API -> return job list ordered by start_time desc
```

### Cancel job

```text
User client -> DELETE /jobs/{job_id} with JWT
API -> role check: user, poweruser, or admin
API -> query job (filtered by username if not admin)
API -> if job status == 'scheduled':
         update job status to 'cancelled'
         update associated reservation status to 'cancelled'
API -> return success
```

## Disk quota metric flow

```text
Disk quota controller loop every 300s
-> list namespaces with label devops.dev/user-namespace=true
-> list PVCs in each namespace
-> classify PVC as home/scratch/project by name
-> try to run BusyBox df command
-> set Prometheus gauge user_disk_usage_percent
-> if >80%, post JSON alert to /internal/alerts
```

Current caveats:

- The BusyBox command does not mount the PVC being measured.
- `/internal/alerts` is not implemented by the API.
