#!/usr/bin/env bash
# Run focused API-module tests with an environment that has backend dependencies installed.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PYTHON="${PYTHON:-python3}"

cd "$HERE"
exec "$PYTHON" -m unittest -v \
  test_password_security.py \
  test_local_dev_security.py \
  test_job_contract.py \
  test_gpu_inventory.py \
  test_gpu_sharing_config.py \
  test_deployment_config.py
