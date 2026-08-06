#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as module
from tests.auth_test_support import authenticate


class ImmediateThread:
    def __init__(self, target, args=(), **_kwargs):
        self.target = target
        self.args = args

    def start(self):
        self.target(*self.args)


class PreviewFlowTest(unittest.TestCase):
    def setUp(self):
        if module.MUTATION_LOCK.locked():
            module.MUTATION_LOCK.release()
        with module.PROGRESS_LOCK:
            module.PROGRESS_JOBS.clear()
        module.app.config.update(TESTING=True, SECRET_KEY="preview-test-secret")
        self.client = module.app.test_client()
        authenticate(self.client, module)
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
            "operation_mode": "fresh",
            "update_backup_source": "yes",
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
        self.assertIn(b"Review the execution plan", response.data)
        self.assertIn(b"<div>0.5.0-rc.3</div>", response.data)
        self.assertNotIn(b"nothing has been changed yet", response.data)
        self.assertIn(b"glpi-preview-test", response.data)
        self.assertIn(b"Fresh installation", response.data)
        self.assertNotIn(b"Fresh installation (rare)", response.data)
        self.assertNotIn(b"Execution order", response.data)
        self.assertNotIn(b"The preflight checks run again", response.data)
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
             patch.object(module, "create_or_restore", return_value=["completed"]) as execute, \
             patch.object(module, "configure_scheduled_backup", return_value=["backup configured"]) as configure_backup, \
             patch.object(module, "write_action_log", return_value="test-create-restore.log"), \
             patch.object(module.threading, "Thread", ImmediateThread):
            response = self.client.post(
                "/create/execute",
                data={"csrf_token": "csrf-test", "preview_token": token},
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/progress/", response.headers["Location"])
        execute.assert_called_once()
        configure_backup.assert_called_once()
        self.assertTrue(execute.call_args.kwargs["fresh_install"])
        self.assertTrue(execute.call_args.kwargs["skip_plugins"])
        progress_response = self.client.get(response.headers["Location"])
        self.assertEqual(progress_response.status_code, 200)
        self.assertIn(b"Completed", progress_response.data)
        self.assertIn(b"100%", progress_response.data)
        with self.client.session_transaction() as flask_session:
            self.assertNotIn("pending_create_preview", flask_session)

    def test_full_restore_rejects_missing_required_backups(self):
        payload = self.payload()
        payload["operation_mode"] = "restore"
        patches = self.validation_patches()
        with patches[0], patches[1], patches[2], patches[3], \
             patch.object(
                 module,
                 "dashboard_docker_snapshot",
                 return_value={"containers": [], "image_tags": ()},
             ), \
             patch.object(
                 module,
                 "scan_backup_choices",
                 return_value={"database": [], "files": []},
             ):
            response = self.client.post("/create", data=payload, follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Full restore requires a database backup", response.data)

    def test_obsolete_ui_preview_route_is_removed(self):
        response = self.client.get("/ui-preview")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
