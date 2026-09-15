# Troubleshooting

## API returns 500 when creating tasks

Likely causes:

- User namespace `user-<username>` does not exist.
- Task CRD is not installed.
- API service account lacks permission.
- Kubernetes in-cluster config failed.

Checks:

```bash
kubectl get ns user-<username>
kubectl get crd tasks.devops.local
kubectl logs -n devops-system deployment/devops-api
kubectl auth can-i create tasks --as=system:serviceaccount:devops-system:api-sa -n user-<username>
```

## Admin can log in but cannot run tasks

If admin was inserted manually into PostgreSQL, create namespace manually:

```bash
kubectl create namespace user-admin
kubectl label namespace user-admin devops.dev/user=admin
kubectl label namespace user-admin devops.dev/user-namespace=true
```

Both labels matter. `devops.dev/user-namespace=true` is what the disk quota controller
discovers, and it is also the `namespaceSelector` of the VRAM mutating webhook — without it,
pods in this namespace never receive the default `VRAM_LIMIT_MB`.

## Pods stuck in CrashLoopBackOff

Check logs:

```bash
kubectl logs -n devops-system <pod-name>
kubectl describe pod -n devops-system <pod-name>
```

Common causes:

- Python dependency install fails at startup.
- ConfigMap missing or wrong mount path.
- PostgreSQL unavailable when API starts.
- Webhook certificate secret missing.

## GPU not discovered

Checks:

```bash
nvidia-smi
kubectl get nodes -o json | jq '.items[].status.capacity'
kubectl get pods -n gpu-operator
kubectl describe node <node-name> | grep -i nvidia
```

If `/gpu/list` returns empty, inspect node labels:

```bash
kubectl get nodes --show-labels | tr ',' '\n' | grep nvidia
```

### On k3s: device plugin runs but reports "No devices found"

`nvidia-smi` works on the host, the device plugin pod is `1/1 Running`, yet the node advertises
no `nvidia.com/gpu`:

```text
Incompatible strategy detected auto
If this is a GPU node, did you configure the NVIDIA Container Toolkit?
No devices found. Waiting indefinitely.
```

NVIDIA's static device-plugin manifest assumes `nvidia` is containerd's **default** runtime.
On k3s it is not — k3s creates a separate `nvidia` RuntimeClass (when it detects
`nvidia-container-runtime`), and pods must request it explicitly:

```bash
kubectl get runtimeclass nvidia                       # must exist
kubectl patch daemonset nvidia-device-plugin-daemonset -n kube-system --type=merge \
  -p '{"spec":{"template":{"spec":{"runtimeClassName":"nvidia"}}}}'
kubectl -n kube-system rollout status daemonset/nvidia-device-plugin-daemonset
kubectl get node -o jsonpath='{.items[0].status.capacity.nvidia\.com/gpu}{"\n"}'   # -> 1
```

`other-setup.sh` now applies this automatically and idempotently. If the RuntimeClass is
missing, install the NVIDIA Container Toolkit and restart k3s so it gets detected.

Note `/gpu/capabilities` falls back to nodes with `nvidia.com/gpu` **capacity**, so it works
without NFD labels. `/gpu/list` has no such fallback — it strictly requires
`nvidia.com/gpu.present=true`, which the GPU Operator's NFD normally applies.

## Webhook blocks or fails pod creation

Checks:

```bash
kubectl get mutatingwebhookconfiguration vram-limit-webhook
kubectl get secret -n devops-system vram-webhook-tls
kubectl logs -n devops-system deployment/vram-webhook
```

Regenerate certificates:

```bash
cd devops-backend/scripts
./generate-webhook-certs.sh
kubectl apply -f ../gpu-operator/vram-webhook.yaml
```

## Reservation reminder CronJob fails

The CronJob command pipes through `jq`. Verify `jq` exists in `curlimages/curl:latest`; if not, use an image with jq or rewrite command.

```bash
kubectl get cronjob -n devops-system reservation-reminder
kubectl get jobs -n devops-system
kubectl logs -n devops-system job/<job-name>
```

## Disk quota metrics are missing or incorrect

The controller discovers namespaces labeled `devops.dev/user-namespace=true`. Confirm that the
label exists and that the controller can list the namespace and its PVCs:

```bash
kubectl get namespaces -l devops.dev/user-namespace=true
kubectl logs -n devops-system deployment/disk-quota-controller
```

The controller's temporary BusyBox Pod does not currently mount the PVC it is intended to measure,
so reported usage may be inaccurate even when discovery succeeds.

## Every PVC stays Pending / result download hangs or 503s

Check the StorageClass first — this is the most common silent killer:

```bash
kubectl get sc                       # is there a (default) class?
kubectl get pvc -A                   # anything Pending?
kubectl describe pvc -n devops-system postgres-pvc | tail -15
```

`storageclass.storage.k8s.io "standard" not found` means the API was configured with a class
that is not installed in the target cluster. StorageClass names vary by Kubernetes distribution.
The API leaves `storageClassName` unset by default so the cluster's *default* class
applies. Only set `WORKSPACE_STORAGE_CLASS` if your cluster has no default class:

```bash
kubectl set env deployment/devops-api -n devops-system WORKSPACE_STORAGE_CLASS=local-path
```

The API logs the resolved class at startup, so check it directly:

```bash
kubectl logs -n devops-system deployment/devops-api | grep -i storageclass
```

Because `<user>-scratch` is what the `results-helper` pod mounts, an unbound workspace PVC makes
the helper unschedulable and result listing/download fail. That now surfaces as a `503` naming
the cause instead of hanging until the client times out:

```text
503 Results helper not ready (Unschedulable: pod has unbound immediate PersistentVolumeClaims)
```

## Cannot log in

Check that the user exists without printing password hashes into terminal logs:

```bash
kubectl exec -it -n devops-system deployment/postgres -- \
  psql -U postgres devops -c "SELECT username, role FROM users ORDER BY username;"
```

The API expects a `pbkdf2_sha256$...` value in `hashed_password`; plaintext values are deliberately rejected. Reset disposable development accounts or use the normal bootstrap/user-creation paths rather than editing password values with SQL.

## Jobs not dispatching

Jobs should be dispatched automatically when their `start_time` is reached. The background scheduler runs every 10 seconds.

Check API logs for scheduler activity:

```bash
kubectl logs -n devops-system deployment/devops-api | grep -i "scheduled job"
```

Check job status in database:

```sql
SELECT id, username, status, start_time, end_time, task_name 
FROM jobs 
WHERE status = 'scheduled' AND start_time <= NOW()
ORDER BY start_time DESC;
```

Possible issues:

- API pod restarted and scheduler thread stopped
- Job dispatch failed (status will be `dispatch_failed`)
- User namespace doesn't exist
- `LOCAL_DEV_MODE` is enabled and Kubernetes isn't available

## Tasks not appearing in listing

If tasks don't show in `GET /tasks`:

In Kubernetes mode:
```bash
kubectl get tasks -n user-<username>
kubectl get tasks -A  # if admin
```

In local dev mode:
```sql
SELECT * FROM tasks WHERE user = '<username>';
```

Check API logs for errors during task creation.

## CORS errors from frontend

If browser shows CORS errors:

1. Check `CORS_ORIGINS` environment variable in API deployment
2. Verify it includes your frontend origin
3. Restart API pod after changing env vars:

```bash
kubectl rollout restart deployment/devops-api -n devops-system
```

## Local dev mode issues

If `LOCAL_DEV_MODE=true` but tasks aren't working:

1. Check database connection - tasks are stored in PostgreSQL
2. Verify `tasks` table exists:

```sql
\dt tasks
SELECT * FROM tasks ORDER BY created_at DESC LIMIT 5;
```

3. Check API logs for SQL errors

If GPU list is empty in local dev mode:

Set `LOCAL_DEV_GPUS`:

```bash
kubectl set env deployment/devops-api -n devops-system \
  LOCAL_DEV_GPUS='[{"uuid":"dev-gpu-0","product":"Dev GPU","node":"local"}]'
```
