import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app as module
from tests.auth_test_support import authenticate


class ApplicationRouteTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.base = Path(self.temporary_directory.name)
        module.app.config.update(TESTING=True, SECRET_KEY="application-route-test")
        self.client = module.app.test_client()
        authenticate(self.client, module)

    def csrf(self):
        with self.client.session_transaction() as state:
            return state["csrf_token"]

    def payload(self):
        return {
            "csrf_token": self.csrf(),
            "app_type": "n8n",
            "project": "n8n-production",
            "host_port": "18775",
            "image": "docker.n8n.io/n8nio/n8n:latest",
            "timezone": "Europe/Brussels",
        }

    @patch.object(module, "assert_docker_port_free")
    def test_preview_is_review_first_and_does_not_write_files(self, _port):
        with patch.object(module, "BASE_PATH", self.base):
            response = self.client.post("/applications/create", data=self.payload())
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Review deployment", response.data)
        self.assertFalse((self.base / "n8n-production").exists())

    @patch.object(module, "assert_docker_port_free")
    @patch.object(module.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
    def test_confirmed_deployment_writes_private_owned_project(self, _run, _port):
        with patch.object(module, "BASE_PATH", self.base):
            preview = self.client.post("/applications/create", data=self.payload())
            self.assertEqual(preview.status_code, 200)
            with self.client.session_transaction() as state:
                token = state["pending_application_preview"]["token"]
            response = self.client.post(
                "/applications/create/execute",
                data={"csrf_token": self.csrf(), "preview_token": token},
            )
        self.assertEqual(response.status_code, 302)
        folder = self.base / "n8n-production"
        self.assertTrue((folder / ".builder-app.json").is_file())
        self.assertEqual((folder / ".env").stat().st_mode & 0o777, 0o600)
        compose = (folder / "docker-compose.yml").read_text(encoding="utf-8")
        environment = (folder / ".env").read_text(encoding="utf-8")
        encryption_line = next(line for line in environment.splitlines() if line.startswith("N8N_ENCRYPTION_KEY="))
        self.assertNotIn(encryption_line.split("=", 1)[1], compose)
        self.assertEqual(_run.call_count, 2)

    @patch.object(module, "assert_docker_port_free")
    def test_existing_directory_is_never_adopted(self, _port):
        (self.base / "n8n-production").mkdir()
        with patch.object(module, "BASE_PATH", self.base):
            response = self.client.post("/applications/create", data=self.payload())
        self.assertEqual(response.status_code, 302)

    @patch.object(module.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
    def test_lifecycle_requires_valid_owned_manifest(self, run):
        profile = module.get_profile("n8n")
        with patch.object(module, "BASE_PATH", self.base):
            module.write_private_application_files({
                "app_type": profile.key, "project": "n8n-production",
                "host_port": 18775, "image": profile.default_image,
                "timezone": "Europe/Brussels",
            })
            response = self.client.post("/applications/action", data={
                "csrf_token": self.csrf(), "project": "n8n-production", "action": "restart",
            })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(run.call_args.args[0], ["docker", "compose", "restart"])

    @patch.object(module.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
    def test_unowned_directory_cannot_receive_lifecycle_action(self, run):
        (self.base / "other-project").mkdir()
        with patch.object(module, "BASE_PATH", self.base):
            response = self.client.post("/applications/action", data={
                "csrf_token": self.csrf(), "project": "other-project", "action": "stop",
            })
        self.assertEqual(response.status_code, 302)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
