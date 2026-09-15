#!/usr/bin/env python3
"""Focused contract tests for scheduled-job field persistence and dispatch."""

from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest


class JobContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(cls.tmpdir.name) / "job-contract.db"
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
        spec = importlib.util.spec_from_file_location("mlmanage_job_contract", main_path)
        cls.api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.api)

    @classmethod
    def tearDownClass(cls):
        cls.tmpdir.cleanup()

    def setUp(self):
        self.api.Base.metadata.drop_all(bind=self.api.engine)
        self.api.Base.metadata.create_all(bind=self.api.engine)
        db = self.api.SessionLocal()
        try:
            db.add(self.api.UserDB(
                username="admin",
                hashed_password=self.api.hash_password("test"),
                role="admin",
            ))
            db.add(self.api.GPUPartitionDB(
                node="local",
                gpu_uuid="gpu-mig",
                product="Fake MIG GPU",
                mode="mig",
                mig_profiles=json.dumps({"1g.5gb": 2}),
            ))
            db.commit()
        finally:
            db.close()

    def user(self):
        db = self.api.SessionLocal()
        try:
            return db.query(self.api.UserDB).filter(self.api.UserDB.username == "admin").one()
        finally:
            db.close()

    def test_scheduled_job_persists_execution_and_reservation_fields(self):
        now = datetime.now(timezone.utc)
        result = self.api.create_job(
            self.api.JobCreate(
                gpu_uuid="gpu-mig",
                start_time=now + timedelta(hours=1),
                end_time=now + timedelta(hours=2),
                image="alpine:latest",
                command=["echo", "scheduled"],
                vram_limit_gb=5,
                project="training",
                gpu_partition="1g.5gb",
                priority=7,
            ),
            self.user(),
        )

        self.assertEqual(5, result["job"]["vram_limit_gb"])
        self.assertEqual("training", result["job"]["project"])
        self.assertEqual("1g.5gb", result["job"]["gpu_partition"])
        self.assertEqual(7, result["job"]["priority"])

        db = self.api.SessionLocal()
        try:
            reservation = db.query(self.api.Reservation).filter(
                self.api.Reservation.id == result["job"]["reservation_id"]
            ).one()
            self.assertEqual("1g.5gb", reservation.gpu_partition)
            self.assertEqual(7, reservation.priority)
            self.assertEqual(0, reservation.slice_index)
        finally:
            db.close()

    def test_immediate_job_dispatches_vram_project_and_partition_to_task(self):
        now = datetime.now(timezone.utc)
        result = self.api.create_job(
            self.api.JobCreate(
                gpu_uuid="gpu-mig",
                start_time=now - timedelta(seconds=1),
                end_time=now + timedelta(minutes=30),
                image="alpine:latest",
                command=["echo", "immediate"],
                vram_limit_gb=4,
                project="inference",
                gpu_partition="1g.5gb",
                priority=3,
            ),
            self.user(),
        )

        self.assertEqual("submitted", result["job"]["status"])
        self.assertTrue(result["job"]["task_name"])
        db = self.api.SessionLocal()
        try:
            task = db.query(self.api.TaskDB).filter(
                self.api.TaskDB.name == result["job"]["task_name"]
            ).one()
            self.assertEqual(4, task.vram_limit_gb)
            self.assertEqual("inference", task.project)
            self.assertEqual("1g.5gb", task.gpu_partition)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
