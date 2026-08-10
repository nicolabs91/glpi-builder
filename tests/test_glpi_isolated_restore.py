import gzip
import hashlib
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import app as module


class GlpiIsolatedRestoreTest(unittest.TestCase):
    def test_isolated_services_are_started_from_saved_compose_yaml(self):
        completed = MagicMock(returncode=0, stdout="started", stderr="")
        with tempfile.TemporaryDirectory() as root, \
             patch.object(module, "BASE_PATH", Path(root)), \
             patch.object(module.subprocess, "run", return_value=completed) as run:
            (Path(root) / "glpi-isolated").mkdir()
            result = module.run_isolated_compose(
                "glpi-isolated", ["glpi-isolated"], force_recreate=True,
            )
        self.assertEqual(result, "started")
        self.assertEqual(
            run.call_args.args[0],
            ["docker", "compose", "--project-name", "glpi-isolated", "-f",
             "docker-compose.yml", "up", "-d", "--force-recreate", "glpi-isolated"],
        )

    def test_isolated_port_binding_must_match_requested_port(self):
        container = MagicMock()
        container.attrs = {
            "HostConfig": {"PortBindings": {"8080/tcp": [{"HostPort": "8778"}]}}
        }
        with patch.object(module, "get_container", return_value=container):
            self.assertIn("8778:8080", module.verify_glpi_port_binding("glpi-isolated", 8778))
            with self.assertRaisesRegex(RuntimeError, "no matching published port"):
                module.verify_glpi_port_binding("glpi-isolated", 8779)

    def test_isolated_compose_only_adds_supported_isolation_settings(self):
        env = module.build_env(
            "glpi-isolated",
            "glpi/glpi:11.0.8",
            "mariadb:11.4",
            18080,
            8080,
            "Europe/Brussels",
            True,
            isolated_restore=True,
        )

        compose = module.render_glpi_compose("glpi-isolated", env)

        self.assertIn("chmod 666 /tmp/supervisord.log /tmp/stdout.log /tmp/stderr.log", compose)
        self.assertIn("internal: true", compose)
        self.assertIn('GLPI_CRONTAB_ENABLED: "0"', compose)
        self.assertNotIn("/tmp/supervisord.isolated.conf", compose)
        self.assertNotIn("command=/bin/true", compose)
        self.assertNotIn("autostart=false", compose)
        self.assertNotIn("autorestart=false", compose)

    def test_normal_compose_does_not_change_proven_cron_program(self):
        env = module.build_env(
            "glpi-normal", "glpi/glpi:11.0.8", "mariadb:11.4",
            18080, 8080, "Europe/Brussels", False,
        )
        compose = module.render_glpi_compose("glpi-normal", env)
        self.assertNotIn("command=/bin/true", compose)
        self.assertNotIn("autorestart=false", compose)

    def test_isolated_entrypoint_is_byte_for_byte_the_proven_entrypoint(self):
        env = module.build_env(
            "glpi-isolated", "glpi/glpi:11.0.8", "mariadb:11.4",
            18080, 8080, "Europe/Brussels", True, isolated_restore=True,
        )
        compose = module.render_glpi_compose("glpi-isolated", env)
        expected = module.indent_text(module.GLPI_ENTRY_COMMAND, 8)
        self.assertIn("      - |\n" + expected + "\n", compose)

    def test_isolated_yaml_diff_is_limited_to_two_isolation_lines(self):
        normal_env = module.build_env(
            "glpi-compare", "glpi/glpi:11.0.8", "mariadb:11.4",
            18080, 8080, "Europe/Brussels", True, isolated_restore=False,
        )
        isolated_env = dict(normal_env, BUILDER_QUARANTINE="1")
        normal = module.render_glpi_compose("glpi-compare", normal_env)
        isolated = module.render_glpi_compose("glpi-compare", isolated_env)

        normalized = isolated.replace(
            'GLPI_CRONTAB_ENABLED: "0"', 'GLPI_CRONTAB_ENABLED: "1"',
        ).replace("    driver: bridge\n    internal: true\n", "    driver: bridge\n")
        self.assertEqual(normalized, normal)

    def test_isolated_runtime_removes_only_restored_runtime_traces(self):
        with tempfile.TemporaryDirectory() as root:
            project_root = Path(root) / "glpi-isolated"
            for relative in ("files/_log", "files/_cron", "logs"):
                folder = project_root / "glpi" / relative
                folder.mkdir(parents=True)
                (folder / "production.log").write_text("An email was sent", encoding="utf-8")
            config = project_root / "glpi" / "config"
            config.mkdir(parents=True)
            (config / "config_db.php").write_text("keep", encoding="utf-8")

            with patch.object(module, "BASE_PATH", Path(root)):
                result = module.prepare_glpi_isolated_runtime("glpi-isolated")

            self.assertIn("Cleared isolated runtime logs", result)
            self.assertEqual((config / "config_db.php").read_text(encoding="utf-8"), "keep")
            for relative in ("files/_log", "files/_cron", "logs"):
                self.assertEqual(list((project_root / "glpi" / relative).iterdir()), [])

    def make_set(self, root):
        folder = Path(root) / "glpi-production" / "2026-08-06_100000"
        folder.mkdir(parents=True)
        database = folder / "database.sql.gz"
        database.write_bytes(gzip.compress(b"CREATE TABLE glpi_users (id int);\nINSERT INTO glpi_users VALUES (1);\n"))
        files = folder / "files.tar.gz"
        source = folder / "source"
        (source / "glpi" / "config").mkdir(parents=True)
        (source / "glpi" / "config" / "config_db.php").write_text("<?php class DB extends DBmysql { public $dbhost='production-db'; }", encoding="utf-8")
        (source / "plugins").mkdir()
        with tarfile.open(files, "w:gz") as archive:
            archive.add(source / "glpi", arcname="glpi")
            archive.add(source / "plugins", arcname="plugins")
        checksums = "".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
            for path in (database, files)
        )
        (folder / "SHA256SUMS").write_text(checksums, encoding="utf-8")
        (folder / "manifest.json").write_text(json.dumps({
            "schema": 1,
            "application": "glpi",
            "application_version": "11.0.8",
            "database_version": "MariaDB 11.4",
            "project": "glpi-production",
            "database": database.name,
            "files": files.name,
            "checksums": "SHA256SUMS",
        }), encoding="utf-8")
        return database, files

    def test_verified_set_allows_different_target_versions(self):
        with tempfile.TemporaryDirectory() as root:
            database, files = self.make_set(root)
            with patch.object(module, "BACKUP_ROOT", Path(root)):
                result = module.inspect_glpi_backup_set(database, files)
            self.assertEqual(result["application_version"], "11.0.8")
            self.assertEqual(result["database_version"], "MariaDB 11.4")

    def test_checksum_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            database, files = self.make_set(root)
            database.write_bytes(b"changed")
            with patch.object(module, "BACKUP_ROOT", Path(root)):
                with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                    module.inspect_glpi_backup_set(database, files)

    def test_database_and_files_must_be_from_same_set(self):
        with tempfile.TemporaryDirectory() as root:
            database, files = self.make_set(root)
            other = Path(root) / "other"
            other.mkdir()
            copied = other / files.name
            copied.write_bytes(files.read_bytes())
            with patch.object(module, "BACKUP_ROOT", Path(root)):
                with self.assertRaisesRegex(ValueError, "same backup set"):
                    module.inspect_glpi_backup_set(database, copied)

    def test_request_keeps_source_and_target_versions_independent(self):
        with tempfile.TemporaryDirectory() as root:
            backup_root = Path(root) / "backups"
            database, files = self.make_set(backup_root)
            projects = Path(root) / "projects"
            projects.mkdir()
            payload = {
                "project": "glpi-compat-test",
                "glpi_image": "glpi/glpi:11.1.0",
                "mariadb_image": "mariadb:12.0",
                "host_port": "18888",
                "container_port": "8080",
                "operation_mode": "isolated",
                "db_backup_select": str(database),
                "file_backup_select": str(files),
                "tz": "Europe/Brussels",
            }
            with patch.object(module, "BACKUP_ROOT", backup_root), \
                 patch.object(module, "BASE_PATH", projects), \
                 patch.object(module, "validate_local_image", side_effect=lambda image, _kind: image), \
                 patch.object(module, "project_has_existing_state", return_value=False), \
                 patch.object(module, "assert_docker_port_free"):
                result = module.validate_create_request(payload)
            self.assertTrue(result["isolated_restore"])
            self.assertTrue(result["clean_db"])
            self.assertEqual(result["backup_inspection"]["application_version"], "11.0.8")
            self.assertEqual(result["glpi_image"], "glpi/glpi:11.1.0")
            self.assertEqual(result["mariadb_image"], "mariadb:12.0")
            self.assertFalse(result["update_backup_source"])


if __name__ == "__main__":
    unittest.main()
