#!/usr/bin/env python3
"""Configuration-only tests for deployment image and GPU mode selection."""

import os
from pathlib import Path
import subprocess
import unittest


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "devops-backend" / "scripts"
SCRIPT = SCRIPTS_DIR / "deploy-all.sh"


@unittest.skipUnless(SCRIPT.exists(), "deploy-all.sh is not present in this focused-test copy")
class DeploymentConfigTests(unittest.TestCase):
    def run_config(self, **overrides):
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "MLM_CONFIG_ONLY": "1",
            "HAS_GPU": "0",
            "GPU_SIM": "0",
            **overrides,
        }
        return subprocess.run(
            ["bash", str(SCRIPT)],
            cwd=SCRIPT.parent,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_dummy_defaults_to_local_minikube_images(self):
        result = self.run_config(MLM_ENV="dummy")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("API_IMAGE=devops-api:local", result.stdout)
        self.assertIn("CONTROLLER_IMAGE=devops-controller:local", result.stdout)
        self.assertIn("IMAGE_PULL_POLICY=IfNotPresent", result.stdout)

    def test_professional_build_defaults_to_in_cluster_registry(self):
        # OOBE: with no IMAGE_REGISTRY, a professional build must default to the
        # in-cluster registry (localhost:32000) that other-setup.sh deploys, so no
        # registry configuration is required. It must not fail asking for a registry.
        result = self.run_config(MLM_ENV="professional", BUILD_IMAGES="1")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("API_IMAGE=localhost:32000/devops-api:local", result.stdout)
        self.assertIn("CONTROLLER_IMAGE=localhost:32000/devops-controller:local", result.stdout)
        self.assertIn("IMAGE_PULL_POLICY=Always", result.stdout)

    def test_professional_build_honours_explicit_registry(self):
        result = self.run_config(
            MLM_ENV="professional",
            BUILD_IMAGES="1",
            IMAGE_REGISTRY="registry.example/mlmanage",
            IMAGE_TAG="v1",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("API_IMAGE=registry.example/mlmanage/devops-api:v1", result.stdout)
        self.assertIn("CONTROLLER_IMAGE=registry.example/mlmanage/devops-controller:v1", result.stdout)

    def test_professional_prebuilt_images_are_explicit(self):
        result = self.run_config(
            MLM_ENV="professional",
            BUILD_IMAGES="0",
            API_IMAGE="registry.example/mlmanage/api:v1",
            CONTROLLER_IMAGE="registry.example/mlmanage/controller:v1",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("API_IMAGE=registry.example/mlmanage/api:v1", result.stdout)
        self.assertIn("CONTROLLER_IMAGE=registry.example/mlmanage/controller:v1", result.stdout)

    def test_simulation_is_an_explicit_inventory_mode(self):
        result = self.run_config(MLM_ENV="dummy", HAS_GPU="1", GPU_SIM="1")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("GPU_SIM=1", result.stdout)

    def test_simulator_never_patches_node_capacity(self):
        simulator = (SCRIPTS_DIR / "simulate-gpu.sh").read_text()
        self.assertNotIn("/status/capacity", simulator)
        self.assertNotIn("kubectl proxy", simulator)
        self.assertIn("mlmanage.dev/gpu-simulated", simulator)

    def test_accelerators_are_not_divided_by_default(self):
        """Świeży deploy nie może dzielić żadnej karty — podział włącza się przez API.

        Ta ConfigMapa ustawiała na sztywno `replicas: 4` dla wszystkich kart każdego węzła,
        więc każda karta startowała pocięta na 4 udziały, choć nikt o to nie prosił.
        """
        config = (SCRIPTS_DIR.parent / "gpu-operator" / "time-slicing-config.yaml").read_text()
        profiles = config.split("\ndata:\n", 1)[1]   # same profile, bez komentarzy powyżej
        self.assertIn("default: |", profiles)        # profil bez podziału = stan wyjściowy
        for divides in ("sharing:", "timeSlicing:", "mps:", "replicas:"):
            self.assertNotIn(divides, profiles)
        deploy = SCRIPT.read_text()
        self.assertIn("devicePlugin.config.default=default", deploy)

    def test_webhook_defaults_to_fail_closed_and_uses_ephemeral_certificates(self):
        generator = (SCRIPTS_DIR / "generate-webhook-certs.sh").read_text()
        self.assertIn('VRAM_WEBHOOK_FAILURE_POLICY:-Fail', generator)
        self.assertIn('CERT_DIR="$(mktemp -d)"', generator)
        self.assertNotIn("failurePolicy: Ignore", generator)


if __name__ == "__main__":
    unittest.main(verbosity=2)
