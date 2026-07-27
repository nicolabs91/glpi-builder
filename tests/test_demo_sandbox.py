#!/usr/bin/env python3
import re
import unittest
from unittest.mock import patch

import app as module
from tests.auth_test_support import configure_auth


class DemoSandboxTest(unittest.TestCase):
    def setUp(self):
        module.app.config.update(TESTING=True, SECRET_KEY="demo-sandbox-test")
        configure_auth(module)
        self.client = module.app.test_client()
        self.original_preview_mode = module.BUILDER_TEST_PREVIEW_MODE
        module.BUILDER_TEST_PREVIEW_MODE = True
        self.client.get("/test-preview/enter")
        self.docker_snapshot = {
            "containers": [],
            "image_tags": (),
        }

    def tearDown(self):
        module.BUILDER_TEST_PREVIEW_MODE = self.original_preview_mode

    def preview_data(self):
        return patch.multiple(
            module,
            dashboard_docker_snapshot=lambda: self.docker_snapshot,
            discover_projects=lambda _containers=None: [],
            discover_unmanaged_glpi_projects=lambda *_args, **_kwargs: [],
        )

    def test_seeded_projects_backups_activity_and_yaml_are_available(self):
        with self.preview_data():
            projects = self.client.get("/projects")
            backups = self.client.get("/backups")
            activity = self.client.get("/activity")
            yaml_page = self.client.get(
                "/projects/demo-production/compose"
            )
        self.assertEqual(projects.status_code, 200)
        self.assertIn(b"demo-production", projects.data)
        self.assertIn(b"demo-recovery", projects.data)
        self.assertIn(b"Simulated", projects.data)
        self.assertIn(b"demo-production.sql.gz", backups.data)
        self.assertIn(b"Scheduled backup completed", activity.data)
        self.assertIn(b"Simulated configuration", yaml_page.data)
        self.assertIn(b"${GLPI_DB_PASSWORD}", yaml_page.data)

    def test_wizard_adds_only_a_session_scoped_simulated_project(self):
        with self.client.session_transaction() as current_session:
            csrf_token = current_session["csrf_token"]
        form = {
            "csrf_token": csrf_token,
            "backup_root": "/demo-only/backups",
            "container_port": "8080",
            "operation_mode": "fresh",
            "project": "demo-created",
            "host_port": "8099",
            "glpi_image": "glpi/glpi:11.0.8",
            "mariadb_image": "mariadb:11.4",
            "tz": "Europe/Brussels",
            "cookie_samesite": "Lax",
            "cookie_secure": "Off",
        }
        with self.preview_data(), patch.object(module, "run_create_job") as real_job:
            preview = self.client.post("/create", data=form)
            match = re.search(
                rb'name="preview_token" value="([^"]+)"',
                preview.data,
            )
            self.assertIsNotNone(match)
            execution = self.client.post(
                "/create/execute",
                data={
                    "csrf_token": csrf_token,
                    "preview_token": match.group(1).decode(),
                },
            )
            detail = self.client.get(execution.headers["Location"])
        self.assertEqual(preview.status_code, 200)
        self.assertIn(b"Simulation only", preview.data)
        self.assertIn(b"None - simulation only", preview.data)
        self.assertIn(b"Add simulated project", preview.data)
        self.assertEqual(execution.status_code, 302)
        self.assertIn("/projects/demo-created", execution.headers["Location"])
        self.assertEqual(detail.status_code, 200)
        self.assertIn(b"Simulated test project", detail.data)
        real_job.assert_not_called()
        with self.client.session_transaction() as current_session:
            self.assertEqual(
                current_session["test_demo_projects"][0]["name"],
                "demo-created",
            )

    def test_demo_data_is_not_present_outside_preview_session(self):
        self.client.get("/test-preview/exit")
        with self.preview_data():
            response = self.client.get("/projects")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_demo_schedule_changes_are_session_only(self):
        with self.client.session_transaction() as current_session:
            csrf_token = current_session["csrf_token"]
        response = self.client.post(
            "/set-backup-source",
            data={
                "csrf_token": csrf_token,
                "project": "demo-staging",
                "schedule_enabled": "yes",
                "schedule_kind": "weekly",
                "schedule_time": "03:30",
                "schedule_weekdays": "1,5",
                "interval_hours": "24",
                "retention_days": "30",
            },
        )
        self.assertEqual(response.status_code, 302)
        with self.preview_data():
            detail = self.client.get("/projects/demo-staging")
        self.assertIn(b"Selected weekdays", detail.data)
        self.assertIn(b'value="03:30"', detail.data)
        with self.client.session_transaction() as current_session:
            self.assertEqual(
                current_session["test_demo_schedule_overrides"]["demo-staging"]["weekdays"],
                "1,5",
            )

    def test_tampered_demo_values_are_rejected_before_session_storage(self):
        with self.client.session_transaction() as current_session:
            csrf_token = current_session["csrf_token"]
        form = {
            "csrf_token": csrf_token,
            "operation_mode": "fresh",
            "project": "demo-tampered",
            "host_port": "8099",
            "glpi_image": "glpi/glpi:" + ("x" * 5000),
            "mariadb_image": "mariadb:11.4",
            "tz": "Europe/Brussels",
            "cookie_samesite": "Lax",
            "cookie_secure": "Off",
        }
        with self.preview_data():
            response = self.client.post("/create", data=form)
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as current_session:
            self.assertNotIn("pending_create_preview", current_session)

    def test_every_database_image_shown_by_demo_wizard_is_accepted(self):
        base = {
            "operation_mode": "fresh",
            "project": "demo-image",
            "host_port": "8099",
            "glpi_image": "glpi/glpi:11.0.8",
            "tz": "Europe/Brussels",
            "cookie_samesite": "Lax",
            "cookie_secure": "Off",
        }
        for image in ("mariadb:10.11", "mariadb:11.4"):
            with self.subTest(image=image):
                with module.app.test_request_context("/"):
                    result = module.validate_demo_create_request({
                        **base,
                        "mariadb_image": image,
                    })
                self.assertEqual(result["mariadb_image"], image)


if __name__ == "__main__":
    unittest.main()
