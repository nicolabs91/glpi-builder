#!/usr/bin/env python3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as module
from tests.auth_test_support import authenticate


class UiLanguageAndProgressTest(unittest.TestCase):
    def setUp(self):
        module.app.config.update(TESTING=True, SECRET_KEY="ui-test-secret")
        with module.PROGRESS_LOCK:
            module.PROGRESS_JOBS.clear()
        self.snapshot_patcher = patch.object(
            module,
            "dashboard_docker_snapshot",
            return_value={"containers": [], "image_tags": ("glpi/glpi:test", "mariadb:test")},
        )
        self.snapshot_patcher.start()
        self.addCleanup(self.snapshot_patcher.stop)
        self.client = module.app.test_client()
        authenticate(self.client, module)

    def test_dashboard_is_english(self):
        self.assertEqual(module.APP_VERSION, "0.1.1-rc.1")
        with patch.object(module, "discover_projects", return_value=[]), \
             patch.object(module, "scan_backup_choices", return_value={"database": [], "files": []}), \
             patch.object(module, "suggest_free_host_port", return_value=18888), \
             patch.object(module, "local_image_tags", side_effect=lambda kind, *_: ["glpi/glpi:test"] if kind == "glpi" else ["mariadb:test"]):
            response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Projects", response.data)
        self.assertIn(b"New Project or Restore", response.data)
        self.assertEqual(response.data.count(b"New Project or Restore"), 1)
        self.assertNotIn(b"New project or restore", response.data)
        self.assertIn(b"<h3>Project</h3>", response.data)
        self.assertIn(b"<h3>Versions</h3>", response.data)
        self.assertIn(b"<h3>Mode and required backups</h3>", response.data)
        self.assertIn(b"<h3>Review and execute</h3>", response.data)
        self.assertNotIn(b"<h3>1. Project</h3>", response.data)
        self.assertNotIn(b"<h3>2. Versions</h3>", response.data)
        self.assertNotIn(b"<h3>3. Mode and required backups</h3>", response.data)
        self.assertNotIn(b"<h3>4. Review and execute</h3>", response.data)
        self.assertNotIn(b'href="#new-project"', response.data)
        self.assertIn(b"Review plan", response.data)
        self.assertIn(b'class="review-button">Review plan', response.data)
        self.assertIn(b".review-button{margin-top:16px}", response.data)
        self.assertIn(b"row row-project", response.data)
        self.assertIn(b"row row-settings", response.data)
        self.assertIn(b'name="host_port" min="1" max="65535" value="18888"', response.data)
        self.assertIn(b"compact-control", response.data)
        self.assertIn(b"font:inherit", response.data)
        self.assertIn(b'value="restore" checked', response.data)
        self.assertIn(b"Full restore", response.data)
        self.assertIn(b"Fresh installation", response.data)
        self.assertNotIn(b"Full restore (standard)", response.data)
        self.assertNotIn(b"Fresh installation (rare)", response.data)
        self.assertIn(b"Restore without plugins", response.data)
        self.assertIn(b"Overwrite existing project", response.data)
        self.assertIn(b'name="update_backup_source" value="yes"', response.data)
        self.assertNotIn(b'name="update_backup_source" value="yes" checked', response.data)
        self.assertIn(b"Use this project for scheduled backups", response.data)
        self.assertIn(b'class="overwrite-confirmation"', response.data)
        self.assertIn(b".overwrite-confirmation{display:none}", response.data)
        self.assertIn(b"#overwrite-existing:checked", response.data)
        self.assertIn(b"Type the project name to confirm overwrite", response.data)
        self.assertNotIn(b"For an existing project, type the project name to confirm", response.data)
        self.assertNotIn(b"I understand that an existing project name", response.data)
        self.assertIn(b'class="version">0.1.1-rc.1</div>', response.data)
        self.assertNotIn(b"internal administration tool", response.data)
        self.assertNotIn(b"Status, web port and recent administration activity", response.data)
        self.assertNotIn(b"Complete these four steps", response.data)
        self.assertNotIn(b"Used in normal operation", response.data)
        self.assertNotIn(b"Deletes database, config, files and plugins", response.data)
        self.assertNotIn(b"Full restore is the default", response.data)
        self.assertNotIn(b"GLPI config and files are restored", response.data)
        self.assertNotIn(b"Nothing is changed until", response.data)
        self.assertIn(b'<html lang="en">', response.data)

    def test_project_card_uses_the_native_management_section_without_manage_link(self):
        project = type(
            "Project",
            (),
            {
                "name": "glpi-existing",
                "glpi_status": "running",
                "db_status": "running",
                "active_port": "8775",
                "backup_source": True,
                "backup_status": {
                    "selected": True,
                    "ready": True,
                    "issues": [],
                    "latest": {
                        "name": "GLPI_Backup_20260711-120000",
                        "created_at": "2026-07-11T12:00:00+0200",
                        "size_label": "13 B",
                        "checksum_manifest": True,
                    },
                },
                "cookie_samesite": "Lax",
                "cookie_secure": "Off",
                "logs": [],
            },
        )()
        project_without_backup = type(
            "Project",
            (),
            {
                "name": "glpi-secondary",
                "glpi_status": "not present",
                "db_status": "created",
                "active_port": "8776",
                "backup_source": False,
                "backup_status": {
                    "selected": False,
                    "ready": False,
                    "issues": [],
                    "latest": None,
                },
                "cookie_samesite": "Lax",
                "cookie_secure": "Off",
                "logs": [],
                "glpi_image": "glpi/glpi:test",
                "mariadb_image": "mariadb:test",
            },
        )()
        with patch.object(module, "discover_projects", return_value=[project, project_without_backup]), \
             patch.object(module, "scan_backup_choices", return_value={"database": [], "files": []}), \
             patch.object(module, "suggest_free_host_port", return_value=8776), \
             patch.object(module, "local_image_tags", side_effect=lambda kind, *_: ["glpi/glpi:test"] if kind == "glpi" else ["mariadb:test"]):
            response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Project management", response.data)
        self.assertIn(b"Backup ready", response.data)
        self.assertIn(b"Run backup now", response.data)
        self.assertIn(b"GLPI_Backup_20260711-120000", response.data)
        self.assertNotIn(b"Not selected", response.data)
        self.assertIn(b"Backup source", response.data)
        self.assertLess(response.data.index(b"Backup source"), response.data.index(b"Web port:"))
        self.assertLess(response.data.index(b"Project management"), response.data.index(b"Scheduled backup"))
        self.assertIn(b"Use for scheduled backups", response.data)
        self.assertIn(b"Recreate GLPI container", response.data)
        self.assertNotIn(b"Reapply GLPI container", response.data)
        self.assertIn(b"Change port + recreate container", response.data)
        self.assertNotIn(b">Apply port</button>", response.data)
        self.assertIn(b'id="manage-glpi-existing"', response.data)
        self.assertNotIn(b'href="#manage-glpi-existing"', response.data)
        self.assertIn(
            b'details[id^="manage-"] button{padding:7px 10px;font-size:13px;border-radius:7px}',
            response.data,
        )
        self.assertIn(
            b'details[id^="manage-"] > form + form{margin-top:6px}',
            response.data,
        )

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
        self.assertIn(b"<div>0.1.1-rc.1</div>", response.data)
        self.assertNotIn(b"0.2 \xc2\xb7 project", response.data)

    def test_local_ui_preview_button_is_explicitly_gated(self):
        with patch.object(module, "UI_PREVIEW_MODE", True), \
             patch.object(module, "discover_projects", return_value=[]), \
             patch.object(module, "scan_backup_choices", return_value={"database": [], "files": []}), \
             patch.object(module, "local_image_tags", side_effect=lambda kind, *_: ["glpi/glpi:test"] if kind == "glpi" else ["mariadb:test"]):
            response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'href="/ui-preview"', response.data)
        self.assertIn(b"Preview review screen", response.data)

    def test_first_free_docker_port_is_suggested(self):
        class Containers:
            @staticmethod
            def list(all=True):
                return [type("Container", (), {"name": "one"})(), type("Container", (), {"name": "two"})()]

        client = type("Client", (), {"containers": Containers()})()
        mappings = {
            "one": [{"host_port": "8775"}],
            "two": [{"host_port": "8776"}, {"host_port": "5055"}],
        }
        with patch.object(module, "docker_client", return_value=client), \
             patch.object(module, "container_port_mappings", side_effect=lambda container: mappings[container.name]):
            suggested = module.suggest_free_host_port()

        self.assertEqual(suggested, 8777)

    def test_stopped_container_port_binding_is_still_reserved(self):
        container = type(
            "Container",
            (),
            {
                "attrs": {
                    "NetworkSettings": {"Ports": {}},
                    "HostConfig": {
                        "PortBindings": {
                            "8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8775"}],
                        },
                    },
                },
                "reload": lambda self: None,
            },
        )()

        self.assertEqual(
            module.container_port_mappings(container),
            [{
                "private": "8080/tcp",
                "host_ip": "0.0.0.0",
                "host_port": "8775",
                "mapping": "0.0.0.0:8775->8080/tcp",
            }],
        )

    def test_project_env_port_is_reserved_without_a_container(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base_path = Path(temporary_directory)
            project = "glpi-existing"
            with patch.object(module, "BASE_PATH", base_path):
                env = module.build_env(
                    project,
                    "glpi/glpi:test",
                    "mariadb:test",
                    8775,
                    8080,
                    "Europe/Brussels",
                    True,
                )
                module.ensure_dirs(project)
                module.write_env(project, env)
                module.write_compose(project, env)
            client = type(
                "Client",
                (),
                {"containers": type("Containers", (), {"list": staticmethod(lambda all=True: [])})()},
            )()

            with patch.object(module, "BASE_PATH", base_path), \
                 patch.object(module, "docker_client", return_value=client):
                with self.assertRaisesRegex(ValueError, "reserved by project glpi-existing"):
                    module.assert_docker_port_free(8775)
                self.assertEqual(module.suggest_free_host_port(), 8776)

    def test_project_discovery_rejects_non_glpi_env_directories(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base_path = Path(temporary_directory)
            valid_project = "glpi-managed"
            unrelated_project = "unrelated-app"

            with patch.object(module, "BASE_PATH", base_path):
                env = module.build_env(
                    valid_project,
                    "glpi/glpi:test",
                    "mariadb:test",
                    8775,
                    8080,
                    "Europe/Brussels",
                    True,
                )
                module.ensure_dirs(valid_project)
                module.write_env(valid_project, env)
                module.write_compose(valid_project, env)

                unrelated = base_path / unrelated_project
                unrelated.mkdir()
                (unrelated / ".env").write_text(
                    "PROJECT_NAME=unrelated-app\nIMAGE=nginx:latest\nPORT=8080\n",
                    encoding="utf-8",
                )

                containers = type("Containers", (), {"list": staticmethod(lambda all=True: [])})()
                client = type("Client", (), {"containers": containers})()
                with patch.object(module, "docker_client", return_value=client), \
                     patch.object(module, "get_container", return_value=None), \
                     patch.object(module, "current_backup_source_project", return_value=""):
                    projects = module.discover_projects()

            self.assertEqual([project["name"] for project in projects], [valid_project])
            self.assertTrue(module.is_managed_glpi_project(valid_project, base_path=base_path))
            self.assertFalse(module.is_managed_glpi_project(unrelated_project, base_path=base_path))

    def test_completed_progress_page_stops_refreshing(self):
        token = module.create_progress_job("glpi-progress-test", module.BACKUP_ROOT)
        module.update_progress_job(token, 100, "Completed", status="completed")

        response = self.client.get(f"/progress/{token}")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'http-equiv="refresh"', response.data)
        self.assertIn(b"100%", response.data)


if __name__ == "__main__":
    unittest.main()
