# Deployment script: `devops-backend/scripts/deploy-all.sh`

Run from the scripts directory:

```bash
cd devops-backend/scripts
./deploy-all.sh
```

## Modes and configuration

- `MLM_ENV=dummy` uses Minikube and builds `devops-api:local` / `devops-controller:local` inside Minikube's Docker daemon.
- `MLM_ENV=professional BUILD_IMAGES=1` builds, pushes, and renders the images into the deployments. `IMAGE_REGISTRY` defaults to the in-cluster registry `localhost:32000` (deployed by `other-setup.sh`, with k3s auto-configured to pull from it over HTTP), so no registry configuration is required; set `IMAGE_REGISTRY=…` to publish to an external registry instead.
- `MLM_ENV=professional BUILD_IMAGES=0` requires explicit pre-published `API_IMAGE` and `CONTROLLER_IMAGE` values.
- `HAS_GPU=0` skips GPU Operator, VRAM webhook, MIG, and time-slicing.
- `HAS_GPU=1 GPU_SIM=1` publishes inventory-only labels. It does not install enforcement components or claim schedulable GPU capacity.
- A real GPU deployment installs the webhook and GPU Operator. `VRAM_WEBHOOK_FAILURE_POLICY` defaults to `Fail`.

Use `MLM_CONFIG_ONLY=1` to print resolved mode/image settings without touching Docker or Kubernetes.

## Execution order

1. Resolves environment, GPU, simulation, and control-plane image settings; invalid professional image configuration fails immediately.
2. Starts or reuses Minikube for `dummy`, or verifies the configured professional cluster.
3. Creates the backend namespaces.
4. For a real GPU, generates ephemeral webhook certificates, applies/restarts the webhook, and installs GPU Operator configuration. For simulation, applies labels only.
5. Applies CRDs and controller RBAC.
6. Builds/prepares the controller image when enabled, creates its source ConfigMap, renders the configured image/pull policy, and applies the Deployment.
7. Applies PostgreSQL.
8. Builds/prepares the API image when enabled, creates its source ConfigMap, renders the configured image/pull policy, and applies the Deployment.
9. Applies professional user-image registry environment values where configured.
10. Installs monitoring, alerts, disk quota components, reservation reminders, and remaining RBAC.
11. Reuses a healthy API on port 8000, skips forwarding when the port is occupied, or starts a new port-forward.
12. Runs the backend cluster test suite unless `RUN_TESTS=0`.

The API and controller images contain dependencies, while Python source remains mounted from ConfigMaps. Source-only changes therefore update through ConfigMaps; dependency changes require rebuilding the corresponding image.

## Generated webhook certificates

`generate-webhook-certs.sh` creates its CA, key, CSR, and certificate in a temporary directory, applies only the TLS Secret and `MutatingWebhookConfiguration`, then removes local material automatically. Generated keys and certificates are not tracked in Git.

## Verification commands

```bash
kubectl get pods -n devops-system
kubectl rollout status deployment/devops-api -n devops-system
kubectl rollout status deployment/devops-controller -n devops-system
kubectl get mutatingwebhookconfiguration vram-limit-webhook  # real-GPU mode only
curl http://localhost:8000/health
curl http://localhost:8000/gpu/list
```
