# GPU and monitoring

## NVIDIA GPU Operator

The main deployment script installs/upgrades the NVIDIA GPU Operator from:

```text
https://helm.ngc.nvidia.com/nvidia
```

Release name:

```text
gpu-operator
```

Namespace:

```text
gpu-operator
```

Helm settings used by `deploy-all.sh`:

```bash
--set devicePlugin.config.name=time-slicing-config
--set devicePlugin.config.default=default
--set dcgm.enabled=true
--set dcgmExporter.enabled=true
```

## Device-plugin profiles: undivided by default

File: `devops-backend/gpu-operator/time-slicing-config.yaml`

Shipped configuration — one profile, `default`, with **no `sharing` section**:

```yaml
data:
  default: |
    version: v1
```

Every GPU node starts on this profile (`devicePlugin.config.default=default`), so **each card is published as one whole device** until it is divided through the API.

The partition API adds a second profile, `mlm-<node>`, built from all partition records of that node, and labels the node `nvidia.com/device-plugin.config=mlm-<node>`. `kubectl apply` of this file does not remove those runtime profiles, so a redeploy keeps existing splits.

## GPU partitioning API (hardware-agnostic)

The API exposes runtime, per-GPU partitioning that is validated against each card's real capabilities (detected from GPU Operator/NFD node labels, with a capacity-based fallback).

Endpoints:

- `GET /gpu/capabilities` — each GPU with `supported_modes` (`full`, `timeslice`, `mps`, and `mig` only when `nvidia.com/mig.capable=true`).
- `GET /gpu/partitions` — desired/applied partition state (DB table `gpu_partitions`).
- `POST /gpu/{uuid}/partition` (admin) — set mode:
  - `full` — whole-GPU passthrough (bare-metal CUDA/NVLink). Also the default state of every card, and how a divided card is rejoined.
  - `timeslice` / `mps` — writes the `mlm-<node>` profile into the `time-slicing-config` ConfigMap and labels the node `nvidia.com/device-plugin.config=mlm-<node>`. **Per-card**: the profile names device indices, so only the cards with a sharing record are divided (each with its own share count) and the rest stay whole. `replicas` must be **≥ 2** (one share is not a division — that is `full`), and one node cannot mix `timeslice` with `mps`: the plugin takes one mechanism per node, so the API answers **HTTP 409**.
  - `mig` — labels the node `nvidia.com/mig.config=all-<profile>` (single profile) for the GPU Operator mig-manager. **Only on MIG-capable hardware** (A100/A30/H100/H200); rejected with HTTP 400 otherwise.

Mechanism functions in `main.py`: `detect_gpu_capabilities()`, `apply_device_sharing()`, `apply_mig()`.

How many shares a card publishes (`shares` in `/gpu/list` and `/gpu/capabilities`) comes from that card's own partition record — `shares_by_gpu()` — and is `1` for a whole card. The node-wide labels `nvidia.com/gpu.replicas` and `nvidia.com/gpu.sharing-strategy` are only read for nodes whose profile MLManage does not manage (`node_sharing_is_managed()`), because GPU Feature Discovery sets them for the entire node: used as a per-card default they reported a split on cards nobody had divided.

Hardware reality (encoded honestly, not faked):

| Mode | Memory isolation | Hardware requirement |
|---|---|---|
| `full` | whole GPU | any GPU |
| `timeslice`/`mps` | none / best-effort | any GPU (per-card, ≥ 2 shares; one mechanism per node) |
| `mig` | real, hardware-isolated VRAM slices | MIG-capable only |

RBAC: the API ServiceAccount has `nodes: patch` and `configmaps: get/list/create/update/patch` (added to `api-role`) to drive this.

> **Caveat — minikube vs production:** minikube (the `dummy` environment) is single-node and runs the node as a docker container, where MIG reconfiguration is unreliable. Real MIG/multi-node belongs on a `professional` cluster (k3s/RKE2/kubeadm) with the GPU Operator controlling the host driver.

## GPU discovery by API

The API lists nodes with:

```text
nvidia.com/gpu.present=true
```

It reads:

- `node.metadata.labels["nvidia.com/gpu.product"]`
- `node.status.capacity["nvidia.com/gpu"]`
- `node.metadata.labels[f"nvidia.com/gpu.uuid.{i}"]` or fallback ID

If the GPU Operator/device plugin uses different labels, `/gpu/list` may return fallback UUIDs.

## GPU usage collector (per-user metrics)

The API runs a background thread (`gpu_metrics_loop`) that records per-user GPU usage into the `gpu_metrics` table:

1. Every `GPU_METRICS_INTERVAL` seconds (default 60), it queries `PROMETHEUS_URL` for DCGM metrics:
   - `DCGM_FI_DEV_GPU_UTIL` (utilization)
   - `DCGM_FI_DEV_FB_USED` (memory used)
   - `DCGM_FI_DEV_GPU_TEMP` (temperature)
   - `DCGM_FI_DEV_POWER_USAGE` (power)
2. It builds a GPU→user map from task pods (`app=task`) across `user-*` namespaces, keyed by node.
3. It writes one `gpu_metrics` row per GPU, attributed to the mapped user (or `unassigned`).

Notes:

- Controlled by `GPU_METRICS_ENABLED` (default `true`).
- Rows are written only when a DCGM exporter is publishing metrics; with no DCGM data the collector is a no-op.
- History is served by authenticated `GET /gpu/usage` (caller) and admin-only `GET /gpu/usage/{username}`.

## Prometheus/Grafana

The deployment installs kube-prometheus-stack from:

```text
https://prometheus-community.github.io/helm-charts
```

Release name:

```text
monitoring
```

Namespace:

```text
monitoring
```

Values file: `devops-backend/monitoring/prometheus-values.yaml`

Key settings:

- Disable default Grafana dashboards (`grafana.defaultDashboardsEnabled: false`).
- **Keep the datasource sidecar enabled** (`grafana.sidecar.datasources.enabled: true`).
  kube-prometheus-stack renders the datasource ConfigMap
  (`monitoring-kube-prometheus-grafana-datasource`, label `grafana_datasource=1`) and *only*
  that sidecar reads it into `/etc/grafana/provisioning/datasources/`. Disabling the sidecar
  leaves Grafana with no datasource at all and every panel empty.
- Do **not** add the Prometheus datasource by hand via `grafana.additionalDataSources`. The
  chart already provisions it from `sidecar.datasources.defaultDatasourceEnabled`:

```text
name: Prometheus   uid: prometheus   isDefault: true
url: http://monitoring-kube-prometheus-prometheus.monitoring:9090
```

  That URL is derived from the release name, so it always matches the Service that exists and
  the `PROMETHEUS_URL` default in `devops-backend/api/main.py`. A hand-written second entry
  named `Prometheus` collides on name/uid and gives two `isDefault` datasources, which breaks
  provisioning.
- `deploy-all.sh` must pass **no** `--set grafana.*` overrides on the `helm upgrade`; a
  `--set grafana.additionalDataSources={}` after `-f prometheus-values.yaml` wipes the
  datasource again.

Verify after deploying:

```bash
kubectl get cm -n monitoring | grep datasource        # ConfigMap must exist
kubectl get pod -n monitoring -l app.kubernetes.io/name=grafana \
  -o jsonpath='{.items[*].spec.containers[*].name}'   # must list grafana-sc-datasources
kubectl logs -n monitoring deploy/monitoring-grafana -c grafana | grep provisioning.datasources
# expect: inserting datasource from configuration name=Prometheus uid=prometheus
```

## GPU alert rules

File: `devops-backend/monitoring/gpu-alerts.yaml`

Rules:

| Alert | Expression | Duration |
|---|---|---|
| `GPUHighTemperature` | `DCGM_FI_DEV_GPU_TEMP > 85` | 5m |
| `GPUHighPower` | `DCGM_FI_DEV_POWER_USAGE > 250` | 5m |
| `GPUOOM` | `DCGM_FI_DEV_FB_FREE < 100` | 1m |

## Access Grafana

```bash
kubectl port-forward -n monitoring svc/monitoring-grafana 3000:80
```

Get admin password:

```bash
kubectl get secret -n monitoring monitoring-grafana -o jsonpath="{.data.admin-password}" | base64 -d ; echo
```

## GPU dashboard

`devops-backend/monitoring/gpu-dashboard.json` is the pre-built GPU dashboard (uid `mlmanage-gpu`,
title "GPU Monitoring") with four `timeseries` panels:

| Panel | Query | Unit |
| --- | --- | --- |
| GPU Utilization | `DCGM_FI_DEV_GPU_UTIL` | percent |
| GPU Memory Usage | `DCGM_FI_DEV_FB_USED / DCGM_FI_DEV_FB_TOTAL * 100` | percent |
| GPU Temperature | `DCGM_FI_DEV_GPU_TEMP` | celsius |
| GPU Power Consumption | `DCGM_FI_DEV_POWER_USAGE` | watt |

How it reaches Grafana:

- `deploy-all.sh` creates ConfigMap `gpu-dashboard` in `monitoring` from that file and labels it
  `grafana_dashboard=1`.
- The chart's **dashboards sidecar** (container `grafana-sc-dashboard`, enabled by default) watches
  for that label and writes the JSON into `/tmp/dashboards` inside the Grafana pod, where Grafana's
  sidecar provider picks it up. No `dashboardProviders` entry is needed — and a `type: file`
  provider must **not** be added without also mounting a directory (see below).

Three things the file must keep, or Grafana silently refuses it:

1. **No `{"dashboard": {...}}` wrapper.** That envelope is the format of the HTTP API / "Export for
   sharing externally"; the file provisioner expects the dashboard object at the top level.
2. **No `"type": "graph"` panels.** The Angular graph panel was removed in Grafana 11; such panels
   render as "Panel plugin not found: graph". Use `timeseries`.
3. **An explicit `datasource` on every panel and target.** `{"type": "prometheus", "uid":
   "prometheus"}` — `prometheus` is the fixed uid the kube-prometheus-stack chart gives the
   provisioned Prometheus datasource (`grafana.sidecar.datasources.uid`), so it cannot drift.
   Without it Grafana falls back to whatever default happens to exist at import time.

Verify after a deploy:

```bash
# ConfigMap is present and labelled
kubectl get configmap gpu-dashboard -n monitoring --show-labels

# The sidecar wrote the file into the pod
POD=$(kubectl get pod -n monitoring -l app.kubernetes.io/name=grafana -o name | head -1)
kubectl exec -n monitoring "$POD" -c grafana -- ls /tmp/dashboards

# Provisioning log: expect no "Cannot read directory" / "invalid dashboard" errors
kubectl logs -n monitoring "$POD" -c grafana | grep -i "provisioning.dashboard"
```

Note: the panels stay empty until `DCGM_*` series actually exist in Prometheus — dcgm-exporter must
be running (see the GPU Operator section above).
