# Project summary

## Purpose

MLManage Compute is a backend platform for running and managing AI/ML workloads on Kubernetes clusters, especially clusters with shared NVIDIA GPUs.

The project combines:

- A FastAPI backend with JWT authentication.
- PostgreSQL persistence for users, reservations, and GPU metrics.
- Kubernetes custom resources for tasks and planned user/group/project abstractions.
- A Kopf controller that turns `Task` custom resources into workload Pods.
- NVIDIA GPU Operator deployment, GPU time slicing, and a simple VRAM mutating webhook.
- Prometheus/Grafana monitoring assets and GPU alert rules.
- Setup scripts for Ubuntu GPU hosts, Docker, Minikube, kubectl, Helm, and cluster deployment.

## Main capabilities

| Capability | Implemented by | Notes |
|---|---|---|
| Health check | `GET /health` | Returns service health status. |
| User creation | `POST /users` in `devops-backend/api/main.py` | Admin-only; also attempts to create a `user-<username>` namespace. |
| User listing | `GET /users` | Admin-only; returns all users. |
| Current user info | `GET /me` | Returns authenticated user's details. |
| Login | `POST /login` | Verifies a salted PBKDF2 password hash and returns a 24-hour JWT. |
| GPU listing | `GET /gpu/list` | Reads Kubernetes node labels/capacity, or uses `LOCAL_DEV_GPUS` in dev mode. |
| GPU reservation | `POST /reservations`, `GET /reservations/calendar` | Stored in PostgreSQL, not a working CRD. |
| Task submission | `POST /tasks` | Creates a `Task` custom resource in the user's namespace (or PostgreSQL row in dev mode). |
| Task listing | `GET /tasks` | Lists user's tasks (or all if admin). |
| Task deletion | `DELETE /tasks/{task_name}` | Cancels/deletes a task. |
| Job scheduling | `POST /jobs`, `GET /jobs`, `DELETE /jobs/{job_id}` | Combines reservation + automatic task dispatch at scheduled time. |
| Background scheduler | Startup daemon thread | Dispatches scheduled jobs every 10 seconds when start_time is reached. |
| Task execution | `devops-backend/controllers/task_controller.py` | Kopf controller creates Pods from `Task` specs. |
| Time limits | Kopf timer in task controller | Deletes task Pod when `timeLimitSeconds` is exceeded. |
| VRAM limit injection | `devops-backend/gpu-operator/webhook.py` | Injects the configured default only when the controller has not set a per-task limit. |
| Disk usage metrics | `devops-backend/quotas/disk-quota-controller.py` | Polls PVCs and exposes a Prometheus metric; PVC mounting remains incomplete. |
| Monitoring | `devops-backend/monitoring/*` | kube-prometheus-stack values, GPU alerts, and a Grafana dashboard. |
| Local dev mode | `LOCAL_DEV_MODE` env var | Runs without Kubernetes, stores tasks in PostgreSQL. |
| CORS support | CORS middleware | Configurable via `CORS_ORIGINS` env var. |

## Expected runtime environment

The intended runtime is a Kubernetes cluster, commonly Minikube for development or demonstration. For GPU functionality, the host should have working NVIDIA drivers and NVIDIA Container Toolkit. The main deploy script installs/updates the NVIDIA GPU Operator and Prometheus stack through Helm.

## Main user workflow

1. Deploy infrastructure with `devops-backend/scripts/deploy-all.sh`.
2. Let the API bootstrap the first admin from `BOOTSTRAP_ADMIN_*` settings.
3. Port-forward the API service to `localhost:8000`.
4. Log in and receive a JWT.
5. Create regular users through the API.
6. Users create reservations and submit tasks.
7. The controller creates workload Pods in `user-<username>` namespaces.

## Relationship to root-level files

The repository includes two areas:

- `devops-backend/`: the main Kubernetes backend platform.
- Root-level `Dockerfile`, `script.sh`, and `manifests/`: a legacy/local developer environment that creates per-user Ubuntu containers with PVCs and a NodePort SSH service. This is separate from the main backend deployment.
