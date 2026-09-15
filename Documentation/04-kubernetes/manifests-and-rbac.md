# Manifests and RBAC

## Main namespaces

| Namespace | Purpose |
|---|---|
| `devops-system` | API, PostgreSQL, controller, webhook, quota controller, CronJobs. |
| `gpu-operator` | NVIDIA GPU Operator and device plugin components. |
| `monitoring` | kube-prometheus-stack, Grafana, PrometheusRule. |
| `user-<username>` | Per-user workload namespace. |

## API manifests

File: `devops-backend/api/api-deployment.yaml`

Creates:

- Deployment `devops-api`
- Service `devops-api` on port 8000
- ServiceAccount `api-sa`
- ClusterRole `api-role`
- ClusterRoleBinding `api-binding`

API Pod:

- Uses the API image selected by the deployment script.
- Runs `uvicorn main:app --host 0.0.0.0 --port 8000`.
- Mounts the `api-script` ConfigMap at `/app` so source-only changes can be deployed without rebuilding dependencies.

## PostgreSQL manifests

File: `devops-backend/api/postgres-deployment.yaml`

Creates:

- Secret `postgres-secret`
- Service `postgres` on port 5432
- Deployment `postgres`
- PVC `postgres-pvc` requesting 10Gi

## Controller manifests

Files:

- `devops-backend/controllers/controller-deployment.yaml`
- `devops-backend/controllers/controller-rbac.yaml`

Creates/defines:

- Deployment `devops-controller`
- ServiceAccount `devops-controller-sa`
- ClusterRole `devops-controller-role`
- ClusterRoleBinding `devops-controller-binding`

There is overlap between the deployment YAML and the separate RBAC YAML. Applying both should be idempotent if definitions match closely.

## Webhook manifests

File: `devops-backend/gpu-operator/vram-webhook.yaml`

Creates:

- Deployment `vram-webhook`
- Service `vram-webhook` port 443 -> target 8443

The TLS Secret and MutatingWebhookConfiguration are created by `generate-webhook-certs.sh`.

## Disk quota manifests

File: `devops-backend/quotas/disk-quota-controller.yaml`

Creates Deployment `disk-quota-controller` using ServiceAccount `disk-quota-sa`.

Verify RBAC for this service account before production use.

## User-facing ClusterRoles

File: `devops-backend/rbac/cluster-roles.yaml`

| ClusterRole | Permissions summary |
|---|---|
| `devops-admin` | Full access to `devops.local` resources plus namespaces, PVCs, Pods, exec/logs, deployments. |
| `devops-poweruser` | Create/get/list/update/delete tasks and reservations; pod exec; logs. |
| `devops-user` | Create/get/list/delete tasks and reservations; pod exec/logs. |
| `devops-readonly` | Get/list/watch all `devops.local` and core resources. |

Also creates `devops-admin-binding` for Kubernetes User `admin`.

## Legacy root manifests

Root `manifests/` are used by root `script.sh`, not the main backend deployment:

- `configmap.yaml`: SSH banner ConfigMap.
- `deployment.yaml`: Ubuntu Deployment using `prodimage:1`, PVC mounted at `/home/developer`.
- `networkpolicy.yaml`: deny all and allow SSH ingress.
- `service.yaml`: NodePort Service for SSH.
- `storage.yaml`: 10Gi PVC `dev-disk`.
