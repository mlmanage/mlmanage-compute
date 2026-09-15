# Current state and known gaps

This file records known implementation caveats. Read it before changing behavior or relying on partially implemented features.

## Important mismatches

| Area | Current state | Impact |
|---|---|---|
| API location | Root `main.py` is a stub. The backend API is `devops-backend/api/main.py`. | Make API changes in the backend module. |
| Password storage | `hashed_password` stores salted PBKDF2-HMAC-SHA256 hashes; plaintext records are rejected. | Existing plaintext development rows must be reset rather than migrated. |
| Reservation CRD | `devops-backend/crd/reservation-crd.yaml` duplicates the Task CRD. | Reservations exist only as SQL rows; Kubernetes reservation objects are not usable. |
| Dedicated workspaces | `POST /users` creates `<user>-home`, `<user>-scratch`, `<user>-project` PVCs labeled `devops.dev/volume-type`. | /home, /scratch, /project now provisioned automatically (sizes via `DEFAULT_DISK_*_GB`). |
| Disk quota mount check | `get_pvc_usage()` starts a BusyBox Pod but does not mount the PVC being measured. | The usage result likely does not measure the target PVC. |
| Disk alert endpoint | Quota controller posts to `/internal/alerts`; API has no such route. | Soft-limit alerts are effectively no-op except logs/metrics. |
| VRAM webhook | Injects `VRAM_LIMIT_MB` only when absent; controller sets it from the Task's `vramLimitGB`. | VRAM limits are now per-task (`vram_limit_gb`); webhook default is configurable via `DEFAULT_VRAM_LIMIT_MB`. |
| Calendar/GPU list auth | `/reservations/calendar`, `/gpu/list`, `/metrics`, and `/health` have no auth dependency; cross-user `/gpu/usage/{username}` is admin-only. | The remaining public inventory/calendar/metrics endpoints may expose operational metadata. |
| Local dev mode | `LOCAL_DEV_MODE` enables operation without Kubernetes; tasks stored in PostgreSQL. | Useful for testing but changes behavior significantly. |
| Job scheduler | Background thread runs every 10s checking for scheduled jobs to dispatch. | No documented way to monitor scheduler health or failed dispatches except logs. |
| Legacy dev SSH | Script notes SSH currently does not work and recommends `kubectl exec`. | Do not rely on SSH until fixed. |
| Monitoring dashboard | `gpu-dashboard.json` holds four `timeseries` panels (GPU utilization, VRAM usage, temperature, power) bound to datasource uid `prometheus`, provisioned by the Grafana dashboards sidecar. | The panels only draw data once `DCGM_*` series exist in Prometheus; with dcgm-exporter missing they render empty. |

## Features that are schema-only or partial

- `Reservation` is implemented as a SQL table and API endpoints, not a functional CRD.
- `Job` scheduling is fully implemented with database tracking and automatic task dispatch.
- RBAC ClusterRoles are defined, but API authorization is separate and based on SQL user roles/JWTs.
- GPU metrics table is populated by a background collector in the API (scrapes DCGM from Prometheus, attributes per user). Rows appear only when a DCGM exporter is publishing metrics.
- Task CRUD (create, read, delete) is fully implemented for both Kubernetes and local dev modes.

## Production-hardening priorities

1. Move credentials out of plaintext manifests and rotate all development defaults.
2. Fix the Reservation CRD or remove the misleading file.
3. Fix PVC mounting logic in the disk quota controller.
4. Add tests and CI validation for API, manifests, and scripts.
5. Add authentication to public endpoints (`/gpu/list`, `/reservations/calendar`, `/metrics`, etc.).
6. Add monitoring and health reporting for the background job scheduler and GPU-metrics collector.
