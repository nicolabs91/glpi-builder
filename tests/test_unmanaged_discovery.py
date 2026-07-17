import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("builder_app_unmanaged", ROOT / "app.py")
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)
from tests.auth_test_support import authenticate


class FakeContainer:
    def __init__(self, name, image, project, status="running", ports=None, env=None, mounts=None, tags=None):
        self.name = name
        self.status = status
        self.image = type("Image", (), {"tags": tags or [], "attrs": {"RepoTags": tags or []}})()
        self.attrs = {
            "Config": {
                "Image": image,
                "Labels": {"com.docker.compose.project": project} if project else {},
                "Env": env or [],
            },
            "NetworkSettings": {"Ports": ports or {}},
            "HostConfig": {"PortBindings": ports or {}},
            "Mounts": mounts or [],
        }


class MissingImageContainer:
    name = "old-builder-rollback"
    status = "exited"
    attrs = {
        "Config": {"Image": "glpi-builder:latest", "Labels": {}, "Env": []},
        "NetworkSettings": {"Ports": {}},
        "HostConfig": {"PortBindings": {}},
        "Mounts": [],
    }

    @property
    def image(self):
        raise module.docker.errors.ImageNotFound("deleted rollback image")


class UnmanagedDiscoveryTests(unittest.TestCase):
    def test_deleted_rollback_image_metadata_does_not_break_discovery(self):
        rollback = MissingImageContainer()

        self.assertEqual(
            module.container_image_references(rollback),
            ["glpi-builder:latest"],
        )
        self.assertEqual(module.discover_unmanaged_glpi_projects([rollback], []), [])

    def test_running_glpi_compose_project_is_visible_with_reasons(self):
        glpi = FakeContainer(
            "glpi-prod-1108",
            "glpi/glpi:11.0.8",
            "glpi-prod-1108",
            ports={"80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8775"}]},
        )
        database = FakeContainer(
            "glpi-prod-1108-db", "mariadb:11.2.2", "glpi-prod-1108"
        )
        with tempfile.TemporaryDirectory() as temporary_directory, \
             patch.object(module, "BASE_PATH", Path(temporary_directory)):
            found = module.discover_unmanaged_glpi_projects([glpi, database], [])

        self.assertEqual([item["name"] for item in found], ["glpi-prod-1108"])
        self.assertEqual(found[0]["active_port"], "8775")
        self.assertEqual(found[0]["glpi_image"], "glpi/glpi:11.0.8")
        self.assertEqual(found[0]["mariadb_image"], "mariadb:11.2.2")
        self.assertIn("Project directory", found[0]["issues"][0])

    def test_managed_project_is_not_duplicated(self):
        glpi = FakeContainer("glpi-prod-1108", "glpi/glpi:11.0.8", "glpi-prod-1108")
        managed = [{"name": "glpi-prod-1108"}]
        self.assertEqual(module.discover_unmanaged_glpi_projects([glpi], managed), [])

    def test_dangling_glpi_image_and_nonstandard_db_name_are_detected(self):
        glpi = FakeContainer(
            "glpi1107-test",
            "sha256:old-untagged-image",
            "",
            env=["GLPI_DB_HOST=glpi1107-db-test", "GLPI_DB_NAME=glpi"],
            mounts=[{"Destination": "/var/glpi"}],
            ports={"80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8774"}]},
        )
        database = FakeContainer(
            "glpi1107-db-test", "mariadb:12.2.2", ""
        )
        with tempfile.TemporaryDirectory() as temporary_directory, \
             patch.object(module, "BASE_PATH", Path(temporary_directory)):
            found = module.discover_unmanaged_glpi_projects([glpi, database], [])

        self.assertEqual([item["name"] for item in found], ["glpi1107-test"])
        self.assertEqual(found[0]["db_status"], "running")
        self.assertEqual(found[0]["active_port"], "8774")
        self.assertEqual(found[0]["glpi_image"], "sha256:old-untagged-image")
        self.assertEqual(found[0]["mariadb_image"], "mariadb:12.2.2")

    def test_dangling_named_container_without_glpi_structure_is_ignored(self):
        unrelated = FakeContainer("glpi-metrics", "sha256:untagged", "")
        self.assertEqual(module.discover_unmanaged_glpi_projects([unrelated], []), [])

    def test_database_only_and_unrelated_containers_are_ignored(self):
        database = FakeContainer("accounting-db", "mariadb:11.2.2", "accounting")
        nginx = FakeContainer("website", "nginx:latest", "website")
        self.assertEqual(module.discover_unmanaged_glpi_projects([database, nginx], []), [])

    def test_dashboard_renders_detected_project_without_management_actions(self):
        detected = {
            "name": "glpi-prod-1108", "path": "/volume1/docker/glpi-prod-1108",
            "glpi_status": "running", "db_status": "running",
            "glpi_image": "glpi/glpi:11.0.8", "mariadb_image": "mariadb:11.2.2",
            "active_port": "8775", "mappings": "0.0.0.0:8775->80/tcp",
            "issues": ["Builder .env file is missing."],
        }
        with patch.object(module, "dashboard_docker_snapshot", return_value={"containers": [], "image_tags": ()}), \
             patch.object(module, "discover_projects", return_value=[]), \
             patch.object(module, "discover_unmanaged_glpi_projects", return_value=[detected]), \
             patch.object(module, "scan_backup_choices", return_value={"database": [], "files": []}), \
             patch.object(module, "suggest_free_host_port", return_value=8776):
            client = module.app.test_client()
            authenticate(client, module)
            response = client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Detected but not managed", response.data)
        self.assertIn(b"glpi-prod-1108", response.data)
        self.assertIn(b"Builder .env file is missing", response.data)
        self.assertNotIn(b'id="manage-glpi-prod-1108"', response.data)


if __name__ == "__main__":
    unittest.main()
