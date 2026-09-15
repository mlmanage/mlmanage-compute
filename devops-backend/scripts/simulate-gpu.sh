#!/bin/bash
# Publish inventory-only GPU labels for UI/API development on CPU-only clusters.
#
# This deliberately does NOT patch node capacity/allocatable. Kubernetes extended
# resources must be provided by a device plugin; pretending capacity exists makes
# GPU pods remain Pending and gives false-positive scheduling tests.
#
# Usage:
#   ./simulate-gpu.sh
#   GPU_COUNT=2 GPU_PRODUCT=NVIDIA-A100 ./simulate-gpu.sh
#   ./simulate-gpu.sh --teardown
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
KC="${KUBECTL:-}"
if [ -z "$KC" ]; then
  if [ -x "$REPO/kubectl" ]; then KC="$REPO/kubectl"; else KC="kubectl"; fi
fi
NODE="${GPU_NODE:-minikube}"
N="${GPU_COUNT:-4}"
PRODUCT="${GPU_PRODUCT:-NVIDIA-H100}"
MIG_CAPABLE="${GPU_MIG_CAPABLE:-true}"
SIM_LABEL="mlmanage.dev/gpu-simulated"

if ! [[ "$N" =~ ^[1-9][0-9]*$ ]]; then
  echo "GPU_COUNT must be a positive integer" >&2
  exit 2
fi

gpu_labels() {
  echo "nvidia.com/gpu.present nvidia.com/gpu.product nvidia.com/gpu.count nvidia.com/mig.capable nvidia.com/mig.strategy $SIM_LABEL"
  for ((i=0; i<N; i++)); do echo "nvidia.com/gpu.uuid.$i"; done
}

if [ "${1:-}" = "--teardown" ]; then
  echo "Removing simulated GPU inventory labels from node $NODE..."
  for key in $(gpu_labels); do
    "$KC" label node "$NODE" "${key}-" >/dev/null 2>&1 || true
  done
  echo "Teardown done. No node capacity was modified."
  exit 0
fi

echo "Publishing inventory-only ${N}x ${PRODUCT} labels on node ${NODE}..."
labels="nvidia.com/gpu.present=true nvidia.com/gpu.product=${PRODUCT} nvidia.com/gpu.count=${N} nvidia.com/mig.capable=${MIG_CAPABLE} nvidia.com/mig.strategy=mixed ${SIM_LABEL}=true"
for ((i=0; i<N; i++)); do labels="$labels nvidia.com/gpu.uuid.$i=GPU-sim-$i"; done
# shellcheck disable=SC2086
"$KC" label node "$NODE" $labels --overwrite >/dev/null

actual=$("$KC" get node "$NODE" -o 'jsonpath={.metadata.labels.mlmanage\.dev/gpu-simulated}' 2>/dev/null || true)
if [ "$actual" != "true" ]; then
  echo "Failed to verify simulated inventory label on $NODE" >&2
  exit 1
fi

echo "Done. These GPUs are visible to MLManage inventory/capability APIs only."
echo "They are not schedulable; use LOCAL_DEV_MODE for synthetic workload tests or install a real device plugin."
