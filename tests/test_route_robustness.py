#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as module
from tests.auth_test_support import authenticate


class RouteRobustnessTest(unittest.TestCase):
    def setUp(self):
        self.previous_testing = module.app.config.get("TESTING")
        self.previous_propagate = module.app.config.get("PROPAGATE_EXCEPTIONS")
        module.app.config.update(
            TESTING=False,
            PROPAGATE_EXCEPTIONS=False,
            SECRET_KEY="route-robustness-secret",
        )
        self.client = module.app.test_client()
        authenticate(self.client, module)

    def tearDown(self):
        module.app.config.update(
            TESTING=self.previous_testing,
            PROPAGATE_EXCEPTIONS=self.previous_propagate,
        )

    def test_invalid_or_missing_log_files_are_clean_404_responses(self):
        responses = (
            self.client.get("/logs/glpi-test/not-a-valid-log.log"),
            self.client.get("/logs/glpi-test/20260711-120000-missing.log"),
            self.client.get("/logs/INVALID/20260711-120000-missing.log"),
        )

        for response in responses:
            self.assertEqual(response.status_code, 404)
            self.assertNotIn(b"Internal Server Error", response.data)
            self.assertEqual(response.headers.get("X-Content-Type-Options"), "nosniff")

    def test_invalid_progress_token_redirects_without_server_error(self):
        response = self.client.get("/progress/not-a-real-progress-token")

        self.assertEqual(response.status_code, 302)

    def test_favicon_is_an_empty_success_response(self):
        response = self.client.get("/favicon.ico")
        self.assertEqual(response.status_code, 204)
        self.assertNotIn(b"Internal Server Error", response.data)

    def test_post_routes_reject_missing_csrf_without_server_error(self):
        routes = (
            "/create",
            "/create/execute",
            "/change-port",
            "/change-cookie",
            "/set-backup-source",
            "/run-backup",
            "/rebuild-glpi",
            "/diagnose",
            "/testdb",
            "/resetdb",
        )

        for route in routes:
            with self.subTest(route=route):
                response = self.client.post(route, data={})
                self.assertEqual(response.status_code, 302)
                self.assertNotIn(b"Internal Server Error", response.data)

    def test_second_mutation_is_rejected_while_lock_is_held(self):
        self.assertTrue(module.MUTATION_LOCK.acquire(blocking=False))
        try:
            response = self.client.post("/change-port", data={})
        finally:
            module.MUTATION_LOCK.release()

        self.assertEqual(response.status_code, 409)
        self.assertIn(b"Another administration action is already running", response.data)

    def test_rebuild_requires_exact_typed_project_confirmation(self):
        with patch.object(module, "rebuild_glpi") as rebuild:
            self.client.post("/rebuild-glpi", data={
                "csrf_token": "test-csrf-token",
                "project": "glpi-test",
                "confirm_rebuild": "wrong",
            })
            rebuild.assert_not_called()
            self.client.post("/rebuild-glpi", data={
                "csrf_token": "test-csrf-token",
                "project": "glpi-test",
                "confirm_rebuild": "glpi-test",
            })
            rebuild.assert_called_once_with("glpi-test")

    def test_database_recovery_requires_checkbox_and_typed_name(self):
        with patch.object(module, "reset_db_user", return_value=(True, "ok")) as reset:
            self.client.post("/resetdb", data={
                "csrf_token": "test-csrf-token",
                "project": "glpi-test",
                "confirm_resetdb": "yes",
                "confirm_project": "wrong",
            })
            reset.assert_not_called()
            self.client.post("/resetdb", data={
                "csrf_token": "test-csrf-token",
                "project": "glpi-test",
                "confirm_resetdb": "yes",
                "confirm_project": "glpi-test",
            })
            reset.assert_called_once_with("glpi-test")

    def test_dashboard_dependency_failure_degrades_without_500(self):
        with patch.object(
            module,
            "dashboard_docker_snapshot",
            return_value={"containers": [], "image_tags": ()},
        ), patch.object(
            module,
            "scan_backup_choices",
            return_value={"database": [], "files": []},
        ), patch.object(module, "discover_projects", return_value=[]):
            response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"No managed applications yet", response.data)
        self.assertIn(b"Available images", response.data)
        self.assertNotIn(b"Internal Server Error", response.data)


if __name__ == "__main__":
    unittest.main()
