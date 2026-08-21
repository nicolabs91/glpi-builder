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
    def test_change_port_preserves_isolated_network_and_recreates_ingress(self):
        env = module.build_env(
            "glpi-isolated", "glpi/glpi:11.0.8", "mariadb:11.4",
            18778, 8080, "Europe/Brussels", True, isolated_restore=True,
        )
        db = MagicMock()
        with patch.object(module, "read_env", return_value=env), \
             patch.object(module, "assert_docker_port_free") as port_free, \
             patch.object(module, "ensure_dirs"), \
             patch.object(module, "ensure_network") as ensure_network, \
             patch.object(module, "ensure_container_network") as ensure_container_network, \
             patch.object(module, "docker_client") as docker_client, \
             patch.object(module, "write_env") as write_env, \
             patch.object(module, "write_compose") as write_compose, \
             patch.object(module, "run_isolated_compose", return_value="recreated") as compose, \
             patch.object(module, "verify_glpi_port_binding", return_value="verified") as verify:
            docker_client.return_value.containers.get.return_value = db
            messages = module.change_project_port("glpi-isolated", 18779)

        port_free.assert_called_once_with(
            18779, exclude_containers={"glpi-isolated", "glpi-isolated-ingress"},
        )
        ensure_network.assert_called_once_with("glpi-isolated", internal=True)
        ensure_container_network.assert_called_once_with(
            "glpi-isolated", "glpi-isolated-db", internal=True,
        )
        self.assertEqual(write_env.call_args.args[1]["GLPI_HTTP_PORT"], "18779")
        self.assertEqual(write_compose.call_args.args[1]["GLPI_HTTP_PORT"], "18779")
        compose.assert_called_once_with(
            "glpi-isolated", ["glpi-isolated", "glpi-isolated-ingress"],
            force_recreate=True,
        )
        verify.assert_called_once_with("glpi-isolated", 18779)
        self.assertIn("Changed port from 18778 to 18779.", messages)
        self.assertIn("verified", messages)

    def test_empty_legacy_network_is_removed_before_compose_start(self):
        network = MagicMock()
        network.attrs = {
            "Internal": True,
            "Labels": {},
            "Containers": {},
        }
        client = MagicMock()
        client.networks.get.return_value = network

        with patch.object(module, "docker_client", return_value=client):
            result = module.prepare_compose_network("glpi-isolated", internal=True)

        network.remove.assert_called_once_with()
        self.assertIn("Compose can recreate", result)

    def test_connected_unowned_network_is_not_removed(self):
        network = MagicMock()
        network.attrs = {
            "Internal": True,
            "Labels": {},
            "Containers": {"container-id": {"Name": "unexpected"}},
        }
        client = MagicMock()
        client.networks.get.return_value = network

        with patch.object(module, "docker_client", return_value=client), \
             self.assertRaisesRegex(RuntimeError, "connected containers"):
            module.prepare_compose_network("glpi-isolated", internal=True)

        network.remove.assert_not_called()

    def test_compose_owned_network_is_preserved(self):
        network = MagicMock()
        network.attrs = {
            "Internal": True,
            "Labels": {
                "com.docker.compose.project": "glpi-isolated",
                "com.docker.compose.network": "glpi-isolated-network",
            },
            "Containers": {},
        }
        client = MagicMock()
        client.networks.get.return_value = network

        with patch.object(module, "docker_client", return_value=client):
            result = module.prepare_compose_network("glpi-isolated", internal=True)

        network.remove.assert_not_called()
        self.assertIn("Compose-owned", result)

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

    def test_restore_explicitly_starts_ingress_service(self):
        env = module.build_env(
            "glpi-isolated", "glpi/glpi:11.0.8", "mariadb:11.4",
            18778, 8080, "Europe/Brussels", True, isolated_restore=True,
        )
        with patch.object(module, "prepare_compose_network", return_value="network"), \
             patch.object(module, "ensure_dirs"), \
             patch.object(module, "prepare_db_directory"), \
             patch.object(module, "run_isolated_compose", side_effect=["db", "web"]) as run, \
             patch.object(module, "ensure_container_network"), \
             patch.object(module, "wait_db", return_value=(True, "ready")), \
             patch.object(module, "finalize_db_directory_permissions", return_value="permissions"), \
             patch.object(module, "reset_db_user", return_value=(True, "user")), \
             patch.object(module, "ensure_glpi_writable_dirs"), \
             patch.object(module, "fix_permissions", return_value="fixed"), \
             patch.object(module, "repair_glpi_container_runtime_permissions", return_value="runtime"), \
             patch.object(module, "verify_glpi_port_binding", return_value="port"):
            module.create_or_restore(
                "glpi-isolated", env, clean_db=False, force_recreate=True,
                db_backup=None, file_backup=None, isolated_restore=True,
            )
        self.assertEqual(
            run.call_args_list[-1].args[1],
            ["glpi-isolated", "glpi-isolated-ingress"],
        )

    def test_glpi_runtime_repair_runs_inside_container_as_uid_33(self):
        result = type("ExecResult", (), {"exit_code": 0, "output": b""})()
        container = MagicMock()
        container.exec_run.return_value = result
        with patch.object(module, "get_container", return_value=container):
            message = module.repair_glpi_container_runtime_permissions("glpi-isolated")
        command = container.exec_run.call_args.args[0]
        self.assertIn("mkdir -p /var/glpi/logs", command[2])
        self.assertIn("chown -R 33:33 /var/glpi", command[2])
        self.assertIn("runtime directories", message)

    def test_isolated_port_binding_must_match_requested_port(self):
        container = MagicMock()
        container.attrs = {
            "HostConfig": {"PortBindings": {"8080/tcp": [{"HostPort": "8778"}]}},
            "NetworkSettings": {"Ports": {"8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8778"}]}},
        }
        with patch.object(module, "get_container", return_value=container):
            self.assertIn("8778:8080", module.verify_glpi_port_binding("glpi-isolated", 8778))
            with self.assertRaisesRegex(RuntimeError, "no matching active published port"):
                module.verify_glpi_port_binding("glpi-isolated", 8779)

    def test_isolated_port_proof_rejects_configured_but_inactive_binding(self):
        container = MagicMock()
        container.attrs = {
            "HostConfig": {"PortBindings": {"8080/tcp": [{"HostPort": "8778"}]}},
            "NetworkSettings": {"Ports": {}},
        }
        with patch.object(module, "get_container", return_value=container), \
             self.assertRaisesRegex(RuntimeError, "no matching active published port"):
            module.verify_glpi_port_binding("glpi-isolated", 8778)

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
        self.assertIn('host_ip: 0.0.0.0', compose)
        self.assertIn("glpi-isolated-ingress", compose)
        self.assertIn("alpine/socat:1.8.0.3", compose)
        app_section = compose.split("  glpi-isolated:", 1)[1].split("  glpi-isolated-ingress:", 1)[0]
        self.assertNotIn("ports:", app_section)
        self.assertIn('SAMESITE="$${GLPI_SESSION_COOKIE_SAMESITE:-Lax}"', compose)
        self.assertIn('for dir in /etc/php/*/apache2/conf.d', compose)
        self.assertIn("/var/glpi/logs", compose)
        self.assertNotIn('for dir in /etc/php/*/apache2/conf.d; do\n  if [ -d "" ]', compose)

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
        expected = module.indent_text(module.GLPI_ENTRY_COMMAND.replace("$", "$$"), 8)
        self.assertIn("      - |\n" + expected + "\n", compose)

    def test_isolated_yaml_adds_ingress_without_exposing_app_container(self):
        normal_env = module.build_env(
            "glpi-compare", "glpi/glpi:11.0.8", "mariadb:11.4",
            18080, 8080, "Europe/Brussels", True, isolated_restore=False,
        )
        isolated_env = dict(normal_env, BUILDER_QUARANTINE="1")
        normal = module.render_glpi_compose("glpi-compare", normal_env)
        isolated = module.render_glpi_compose("glpi-compare", isolated_env)

        self.assertIn("glpi-compare-ingress", isolated)
        self.assertNotIn("glpi-compare-ingress", normal)
        isolated_app = isolated.split("  glpi-compare:", 1)[1].split("  glpi-compare-ingress:", 1)[0]
        normal_app = normal.split("  glpi-compare:", 1)[1].split("networks:", 1)[0]
        self.assertNotIn("ports:", isolated_app)
        self.assertIn("ports:", normal_app)

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

    def test_isolated_restore_scrubs_glpi_oauth_clients_and_tokens(self):
        result = type("ExecResult", (), {"exit_code": 0, "output": (b"", b"")})()
        database = MagicMock()
        database.exec_run.return_value = result
        with patch.object(module, "get_container", return_value=database):
            message = module.scrub_glpi_isolated_oauth("glpi-isolated")

        sql = database.exec_run.call_args.args[0][-1]
        self.assertIn("glpi_oauthclients", sql)
        self.assertIn("glpi_oauth_access_tokens", sql)
        self.assertIn("glpi_oauth_refresh_tokens", sql)
        self.assertIn("glpi_oauth_auth_codes", sql)
        self.assertIn("Removed copied GLPI OAuth clients", message)

    def test_oauth_scrub_failure_aborts(self):
        result = type("ExecResult", (), {"exit_code": 1, "output": (b"", b"database error")})()
        database = MagicMock()
        database.exec_run.return_value = result
        with patch.object(module, "get_container", return_value=database), \
             self.assertRaisesRegex(RuntimeError, "Could not remove copied GLPI OAuth"):
            module.scrub_glpi_isolated_oauth("glpi-isolated")

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

    def test_verified_legacy_builder_set_is_accepted(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / "GLPI_Backup_20260813-000001"
            folder.mkdir()
            database = folder / "glpi-database.sql"
            database.write_text("CREATE TABLE glpi_users (id int);", encoding="utf-8")
            files = folder / "glpi-files.tar.gz"
            files.write_bytes(gzip.compress(b"legacy files"))
            info = folder / "BACKUP_INFO"
            info.write_text(
                "PROJECT_NAME=glpi-prod-1108\nCREATED_AT=2026-08-13T00:00:01+0200\n",
                encoding="utf-8",
            )
            checksums = "".join(
                f"{hashlib.sha256(path.read_bytes()).hexdigest()} {path.name}\n"
                for path in (database, files, info)
            )
            (folder / "SHA256SUMS").write_text(checksums, encoding="utf-8")
            with patch.object(module, "BACKUP_ROOT", Path(root)):
                result = module.inspect_glpi_backup_set(database, files)
            self.assertTrue(result["manifest"]["legacy"])
            self.assertEqual(result["manifest"]["project"], "glpi-prod-1108")

    def test_legacy_builder_set_rejects_tampered_backup_info(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / "GLPI_Backup_20260813-000001"
            folder.mkdir()
            database = folder / "glpi-database.sql"
            database.write_text("SELECT 1;", encoding="utf-8")
            files = folder / "glpi-files.tar.gz"
            files.write_bytes(gzip.compress(b"files"))
            info = folder / "BACKUP_INFO"
            info.write_text("PROJECT_NAME=prod\nCREATED_AT=now\n", encoding="utf-8")
            checksums = "".join(
                f"{hashlib.sha256(path.read_bytes()).hexdigest()} {path.name}\n"
                for path in (database, files, info)
            )
            (folder / "SHA256SUMS").write_text(checksums, encoding="utf-8")
            info.write_text("PROJECT_NAME=changed\nCREATED_AT=now\n", encoding="utf-8")
            with patch.object(module, "BACKUP_ROOT", Path(root)):
                with self.assertRaisesRegex(ValueError, "checksum mismatch for BACKUP_INFO"):
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
