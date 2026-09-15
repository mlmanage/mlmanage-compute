# Testing and validation

The repository contains cluster end-to-end suites and focused API-module tests under `tests_suite/`. Run the focused suite for authentication, local-development authorization/fallbacks, image scopes, and scheduled-job contracts:

```bash
cd tests_suite
python3 -m venv .venv-focused
.venv-focused/bin/python -m pip install -r requirements-focused.txt
PYTHON="$PWD/.venv-focused/bin/python" ./run_focused.sh
```

Run `./run.sh` for the Kubernetes end-to-end suite. The checks below remain useful for narrower validation and troubleshooting.

## Python syntax checks

```bash
python3 -m py_compile devops-backend/api/main.py
python3 -m py_compile devops-backend/controllers/task_controller.py
python3 -m py_compile devops-backend/gpu-operator/webhook.py
python3 -m py_compile devops-backend/quotas/disk-quota-controller.py
```

## YAML parse check

If Python has PyYAML installed:

```bash
python3 - <<'PY'
import pathlib, yaml
for p in pathlib.Path('.').rglob('*.yaml'):
    if '.git' in p.parts or 'Documentation' in p.parts:
        continue
    with p.open() as f:
        list(yaml.safe_load_all(f))
    print('ok', p)
PY
```

## Kubernetes dry-run

```bash
kubectl apply --dry-run=client -f devops-backend/api/api-deployment.yaml
kubectl apply --dry-run=client -f devops-backend/controllers/controller-deployment.yaml
kubectl apply --dry-run=client -f devops-backend/crd/task-crd.yaml
```

## API smoke tests

```bash
curl http://localhost:8000/gpu/list
curl -X POST http://localhost:8000/login \
  -H "Content-Type: application/json" \
  -d '{"username":"<username>","password":"<password>"}'
```

With token:

```bash
curl -X POST http://localhost:8000/tasks \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"image":"alpine:latest","command":["echo","hello"],"time_limit_seconds":60}'
```

Then:

```bash
kubectl get tasks -n user-admin
kubectl get pods -n user-admin
kubectl logs -n user-admin <pod-name>
```

## Controller validation

```bash
kubectl logs -n devops-system deployment/devops-controller
kubectl describe task -n user-admin <task-name>
```

## Webhook validation

The webhook's `namespaceSelector` is `devops.dev/user-namespace=true`, so it only mutates pods
in user namespaces (never `kube-system` — an app webhook must not be able to block system
components). Test inside a user namespace, not `default`:

```bash
kubectl -n user-admin run webhook-test --image=alpine --restart=Never -- sleep 3600
kubectl -n user-admin get pod webhook-test -o yaml | grep -A2 VRAM_LIMIT_MB
kubectl -n user-admin delete pod webhook-test
```

## Documentation validation

Check links and tree manually:

```bash
find Documentation -type f | sort
```

Every folder should have a `README.md` with a sub-map.
