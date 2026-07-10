#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as module


class PreviewFlowTest(unittest.TestCase):
    def setUp(self):
        module.app.config.update(TESTING=True, SECRET_KEY="preview-test-secret")
        self.client = module.app.test_client()
        with self.client.session_transaction() as flask_session:
            flask_session["csrf_token"] = "csrf-test"

    def payload(self):
        return {
            "csrf_token": "csrf-test",
            "backup_root": str(module.BACKUP_ROOT),
            "container_port": "8080",
            "project": "glpi-preview-test",
            "host_port": "18775",
            "glpi_image": "glpi/glpi:11-test",
            "mariadb_image": "mariadb:11-test",
            "tz": "Europe/Brussels",
            "cookie_samesite": "Lax",
            "cookie_secure": "Off",
            "force_recreate": "yes",
        }

    def validation_patches(self):
        return (
            patch.object(module, "validate_local_image", side_effect=lambda image, _kind: image),
            patch.object(module, "project_has_existing_state", return_value=False),
            patch.object(module, "get_container", return_value=None),
            patch.object(module, "assert_docker_port_free"),
        )

    def test_create_first_renders_preview_without_mutation(self):
        patches = self.validation_patches()
        with patches[0], patches[1], patches[2], patches[3], \
             patch.object(module, "ensure_dirs") as ensure_dirs:
            response = self.client.post("/create", data=self.payload())

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Controleer het uitvoerplan", response.data)
        self.assertIn(b"glpi-preview-test", response.data)
        ensure_dirs.assert_not_called()
        with self.client.session_transaction() as flask_session:
            self.assertIn("pending_create_preview", flask_session)

    def test_confirmed_preview_executes_once_and_is_consumed(self):
        patches = self.validation_patches()
        with patches[0], patches[1], patches[2], patches[3]:
            preview_response = self.client.post("/create", data=self.payload())
        self.assertEqual(preview_response.status_code, 200)

        with self.client.session_transaction() as flask_session:
            token = flask_session["pending_create_preview"]["token"]

        env = {
            "GLPI_DB_NAME": "glpi",
            "GLPI_SESSION_COOKIE_SAMESITE": "Lax",
            "GLPI_SESSION_COOKIE_SECURE": "Off",
        }
        patches = self.validation_patches()
        with patches[0], patches[1], patches[2], patches[3], \
             patch.object(module, "ensure_dirs"), \
             patch.object(module, "build_env", return_value=env), \
             patch.object(module, "write_env"), \
             patch.object(module, "write_compose"), \
             patch.object(module, "create_or_restore", return_value=["uitgevoerd"]) as execute, \
             patch.object(module, "flash_action_success"):
            response = self.client.post(
                "/create/execute",
                data={"csrf_token": "csrf-test", "preview_token": token},
            )

        self.assertEqual(response.status_code, 302)
        execute.assert_called_once()
        with self.client.session_transaction() as flask_session:
            self.assertNotIn("pending_create_preview", flask_session)


if __name__ == "__main__":
    unittest.main()
