# Install workflows

## Main recommended workflow from repository README

The existing README says the intended path is:

1. Run `./gpu-setup.sh` on the target Ubuntu GPU machine.
2. After reboot, run `./other-setup.sh`.
3. Change into the exact scripts directory:

   ```bash
   cd mlmanage-compute/devops-backend/scripts
   ./deploy-all.sh
   ```

Important: `deploy-all.sh` uses relative paths like `../api/main.py`, so run it from `devops-backend/scripts`.

## Phase 1: `gpu-setup.sh`

This script:

- Runs apt update/upgrade.
- Installs `ubuntu-drivers-common`.
- Adds the graphics drivers PPA.
- Installs the recommended NVIDIA driver or runs `ubuntu-drivers autoinstall`.
- Calls `sudo reboot`.

The script exits after reboot. Run the next phase manually after the machine comes back.

## Phase 2: `other-setup.sh`

This script:

- Verifies `nvidia-smi` works.
- Removes old Docker/container packages.
- Installs Docker CE and related plugins.
- Adds the current user to the `docker` group.
- Installs NVIDIA Container Toolkit and configures Docker runtime.
- Installs Minikube.
- Installs kubectl.
- Installs Helm.
- Starts Minikube with `--gpus=all`.
- Installs NVIDIA GPU Operator through Helm.

## Phase 3: `deploy-all.sh`

This deploys the project-specific backend components. See [`deployment-script.md`](deployment-script.md).

## Non-GPU development path

The README includes a non-GPU path:

```bash
minikube start --driver=docker --cpus=4 --memory=8192
cd devops-backend/scripts
./deploy-all.sh
```

Set `HAS_GPU=0` when running `deploy-all.sh` to skip GPU Operator, VRAM webhook, MIG, and time-slicing components.

## After installation

1. Wait for Pods:

   ```bash
   kubectl get pods -n devops-system -w
   kubectl get pods -n gpu-operator -w
   kubectl get pods -n monitoring -w
   ```

2. Bootstrap the first admin user: [`bootstrap-admin.md`](bootstrap-admin.md).
3. Port-forward if not already running:

   ```bash
   kubectl port-forward -n devops-system svc/devops-api 8000:8000
   ```
