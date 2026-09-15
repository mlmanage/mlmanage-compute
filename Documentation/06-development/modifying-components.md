# Modifying components

## API changes

Edit:

```text
devops-backend/api/main.py
```

Redeploy with:

```bash
cd devops-backend/scripts
./deploy-all.sh
```

Or update only the ConfigMap and restart Deployment:

```bash
kubectl create configmap api-script -n devops-system \
  --from-file=main.py=devops-backend/api/main.py \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl rollout restart deployment/devops-api -n devops-system
```

## Task controller changes

Edit:

```text
devops-backend/controllers/task_controller.py
```

Update ConfigMap and restart:

```bash
kubectl create configmap controller-scripts -n devops-system \
  --from-file=task_controller.py=devops-backend/controllers/task_controller.py \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl rollout restart deployment/devops-controller -n devops-system
```

## Webhook changes

Edit:

```text
devops-backend/gpu-operator/webhook.py
```

Update ConfigMap and restart:

```bash
kubectl create configmap vram-webhook-script -n devops-system \
  --from-file=webhook.py=devops-backend/gpu-operator/webhook.py \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl rollout restart deployment/vram-webhook -n devops-system
```

If TLS/service names change, regenerate webhook certs.

## CRD changes

Edit files under:

```text
devops-backend/crd/
```

Apply:

```bash
kubectl apply -f devops-backend/crd/<file>.yaml
```

Be careful: CRD schema changes can affect existing custom resources.

## Manifest changes

Most manifests can be applied with:

```bash
kubectl apply -f <manifest.yaml>
```

Then restart the matching Deployment if Pod template did not change but mounted ConfigMap content changed.

## Safer future development pattern

Consider moving from runtime `pip install` + ConfigMaps to proper images:

1. Add requirements files.
2. Build versioned API/controller/webhook images.
3. Push to a registry.
4. Reference immutable tags in manifests.
5. Use CI to lint Python and validate YAML.
