#!/usr/bin/env python3
import unittest
from unittest.mock import patch

import app as module
from tests.auth_test_support import configure_auth


class TestPreviewModeTest(unittest.TestCase):
    def setUp(self):
        module.app.config.update(TESTING=True, SECRET_KEY="test-preview-mode")
        configure_auth(module)
        self.client = module.app.test_client()
        self.original_preview_mode = module.BUILDER_TEST_PREVIEW_MODE

    def tearDown(self):
        module.BUILDER_TEST_PREVIEW_MODE = self.original_preview_mode

    def test_preview_controls_and_routes_are_absent_by_default(self):
        module.BUILDER_TEST_PREVIEW_MODE = False
        response = self.client.get("/login")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"Local test preview", response.data)
        self.assertEqual(self.client.get("/test-preview/enter").status_code, 404)
        self.assertEqual(self.client.get("/test-preview/setup").status_code, 404)

    def test_preview_can_enter_builder_without_admin_session(self):
        module.BUILDER_TEST_PREVIEW_MODE = True
        login = self.client.get("/login")
        self.assertIn(b"Local test preview", login.data)
        self.assertIn(b"View Builder", login.data)
        with patch.object(
            module,
            "professional_ui_snapshot",
            return_value=({"containers": [], "image_tags": ()}, []),
        ), patch.object(
            module,
            "discover_unmanaged_glpi_projects",
            return_value=[],
        ):
            response = self.client.get("/test-preview/enter", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Infrastructure overview", response.data)
        self.assertIn(b"Read-only preview", response.data)
        self.assertIn(b"Exit preview", response.data)
        self.assertNotIn(b"Sign out", response.data)

    def test_preview_blocks_every_post_before_mutation_handling(self):
        module.BUILDER_TEST_PREVIEW_MODE = True
        self.client.get("/test-preview/enter")
        response = self.client.post("/logout", data={"csrf_token": "anything"})
        self.assertEqual(response.status_code, 403)
        self.assertIn(b"Changes are disabled", response.data)

    def test_preview_blocks_all_real_management_post_routes(self):
        module.BUILDER_TEST_PREVIEW_MODE = True
        self.client.get("/test-preview/enter")
        simulated_endpoints = {"create", "execute_create", "set_backup_source_route"}
        public_endpoints = {"login", "setup"}
        for rule in module.app.url_map.iter_rules():
            if "POST" not in rule.methods:
                continue
            if rule.endpoint in simulated_endpoints | public_endpoints:
                continue
            path = rule.rule
            path = path.replace("<project>", "demo-production")
            path = path.replace("<filename>", "example.log")
            with self.subTest(endpoint=rule.endpoint):
                response = self.client.post(path, data={"csrf_token": "anything"})
                self.assertEqual(response.status_code, 403)

    def test_setup_preview_is_read_only_and_uses_no_real_secret(self):
        module.BUILDER_TEST_PREVIEW_MODE = True
        response = self.client.get("/test-preview/setup")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Read-only test preview", response.data)
        self.assertIn(b"TEST-PREVIEW-SECRET-NOT-A-REAL-CREDENTIAL", response.data)
        self.assertIn(b"<fieldset disabled>", response.data)
        self.assertNotIn(
            str(module.AUTH_CONFIG.totp_secret).encode(),
            response.data,
        )

    def test_exit_preview_clears_preview_session(self):
        module.BUILDER_TEST_PREVIEW_MODE = True
        self.client.get("/test-preview/enter")
        response = self.client.get("/test-preview/exit")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/login"))
        with self.client.session_transaction() as current_session:
            self.assertNotIn("test_preview_active", current_session)


if __name__ == "__main__":
    unittest.main()
