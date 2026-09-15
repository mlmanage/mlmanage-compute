# Repository inventory

## Top-level files

| Path | Purpose |
|---|---|
| `README.md` | Existing human-facing installation and usage notes. |
| `main.py` | Minimal Hello World FastAPI stub; not the real backend. |
| `Dockerfile` | Ubuntu 22.04 developer-container image with SSH server and `developer` user. Not used by `deploy-all.sh`. |
| `gpu-setup.sh` | Installs/updates NVIDIA drivers on Ubuntu and reboots. |
| `other-setup.sh` | After reboot: installs Docker, NVIDIA Container Toolkit, Minikube, kubectl, Helm, and GPU Operator. |
| `script.sh` | Legacy script creating a per-user dev namespace, PVC, Deployment, Service, NetworkPolicy, and ConfigMap from root `manifests/`. |
| `manifests/` | Legacy local dev Kubernetes resources used by root `script.sh`. |
| `Documentation/` | Project documentation. |

## `devops-backend/` inventory

| Path | Purpose |
|---|---|
| `devops-backend/api/main.py` | Main FastAPI application, DB models, auth, REST endpoints. |
| `devops-backend/api/api-deployment.yaml` | API Deployment, Service, ServiceAccount, and API RBAC. |
| `devops-backend/api/postgres-deployment.yaml` | PostgreSQL Secret, Service, Deployment, and PVC. |
| `devops-backend/controllers/task_controller.py` | Kopf operator that watches `Task` CRs and creates/deletes Pods. |
| `devops-backend/controllers/controller-deployment.yaml` | Controller Deployment and RBAC. |
| `devops-backend/controllers/controller-rbac.yaml` | Controller ServiceAccount, ClusterRole, ClusterRoleBinding; overlaps with deployment YAML. |
| `devops-backend/crd/task-crd.yaml` | Namespaced `Task` CRD used by API and controller. |
| `devops-backend/crd/reservation-crd.yaml` | Duplicates Task CRD content; it is not a valid Reservation CRD. |
| `devops-backend/examples/test-gpu-job.yaml` | Example `Task` custom resource for GPU testing. |
| `devops-backend/gpu-operator/time-slicing-config.yaml` | Device-plugin profiles. Ships only `default` with no sharing, so every GPU starts whole; the partition API adds `mlm-<node>` profiles at runtime. |
| `devops-backend/gpu-operator/webhook.py` | TLS HTTP mutating webhook that injects `VRAM_LIMIT_MB`. |
| `devops-backend/gpu-operator/vram-webhook.yaml` | Webhook Deployment and Service. |
| `devops-backend/monitoring/prometheus-values.yaml` | Helm values for kube-prometheus-stack/Grafana datasource. |
| `devops-backend/monitoring/gpu-alerts.yaml` | PrometheusRule alerts for temperature, power, and low free GPU memory. |
| `devops-backend/monitoring/gpu-dashboard.json` | Grafana dashboard (uid `mlmanage-gpu`): four `timeseries` panels for GPU utilization, VRAM usage, temperature and power. Loaded by the dashboards sidecar from the `gpu-dashboard` ConfigMap. |
| `devops-backend/quotas/disk-quota-controller.py` | Polling disk/PVC quota monitor with Prometheus metric. |
| `devops-backend/quotas/disk-quota-controller.yaml` | Deployment for disk quota controller. |
| `devops-backend/rbac/cluster-roles.yaml` | Role definitions for admin, poweruser, user, readonly. |
| `devops-backend/reservations/reminder-cronjob.yaml` | CronJob that prints reservation reminders from API calendar. |
| `devops-backend/scripts/deploy-all.sh` | Main cluster deployment orchestration script. |
| `devops-backend/scripts/generate-webhook-certs.sh` | Generates an ephemeral CA and webhook certificate, stores the TLS material in Kubernetes, and removes the local temporary files. |

## Empty or unused areas

- `devops-backend/manifests/*.yaml` are currently empty.
- Root `manifests/*.yaml` are populated and used by root `script.sh` only.
