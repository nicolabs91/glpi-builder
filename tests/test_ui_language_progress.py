#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as module


class UiLanguageAndProgressTest(unittest.TestCase):
    def setUp(self):
        module.app.config.update(TESTING=True, SECRET_KEY="ui-test-secret")
        with module.PROGRESS_LOCK:
            module.PROGRESS_JOBS.clear()
        self.client = module.app.test_client()

    def test_dashboard_is_english(self):
        with patch.object(module, "discover_projects", return_value=[]), \
             patch.object(module, "scan_files", return_value=[]), \
             patch.object(module, "local_image_tags", side_effect=lambda kind: ["glpi/glpi:test"] if kind == "glpi" else ["mariadb:test"]):
            response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Projects", response.data)
        self.assertIn(b"New project or restore", response.data)
        self.assertIn(b"Review plan", response.data)
        self.assertIn(b"row row-project", response.data)
        self.assertIn(b"row row-settings", response.data)
        self.assertIn(b"compact-control", response.data)
        self.assertIn(b"font:inherit", response.data)
        self.assertNotIn(b"Projecten", response.data)
        self.assertNotIn(b"Nieuw project", response.data)

    def test_running_progress_page_refreshes_and_lists_real_stage(self):
        token = module.create_progress_job("glpi-progress-test", module.BACKUP_ROOT)
        module.update_progress_job(
            token,
            percent=57,
            stage="Restoring database",
            message="Importing the selected database backup.",
            status="running",
        )

        response = self.client.get(f"/progress/{token}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'http-equiv="refresh"', response.data)
        self.assertIn(b"Restoring database", response.data)
        self.assertIn(b"57%", response.data)

    def test_completed_progress_page_stops_refreshing(self):
        token = module.create_progress_job("glpi-progress-test", module.BACKUP_ROOT)
        module.update_progress_job(token, 100, "Completed", status="completed")

        response = self.client.get(f"/progress/{token}")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'http-equiv="refresh"', response.data)
        self.assertIn(b"100%", response.data)


if __name__ == "__main__":
    unittest.main()
