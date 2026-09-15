#!/bin/bash
# Build the API image into Minikube, or build and push it for a professional cluster.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
API_DIR="$(cd "$HERE/../api" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
IMAGE="${API_IMAGE:-devops-api:local}"
MLM_ENV="${MLM_ENV:-dummy}"
PUSH_IMAGE="${PUSH_IMAGE:-0}"

if [ -x "$REPO/minikube-linux-amd64" ]; then MK="$REPO/minikube-linux-amd64"; else MK="minikube"; fi

if [ "$MLM_ENV" = "dummy" ]; then
  command -v "$MK" >/dev/null 2>&1 || { echo "minikube is required for dummy image builds" >&2; exit 1; }
  echo "Building $IMAGE inside Minikube's Docker daemon..."
  eval "$("$MK" -p minikube docker-env)"
else
  if [ "$PUSH_IMAGE" != "1" ]; then
    echo "Professional image builds require PUSH_IMAGE=1 and a registry-backed API_IMAGE" >&2
    exit 2
  fi
  case "$IMAGE" in
    */*) ;;
    *) echo "API_IMAGE must include a registry/repository path for professional deployments: $IMAGE" >&2; exit 2 ;;
  esac
  echo "Building registry image $IMAGE..."
fi

docker build --network=host -t "$IMAGE" -f "$API_DIR/Dockerfile" "$API_DIR"
if [ "$PUSH_IMAGE" = "1" ]; then docker push "$IMAGE"; fi
echo "Prepared $IMAGE"
