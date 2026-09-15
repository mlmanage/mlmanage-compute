# Local developer environment files

The repository has a legacy root-level developer-container flow separate from the main `devops-backend` deployment.

## `Dockerfile`

Builds an Ubuntu 22.04 image with:

- `openssh-server`
- `sudo`
- `git`
- `nano`
- `iproute2`
- `vim-tiny`
- `curl`
- `ca-certificates`
- User `developer`
- Password login disabled in sshd config
- Root login disabled

Default command:

```dockerfile
CMD ["sudo", "/usr/sbin/sshd", "-D"]
```

## Root `script.sh`

Intended usage:

```bash
./script.sh <name>
```

It creates namespace `dev-<name>` and applies root `manifests/`:

1. PVC `dev-disk`
2. Deployment `ubuntu`
3. NodePort Service `ssh`
4. NetworkPolicies
5. ConfigMap `ssh-config`

It waits for the Pod and prints access info.

Important script note: it says SSH currently does not work and recommends:

```bash
kubectl exec -n dev-<name> -it deployment/ubuntu -- bash
```

## Root `manifests/`

| File | Purpose |
|---|---|
| `configmap.yaml` | SSH banner text. |
| `deployment.yaml` | Ubuntu Deployment using local image `prodimage:1`, PVC mount `/home/developer`, CPU/memory requests/limits. |
| `networkpolicy.yaml` | Deny all ingress/egress; allow TCP 22 ingress to `app=ubuntu`. |
| `service.yaml` | NodePort service exposing port 22. |
| `storage.yaml` | PVC `dev-disk`, `local-path`, 10Gi. |

## Relation to main backend

This legacy flow does not deploy the FastAPI API, PostgreSQL, CRDs, GPU Operator, or controllers. Use it only for standalone developer-container experiments.
