# Prerequisites

## Target GPU host

Recommended by the existing README/scripts:

- Ubuntu 22.04 or 24.04.
- NVIDIA GPU and working `nvidia-smi`.
- 16+ GB RAM recommended.
- 50+ GB free disk recommended.
- Internet access for apt packages, Docker images, Helm charts, and Python packages.

## Required tools

The deployment expects:

- `kubectl`
- `helm`
- Docker or compatible runtime
- Minikube for local/single-node deployments
- `openssl` for webhook certificate generation

The scripts can install many of these on Ubuntu.

## Kubernetes namespaces created by deployment

`deploy-all.sh` creates/applies:

- `gpu-operator`
- `devops-system`
- `monitoring`

User namespaces are named:

```text
user-<username>
```

They are created by `POST /users`, not by the deployment script.

## GPU validation commands

Before attempting GPU deployment:

```bash
nvidia-smi
docker run --rm --gpus all nvidia/cuda:11.0-base nvidia-smi
kubectl get nodes -o json | jq '.items[].status.capacity'
```

For Minikube with GPU:

```bash
minikube start --driver=docker --cpus=4 --memory=8192 --gpus=all
```

For non-GPU development:

```bash
minikube start --driver=docker --cpus=4 --memory=8192
```
