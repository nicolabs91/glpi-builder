import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SynologyNameMigrationTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.old_dir = self.root / "glpi-builder"
        self.new_dir = self.root / "docker-app-manager"
        self.old_dir.mkdir()
        self.new_dir.mkdir()
        (self.old_dir / ".env").write_text(
            "BUILDER_AUTH_STATE_PATH=/volume1/docker/.glpi-builder-auth-state\n",
            encoding="utf-8",
        )
        (self.old_dir / "config").mkdir()
        (self.old_dir / "config/builder-auth.json").write_text(
            '{"secret":"preserved"}\n', encoding="utf-8"
        )
        (self.old_dir / "docker-compose.app.yml").write_text("services: {}\n", encoding="utf-8")
        (self.new_dir / "install_on_synology.sh").write_text(
            "#!/bin/sh\nprintf '%s\\n' installed > \"$1/install-result\"\n",
            encoding="utf-8",
        )
        (self.new_dir / "install_on_synology.sh").chmod(0o755)
        self.old_state = self.root / ".glpi-builder-auth-state"
        self.new_state = self.root / ".docker-app-manager-auth-state"
        self.old_state.write_text("42\n", encoding="utf-8")

    def tearDown(self):
        self.temporary_directory.cleanup()

    def run_migration(self):
        environment = os.environ.copy()
        environment.update(
            DOCKER_APP_MANAGER_MIGRATION_TESTING="1",
            MIGRATION_HEALTH_DELAY_SECONDS="0",
            GLPI_BUILDER_OLD_DIR=str(self.old_dir),
            DOCKER_APP_MANAGER_APP_DIR=str(self.new_dir),
            GLPI_BUILDER_AUTH_STATE_PATH=str(self.old_state),
            DOCKER_APP_MANAGER_AUTH_STATE_PATH=str(self.new_state),
        )
        return subprocess.run(
            ["sh", str(ROOT / "migrate_to_docker_app_manager.sh")],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )

    def install_fake_docker(self, health_succeeds=True):
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        calls = self.root / "docker-calls"
        docker = bin_dir / "docker"
        health_result = "exit 0" if health_succeeds else "exit 1"
        docker.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$*\" >> '{calls}'\n"
            "case \"$*\" in\n"
            "  'container inspect glpi-builder') exit 0 ;;\n"
            f"  'exec docker-app-manager'*) {health_result} ;;\n"
            "  *) exit 0 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        docker.chmod(0o755)
        return bin_dir, calls

    def test_preserves_authentication_and_starts_new_installation(self):
        result = self.run_migration()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (self.new_dir / "config/builder-auth.json").read_text(encoding="utf-8"),
            '{"secret":"preserved"}\n',
        )
        self.assertIn(
            "BUILDER_AUTH_STATE_PATH=/volume1/docker/.docker-app-manager-auth-state",
            (self.new_dir / ".env").read_text(encoding="utf-8"),
        )
        self.assertEqual(self.new_state.read_text(encoding="utf-8"), "42\n")
        self.assertTrue((self.new_dir / "install-result").exists())
        self.assertTrue((self.old_dir / "config/builder-auth.json").exists())

    def test_refuses_to_overwrite_new_authentication_config(self):
        (self.new_dir / "config").mkdir()
        result = self.run_migration()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to merge authentication data", result.stderr)
        self.assertFalse((self.new_dir / "install-result").exists())

    def test_refuses_symlinked_legacy_config(self):
        external = self.root / "external-config"
        self.old_dir.joinpath("config").rename(external)
        self.old_dir.joinpath("config").symlink_to(external, target_is_directory=True)
        result = self.run_migration()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must not be a symlink", result.stderr)
        self.assertFalse((self.new_dir / "config").exists())

    def test_container_manager_installation_uses_new_project_and_container_names(self):
        (self.old_dir / ".env").unlink()
        (self.old_dir / "docker-compose.container-manager.yml").write_text(
            "services: {}\n", encoding="utf-8"
        )
        (self.new_dir / "docker-compose.container-manager.yml").write_text(
            "services: {}\n", encoding="utf-8"
        )
        bin_dir, calls_path = self.install_fake_docker()
        environment = os.environ.copy()
        environment.update(
            PATH=str(bin_dir) + os.pathsep + environment["PATH"],
            DOCKER_APP_MANAGER_MIGRATION_TESTING="1",
            MIGRATION_HEALTH_DELAY_SECONDS="0",
            GLPI_BUILDER_OLD_DIR=str(self.old_dir),
            DOCKER_APP_MANAGER_APP_DIR=str(self.new_dir),
            GLPI_BUILDER_AUTH_STATE_PATH=str(self.old_state),
            DOCKER_APP_MANAGER_AUTH_STATE_PATH=str(self.new_state),
        )
        result = subprocess.run(
            ["sh", str(ROOT / "migrate_to_docker_app_manager.sh")],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = calls_path.read_text(encoding="utf-8")
        self.assertIn("compose --project-name docker-app-manager", calls)
        self.assertIn("rename glpi-builder docker-app-manager-pre-rename-", calls)
        self.assertIn("exec docker-app-manager", calls)
        self.assertTrue((self.new_dir / "config/builder-auth.json").exists())

    def test_failed_container_manager_health_restores_legacy_container(self):
        (self.old_dir / ".env").unlink()
        (self.old_dir / "docker-compose.container-manager.yml").write_text(
            "services: {}\n", encoding="utf-8"
        )
        (self.new_dir / "docker-compose.container-manager.yml").write_text(
            "services: {}\n", encoding="utf-8"
        )
        bin_dir, calls_path = self.install_fake_docker(health_succeeds=False)
        environment = os.environ.copy()
        environment.update(
            PATH=str(bin_dir) + os.pathsep + environment["PATH"],
            DOCKER_APP_MANAGER_MIGRATION_TESTING="1",
            MIGRATION_HEALTH_DELAY_SECONDS="0",
            GLPI_BUILDER_OLD_DIR=str(self.old_dir),
            DOCKER_APP_MANAGER_APP_DIR=str(self.new_dir),
            GLPI_BUILDER_AUTH_STATE_PATH=str(self.old_state),
            DOCKER_APP_MANAGER_AUTH_STATE_PATH=str(self.new_state),
        )
        result = subprocess.run(
            ["sh", str(ROOT / "migrate_to_docker_app_manager.sh")],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            timeout=30,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("legacy glpi-builder container was restored", result.stderr)
        calls = calls_path.read_text(encoding="utf-8")
        self.assertIn("rm -f docker-app-manager", calls)
        self.assertIn("rename docker-app-manager-pre-rename-", calls)
        self.assertIn(" glpi-builder", calls)
        self.assertIn("start glpi-builder", calls)


if __name__ == "__main__":
    unittest.main()
