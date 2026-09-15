#!/usr/bin/env python3
"""Security and contract tests for LOCAL_DEV_MODE fallbacks."""

import asyncio
import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest


class FakeWebSocket:
    def __init__(self, incoming=None):
        self.incoming = list(incoming or [])
        self.messages = []
        self.close_code = None

    async def accept(self):
        return None

    async def send_text(self, message):
        self.messages.append(message)

    async def close(self, code):
        self.close_code = code

    async def receive_text(self):
        if self.incoming:
            return self.incoming.pop(0)
        raise self.api.WebSocketDisconnect()


class LocalDevSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(cls.tmpdir.name) / "local-dev-security.db"
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
        spec = importlib.util.spec_from_file_location("mlmanage_local_dev_security", main_path)
        cls.api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.api)
        FakeWebSocket.api = cls.api

    @classmethod
    def tearDownClass(cls):
        cls.tmpdir.cleanup()

    def setUp(self):
        self.api.Base.metadata.drop_all(bind=self.api.engine)
        self.api.Base.metadata.create_all(bind=self.api.engine)
        if hasattr(self.api, "LOCAL_DEV_DELETED_RESULTS"):
            self.api.LOCAL_DEV_DELETED_RESULTS.clear()
        db = self.api.SessionLocal()
        try:
            db.add_all([
                self.api.UserDB(username="alice", hashed_password=self.api.hash_password("alice-pass"), role="user", team="red"),
                self.api.UserDB(username="bob", hashed_password=self.api.hash_password("bob-pass"), role="user", team="blue"),
                self.api.TaskDB(name="alice-task", user="alice", image="alpine", command="[]", resources="{}"),
                self.api.GroupDB(name="red"),
                self.api.GroupDB(name="blue"),
                self.api.ProjectDB(name="alice-project", owner="alice"),
            ])
            db.commit()
        finally:
            db.close()

    def user(self, username):
        db = self.api.SessionLocal()
        try:
            return db.query(self.api.UserDB).filter(self.api.UserDB.username == username).one()
        finally:
            db.close()

    def token(self, username):
        return self.api.jwt.encode({"sub": username}, self.api.SECRET_KEY)

    def test_local_exec_rejects_cross_user_and_missing_tasks(self):
        cross_user = FakeWebSocket()
        asyncio.run(self.api.task_exec(cross_user, "alice-task", self.token("bob"), "/bin/sh"))
        self.assertEqual(4404, cross_user.close_code)
        self.assertEqual(["ERROR: Task not found"], cross_user.messages)

        missing = FakeWebSocket()
        asyncio.run(self.api.task_exec(missing, "missing-task", self.token("alice"), "/bin/sh"))
        self.assertEqual(4404, missing.close_code)
        self.assertEqual(["ERROR: Task not found"], missing.messages)

    def test_local_exec_owner_gets_echo_without_falling_through_to_kubernetes(self):
        websocket = FakeWebSocket(["hello"])
        asyncio.run(self.api.task_exec(websocket, "alice-task", self.token("alice"), "/bin/sh"))
        self.assertIsNone(websocket.close_code)
        self.assertEqual(
            ["local-dev exec connected to alice-task with /bin/sh", "echo: hello"],
            websocket.messages,
        )

    def test_local_results_are_a_valid_tar_and_delete_changes_state(self):
        alice = self.user("alice")
        listing = self.api.list_results("alice-task", alice)
        self.assertEqual(["stdout.txt"], [item["path"] for item in listing["files"]])

        response = self.api.download_results("alice-task", None, alice)
        body = asyncio.run(self._streaming_body(response))
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:") as archive:
            self.assertEqual(["stdout.txt"], archive.getnames())

        self.api.delete_results("alice-task", alice)
        self.assertEqual([], self.api.list_results("alice-task", alice)["files"])
        with self.assertRaises(self.api.HTTPException) as raised:
            self.api.download_results("alice-task", None, alice)
        self.assertEqual(404, raised.exception.status_code)

    async def _streaming_body(self, response):
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.encode() if isinstance(chunk, str) else chunk)
        return b"".join(chunks)

    def test_private_image_scope_targets_are_validated_and_canonicalized(self):
        alice = self.user("alice")
        bob = self.user("bob")

        self.assertEqual(
            ("group", None, "red"),
            self.api.normalize_image_visibility("group", None, "red", alice),
        )
        with self.assertRaises(self.api.HTTPException) as wrong_group:
            self.api.normalize_image_visibility("group", None, "blue", alice)
        self.assertEqual(403, wrong_group.exception.status_code)
        with self.assertRaises(self.api.HTTPException) as missing_group:
            self.api.normalize_image_visibility("group", None, "missing", alice)
        self.assertEqual(400, missing_group.exception.status_code)

        self.assertEqual(
            ("project", "alice-project", None),
            self.api.normalize_image_visibility("project", "alice-project", None, alice),
        )
        with self.assertRaises(self.api.HTTPException) as wrong_project:
            self.api.normalize_image_visibility("project", "alice-project", None, bob)
        self.assertEqual(403, wrong_project.exception.status_code)
        self.assertEqual(("user", None, None), self.api.normalize_image_visibility("user", "stale", "stale", alice))


if __name__ == "__main__":
    unittest.main(verbosity=2)
