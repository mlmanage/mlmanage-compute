# Uninstall

## Remove Helm releases

```bash
helm uninstall gpu-operator -n gpu-operator || true
helm uninstall monitoring -n monitoring || true
```

## Remove namespaces

```bash
kubectl delete namespace devops-system gpu-operator monitoring
```

Remove user namespaces if desired:

```bash
kubectl get ns | grep '^user-' 
kubectl delete namespace user-<username>
```

## Remove CRDs

Deleting CRDs deletes all custom resources of those kinds.

```bash
kubectl delete crd tasks.devops.local
```

If `reservation-crd.yaml` is fixed in the future, include the reservation CRD too.

## Remove webhook configuration

```bash
kubectl delete mutatingwebhookconfiguration vram-limit-webhook --ignore-not-found
```

## Delete Minikube cluster

```bash
minikube delete
```
