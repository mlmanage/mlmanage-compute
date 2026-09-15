#!/usr/bin/env python3
"""Focused tests for real versus inventory-only simulated GPU discovery."""

import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch


class GPUInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(cls.tmpdir.name) / "gpu-inventory.db"
        os.environ.update({
            "DATABASE_URL": f"sqlite:///{db_path}",
            "BOOTSTRAP_ADMIN": "false",
            "GPU_METRICS_ENABLED": "false",
            "CLEANUP_ENABLED": "false",
            "QUEUE_ENABLED": "false",
            "NOTIFY_ENABLED": "false",
            "IMAGE_CLEANUP_ENABLED": "false",
            "LOCAL_DEV_MODE": "true",
        })
        default_main = Path(__file__).resolve().parents[1] / "devops-backend" / "api" / "main.py"
        main_path = Path(os.environ.get("MLM_MAIN_PATH", default_main))
        spec = importlib.util.spec_from_file_location("mlmanage_gpu_inventory", main_path)
        cls.api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.api)

    @classmethod
    def tearDownClass(cls):
        cls.tmpdir.cleanup()

    @staticmethod
    def node(labels, capacity=None):
        return SimpleNamespace(
            metadata=SimpleNamespace(name="node-a", labels=labels),
            status=SimpleNamespace(capacity=capacity or {}),
        )

    def tearDown(self):
        db = self.api.SessionLocal()
        try:
            db.query(self.api.GPUPartitionDB).delete()
            db.commit()
        finally:
            db.close()

    def record_partition(self, gpu_uuid, mode, replicas, node="node-a"):
        db = self.api.SessionLocal()
        try:
            db.add(self.api.GPUPartitionDB(
                gpu_uuid=gpu_uuid, node=node, product="NVIDIA-RTX-A5000",
                mode=mode, replicas=replicas, applied=True,
            ))
            db.commit()
        finally:
            db.close()

    def test_real_gpu_count_comes_from_node_capacity(self):
        node = self.node({"nvidia.com/gpu.present": "true"}, {"nvidia.com/gpu": "2"})
        self.assertEqual(2, self.api.gpu_count_for_node(node))
        self.assertFalse(self.api.is_simulated_gpu_node(node))

    def test_cards_are_undivided_by_default(self):
        """Bez żadnej konfiguracji karta ma 1 udział, czyli nie jest podzielona."""
        node = self.node({"nvidia.com/gpu.present": "true", "nvidia.com/gpu.count": "2"})
        self.assertEqual(1, self.api.gpu_shares_for_node(node))
        self.assertEqual({}, self.api.shares_by_gpu("node-a"))

    def test_dividing_one_card_leaves_its_siblings_at_one_share(self):
        """Etykieta `nvidia.com/gpu.replicas` jest per węzeł i NIE opisuje sąsiadów.

        Time-slicing i MPS ustawia się per karta, więc podzielenie jednej karty nie może
        pokazywać podziału na kartach, których nikt nie ruszał.
        """
        for mode in ("timeslice", "mps"):
            with self.subTest(mode=mode):
                self.record_partition("GPU-0", mode, 4)
                self.record_partition("GPU-1", "full", 1)
                node = self.node({
                    "nvidia.com/gpu.present": "true",
                    "nvidia.com/gpu.count": "3",
                    # GFD oznacza tak CAŁY węzeł, gdy podzielona jest jedna karta.
                    "nvidia.com/gpu.replicas": "4",
                    "nvidia.com/device-plugin.config": "mlm-node-a",
                })
                shares = self.api.shares_by_gpu("node-a")
                self.assertEqual(4, shares["GPU-0"])
                self.assertEqual(1, shares["GPU-1"])          # rekord `full` = cała karta
                self.assertEqual(1, self.api.gpu_shares_for_node(node))  # karta bez rekordu
                self.assertEqual(3, self.api.gpu_count_for_node(node))
                self.tearDown()

    def test_node_label_still_describes_sharing_configured_outside_mlmanage(self):
        node = self.node({
            "nvidia.com/gpu.present": "true",
            "nvidia.com/gpu.replicas": "2",
        }, {"nvidia.com/gpu": "4"})
        self.assertEqual(2, self.api.gpu_shares_for_node(node))
        self.assertEqual(2, self.api.gpu_count_for_node(node))

    def test_managed_node_counts_physical_cards_without_the_shared_units(self):
        """Fallback z pojemności: podział dokłada jednostki, nie karty."""
        self.record_partition("GPU-0", "timeslice", 4)
        node = self.node({
            "nvidia.com/gpu.present": "true",
            "nvidia.com/device-plugin.config": "mlm-node-a",
            "nvidia.com/gpu.replicas": "4",
        }, {"nvidia.com/gpu": "6"})   # 3 karty: 1 podzielona na 4 + 2 całe
        self.assertEqual(3, self.api.gpu_count_for_node(node))

    def real_capability(self, modes=("full", "timeslice", "mps")):
        return {
            "gpu_uuid": "GPU-0",
            "node": "node-a",
            "product": "NVIDIA-RTX-A5000",
            "simulated": False,
            "supported_modes": list(modes),
        }

    def test_one_share_is_rejected_as_a_sharing_mode(self):
        """`replicas: 1` to karta cała — trybem bez podziału jest `full`, nie 1 udział.

        Device plugin pomija wpis z 1 udziałem, więc taki rekord kłamałby o stanie karty.
        """
        with patch.object(self.api, "detect_gpu_capabilities", return_value=[self.real_capability()]):
            for mode in ("timeslice", "mps"):
                with self.subTest(mode=mode):
                    with self.assertRaises(self.api.HTTPException) as raised:
                        self.api.set_partition("GPU-0", self.api.PartitionRequest(mode=mode, replicas=1))
                    self.assertEqual(400, raised.exception.status_code)
                    self.assertIn(">=2", raised.exception.detail)

    def test_simulated_inventory_uses_label_count_without_claiming_capacity(self):
        node = self.node({
            "nvidia.com/gpu.present": "true",
            "nvidia.com/gpu.count": "4",
            "mlmanage.dev/gpu-simulated": "true",
        })
        self.assertEqual(4, self.api.gpu_count_for_node(node))
        self.assertTrue(self.api.is_simulated_gpu_node(node))
        self.assertNotIn("nvidia.com/gpu", node.status.capacity)

    @staticmethod
    def simulated_capability():
        return {
            "gpu_uuid": "GPU-sim-0",
            "node": "node-a",
            "product": "NVIDIA-H100",
            "simulated": True,
            "supported_modes": [],
        }

    def test_simulated_inventory_rejects_partition_mutation(self):
        with patch.object(self.api, "detect_gpu_capabilities", return_value=[self.simulated_capability()]):
            with self.assertRaises(self.api.HTTPException) as raised:
                self.api.set_partition("GPU-sim-0", self.api.PartitionRequest(mode="full"))
        self.assertEqual(409, raised.exception.status_code)

    def test_public_workload_contracts_reject_simulated_uuid(self):
        user = SimpleNamespace(id="user-1", username="alice", role="user", team=None)
        original_local_mode = self.api.LOCAL_DEV_MODE
        self.api.LOCAL_DEV_MODE = False
        now = self.api.datetime.now(self.api.timezone.utc)
        try:
            with patch.object(self.api, "detect_gpu_capabilities", return_value=[self.simulated_capability()]):
                calls = [
                    lambda: self.api.create_reservation(
                        self.api.ReservationCreate(
                            gpu_uuid="GPU-sim-0",
                            start_time=now,
                            end_time=now + self.api.timedelta(hours=1),
                        ),
                        user,
                    ),
                    lambda: self.api.create_task(
                        self.api.TaskCreate(image="alpine", gpu_uuid="GPU-sim-0"),
                        user,
                    ),
                    lambda: self.api.queue_submit(
                        self.api.QueueSubmit(image="alpine", gpu_uuid="GPU-sim-0"),
                        user,
                    ),
                ]
                for call in calls:
                    with self.subTest(call=call):
                        with self.assertRaises(self.api.HTTPException) as raised:
                            call()
                        self.assertEqual(409, raised.exception.status_code)
        finally:
            self.api.LOCAL_DEV_MODE = original_local_mode


if __name__ == "__main__":
    unittest.main(verbosity=2)
