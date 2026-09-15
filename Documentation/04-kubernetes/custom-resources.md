# Custom resources

CRDs live in `devops-backend/crd/` and are applied by `deploy-all.sh` with:

```bash
for crd in ../crd/*.yaml; do kubectl apply -f "$crd"; done
```

## `Task` CRD

File: `devops-backend/crd/task-crd.yaml`

- API group: `devops.local`
- Version: `v1`
- Kind: `Task`
- Plural: `tasks`
- Scope: Namespaced

Required spec fields:

- `user`
- `image`

Important optional fields:

- `project`
- `command: string[]`
- `resources.limits.cpu`
- `resources.limits.memory`
- `resources.limits.nvidia.com/gpu`
- `resources.requests`
- `timeLimitSeconds`
- `gpuUUID`
- `vramLimitGB`

Status fields:

- `phase`
- `startTime`
- `completionTime`
- `podName`
- `reason`

Example: `devops-backend/examples/test-gpu-job.yaml`.

## Reservation CRD caveat

File: `devops-backend/crd/reservation-crd.yaml`

Despite its name, this file currently contains a duplicate of the Task CRD, including:

```yaml
metadata:
  name: tasks.devops.local
kind: CustomResourceDefinition
spec:
  names:
    kind: Task
    plural: tasks
```

Reservations are currently implemented in PostgreSQL through the FastAPI app, not as Kubernetes custom resources.

## Inspecting custom resources

```bash
kubectl get crd | grep devops.local
kubectl get tasks -A
kubectl describe crd tasks.devops.local
```
