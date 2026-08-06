import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as module
from auth_security import totp_code, verify_password


class BrowserSetupTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temporary_directory.name) / "builder-auth.json"
        self.old_auth = module.AUTH_CONFIG
        self.old_error = module.AUTH_CONFIG_ERROR
        self.old_secret = module.app.secret_key
        self.old_setup_token = module.SETUP_TOKEN
        module.AUTH_CONFIG = None
        module.AUTH_CONFIG_ERROR = "not configured"
        module.SETUP_TOKEN = "test-setup-token"
        module.LOGIN_RATE_BUCKETS.clear()
        module.app.config.update(TESTING=True, SECRET_KEY="temporary-setup-session-key")
        self.path_patcher = patch.object(module, "AUTH_CONFIG_PATH", self.config_path)
        self.path_patcher.start()
        self.client = module.app.test_client()

    def tearDown(self):
        self.path_patcher.stop()
        module.AUTH_CONFIG = self.old_auth
        module.AUTH_CONFIG_ERROR = self.old_error
        module.SETUP_TOKEN = self.old_setup_token
        module.app.secret_key = self.old_secret
        self.temporary_directory.cleanup()

    def test_setup_is_single_use_and_persists_only_derived_credentials(self):
        response = self.client.get("/setup")
        self.assertEqual(response.status_code, 200)
        with self.client.session_transaction() as setup_session:
            csrf = setup_session["csrf_token"]
            secret = setup_session["setup_totp_secret"]

        password = "correct horse battery staple"
        response = self.client.post("/setup", data={
            "csrf_token": csrf,
            "setup_token": "test-setup-token",
            "username": "builder-admin",
            "password": password,
            "confirm_password": password,
            "totp": totp_code(secret),
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/")
        self.assertTrue(self.config_path.is_file())
        self.assertEqual(self.config_path.stat().st_mode & 0o777, 0o600)

        persisted = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertNotIn(password, self.config_path.read_text(encoding="utf-8"))
        self.assertTrue(verify_password(password, persisted["BUILDER_ADMIN_PASSWORD_HASH"]))
        self.assertGreaterEqual(len(persisted["FLASK_SECRET_KEY"]), 64)
        self.assertEqual(self.client.get("/setup").status_code, 404)

    def test_http_setup_cookie_is_not_forced_secure(self):
        self.assertFalse(module.app.config["SESSION_COOKIE_SECURE"])
        response = self.client.get("/setup")
        session_cookie = response.headers.get("Set-Cookie", "")
        self.assertIn("glpi_builder_session=", session_cookie)
        self.assertNotIn("; Secure", session_cookie)
        self.assertIn(b'pattern="[A-Za-z0-9_.\\-]{3,64}"', response.data)

    def test_setup_explains_single_synology_scheduler_task(self):
        response = self.client.get("/setup")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Scheduled backups", response.data)
        self.assertIn(b"Control Panel", response.data)
        self.assertIn(b"Task Scheduler", response.data)
        self.assertIn(b"User-defined script", response.data)
        self.assertIn(b"every <strong>5 minutes</strong>", response.data)
        self.assertIn(b"Copy command", response.data)
        self.assertIn(
            b"/bin/bash /volume1/docker/_BACKUPS/Synology_task_scheduler/Application_backup_dispatcher.sh",
            response.data,
        )
        self.assertIn(b"Optional now", response.data)
        self.assertIn(b"Check task status", response.data)
        self.assertIn(b'<script src="/assets/app.js" defer></script>', response.data)
        script = self.client.get("/assets/app.js")
        self.assertEqual(script.status_code, 200)
        self.assertIn(b"data-copy-command", script.data)

    def test_setup_head_request_is_read_only(self):
        response = self.client.head("/setup")
        self.assertEqual(response.status_code, 200)

    def test_invalid_existing_auth_explains_safe_recovery_and_has_no_setup_token(self):
        self.config_path.write_text("{broken", encoding="utf-8")
        with patch.object(module, "AUTH_CONFIG_ERROR", "Unable to load persisted authentication configuration"):
            response = self.client.get("/setup")
            login = self.client.get("/login")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(login.status_code, 503)
        self.assertIn(b"did not generate a setup token", response.data)
        self.assertIn(b"reset_setup_on_synology.sh --confirm-reset", response.data)
        self.assertIn(b"timestamped backup", response.data)

    def test_setup_rejects_bad_csrf_password_confirmation_and_totp(self):
        self.client.get("/setup")
        with self.client.session_transaction() as setup_session:
            csrf = setup_session["csrf_token"]
        response = self.client.post("/setup", data={
            "csrf_token": csrf,
            "setup_token": "test-setup-token",
            "username": "builder-admin",
            "password": "correct horse battery staple",
            "confirm_password": "different password value",
            "totp": "000000",
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.config_path.exists())


if __name__ == "__main__":
    unittest.main()
