#!/usr/bin/env python3
"""Focused security tests for password storage and login audit logging."""

import importlib.util
import logging
import os
from pathlib import Path
import tempfile
import unittest

from pydantic import ValidationError


class MessageCollector(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


class PasswordSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(cls.tmpdir.name) / "password-security.db"
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
        spec = importlib.util.spec_from_file_location("mlmanage_password_security", main_path)
        cls.api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.api)

    @classmethod
    def tearDownClass(cls):
        cls.tmpdir.cleanup()

    def setUp(self):
        self.api.Base.metadata.drop_all(bind=self.api.engine)
        self.api.Base.metadata.create_all(bind=self.api.engine)
        self.logs = MessageCollector()
        self.api.logger.addHandler(self.logs)
        self.api.logger.setLevel(logging.INFO)

    def tearDown(self):
        self.api.logger.removeHandler(self.logs)

    def stored_password(self, username):
        db = self.api.SessionLocal()
        try:
            return db.query(self.api.UserDB).filter(self.api.UserDB.username == username).one().hashed_password
        finally:
            db.close()

    def test_created_user_password_is_hashed_and_can_authenticate(self):
        plaintext = "created-user-secret"
        self.api.create_user(self.api.UserCreate(username="alice", password=plaintext))

        stored = self.stored_password("alice")
        self.assertNotEqual(plaintext, stored)
        self.assertTrue(stored.startswith("pbkdf2_sha256$"), stored)
        self.assertTrue(self.api.verify_password(plaintext, stored))
        self.assertFalse(self.api.verify_password("wrong", stored))
        self.assertIn("access_token", self.api.login(self.api.LoginRequest(username="alice", password=plaintext)))

    def test_bootstrap_admin_password_is_hashed(self):
        original_username = self.api.BOOTSTRAP_ADMIN_USERNAME
        original_password = self.api.BOOTSTRAP_ADMIN_PASSWORD
        try:
            self.api.BOOTSTRAP_ADMIN_USERNAME = "bootstrap-admin"
            self.api.BOOTSTRAP_ADMIN_PASSWORD = "bootstrap-secret"
            self.api.bootstrap_admin()
        finally:
            self.api.BOOTSTRAP_ADMIN_USERNAME = original_username
            self.api.BOOTSTRAP_ADMIN_PASSWORD = original_password

        stored = self.stored_password("bootstrap-admin")
        self.assertNotEqual("bootstrap-secret", stored)
        self.assertTrue(self.api.verify_password("bootstrap-secret", stored))

    def test_login_audit_contains_only_username_and_boolean_outcomes(self):
        plaintext = "never-log-this-secret"
        self.api.create_user(self.api.UserCreate(username="bob", password=plaintext))

        self.api.login(self.api.LoginRequest(username="bob", password=plaintext))
        with self.assertRaises(self.api.HTTPException):
            self.api.login(self.api.LoginRequest(username="bob", password="also-never-log-this"))
        with self.assertRaises(self.api.HTTPException):
            self.api.login(self.api.LoginRequest(username="missing-user", password="missing-secret"))

        messages = "\n".join(self.logs.messages)
        self.assertIn("Login attempt: username=bob, user_found=True, password_correct=True", messages)
        self.assertIn("Login attempt: username=bob, user_found=True, password_correct=False", messages)
        self.assertIn("Login attempt: username=missing-user, user_found=False, password_correct=False", messages)
        for secret in (plaintext, "also-never-log-this", "missing-secret"):
            self.assertNotIn(secret, messages)

    def test_login_fields_reject_log_injection_and_unbounded_passwords(self):
        with self.assertRaises(ValidationError):
            self.api.LoginRequest(username="alice\nforged-log", password="secret")
        with self.assertRaises(ValidationError):
            self.api.LoginRequest(username="alice", password="x" * 1025)

    def test_plaintext_database_value_is_never_accepted(self):
        db = self.api.SessionLocal()
        try:
            db.add(self.api.UserDB(username="legacy", hashed_password="plaintext", role="user"))
            db.commit()
        finally:
            db.close()

        with self.assertRaises(self.api.HTTPException) as raised:
            self.api.login(self.api.LoginRequest(username="legacy", password="plaintext"))
        self.assertEqual(401, raised.exception.status_code)


if __name__ == "__main__":
    unittest.main(verbosity=2)
