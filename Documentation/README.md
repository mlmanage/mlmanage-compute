# MLManage Compute Documentation

## Whole documentation map

Read this file first. It is the root documentation map. Each folder contains its own `README.md` with more targeted links.

```text
Documentation/
├── README.md                                      # This root map and navigation guide
├── 00-overview/
│   ├── README.md                                  # Overview sub-map
│   ├── project-summary.md                         # What the project is and what it provides
│   ├── repository-inventory.md                    # Source tree and purpose of important files
│   └── current-state-and-gaps.md                  # Known quirks, gaps, and implementation caveats
├── 01-architecture/
│   ├── README.md                                  # Architecture sub-map
│   ├── system-architecture.md                     # Main components and high-level diagrams
│   ├── data-flow.md                               # User, reservation, and task flows
│   └── persistence-and-security.md                # Database, authentication, authorization, secrets
├── 02-setup-and-deployment/
│   ├── README.md                                  # Setup/deployment sub-map
│   ├── prerequisites.md                           # Required host, Kubernetes, GPU, and tooling
│   ├── install-workflows.md                       # Main install flow and manual install notes
│   ├── deployment-script.md                       # `devops-backend/scripts/deploy-all.sh` details
│   ├── bootstrap-admin.md                         # First admin user and namespace bootstrap
│   └── environment-variables.md                   # API configuration via environment variables
├── 03-api/
│   ├── README.md                                  # API sub-map
│   ├── endpoints.md                               # Endpoint reference and examples
│   ├── models.md                                  # SQLAlchemy and Pydantic models
│   └── authentication-and-roles.md                # JWT and role behavior
├── 04-kubernetes/
│   ├── README.md                                  # Kubernetes sub-map
│   ├── custom-resources.md                        # CRDs and custom object schemas
│   ├── controllers-and-webhooks.md                # Kopf task controller, VRAM webhook, quota controller
│   ├── manifests-and-rbac.md                      # Deployments, services, service accounts, RBAC
│   └── gpu-and-monitoring.md                      # GPU Operator, time slicing, Prometheus/Grafana
├── 05-operations/
│   ├── README.md                                  # Operations sub-map
│   ├── common-tasks.md                            # Commands for common operational tasks
│   ├── troubleshooting.md                         # Failure modes and fixes
│   └── uninstall.md                               # Cleanup instructions
└── 06-development/
    ├── README.md                                  # Development sub-map
    ├── local-dev-environment.md                   # Legacy dev container script and Dockerfile
    ├── modifying-components.md                    # How to change API/controller/webhook/docs safely
    └── testing-and-validation.md                  # Practical checks after changes
```

## Fast navigation by intent

| If you need to... | Start here |
|---|---|
| Understand the project quickly | [`00-overview/project-summary.md`](00-overview/project-summary.md) |
| Understand the runtime architecture | [`01-architecture/system-architecture.md`](01-architecture/system-architecture.md) |
| Deploy the system | [`02-setup-and-deployment/install-workflows.md`](02-setup-and-deployment/install-workflows.md) |
| Use or modify the API | [`03-api/endpoints.md`](03-api/endpoints.md) |
| Work with CRDs/controllers/webhooks | [`04-kubernetes/README.md`](04-kubernetes/README.md) |
| Troubleshoot a cluster | [`05-operations/troubleshooting.md`](05-operations/troubleshooting.md) |
| Change code safely | [`06-development/modifying-components.md`](06-development/modifying-components.md) |

## Project in one paragraph

MLManage Compute is a Kubernetes-based backend for shared AI/ML training infrastructure. It exposes a FastAPI service for users, JWT login, GPU discovery, GPU reservations, task submission, and metrics. Submitted tasks become Kubernetes `Task` custom resources, which a Kopf controller translates into Pods in per-user namespaces. The deployment also installs PostgreSQL, NVIDIA GPU Operator, optional GPU time slicing, a VRAM mutating webhook, Prometheus/Grafana monitoring, RBAC, and support controllers/scripts.

## Primary source-code entrypoints

- Real API: [`../devops-backend/api/main.py`](../devops-backend/api/main.py)
- Main deployment script: [`../devops-backend/scripts/deploy-all.sh`](../devops-backend/scripts/deploy-all.sh)
- Task controller: [`../devops-backend/controllers/task_controller.py`](../devops-backend/controllers/task_controller.py)
- Task CRD: [`../devops-backend/crd/task-crd.yaml`](../devops-backend/crd/task-crd.yaml)
- VRAM webhook: [`../devops-backend/gpu-operator/webhook.py`](../devops-backend/gpu-operator/webhook.py)
- Disk quota controller: [`../devops-backend/quotas/disk-quota-controller.py`](../devops-backend/quotas/disk-quota-controller.py)

## Important caveats

- Root [`../main.py`](../main.py) is only a Hello World FastAPI stub; the real API is in `devops-backend/api/main.py`.
- Passwords are stored as salted PBKDF2-HMAC-SHA256 hashes; plaintext database values are rejected.
- `/health` returns `{"status": "ok"}`.
- `devops-backend/crd/reservation-crd.yaml` duplicates the Task CRD instead of defining a Reservation CRD.
- User namespaces carry both `devops.dev/user=<username>` and `devops.dev/user-namespace=true`; the disk quota controller relies on the latter.
- VRAM limits are per-task: the API/controller set `VRAM_LIMIT_MB` from the Task's `vramLimitGB`, and the webhook only injects a configurable default when none is present.
- The API includes a background scheduler thread that dispatches scheduled jobs every 10 seconds.
- Local dev mode (`LOCAL_DEV_MODE`) changes behavior significantly by storing tasks in PostgreSQL instead of Kubernetes.
