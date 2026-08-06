import tempfile
import unittest
import hashlib
import json
import gzip
import io
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app as module
from tests.auth_test_support import authenticate


class ApplicationRouteTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.base = Path(self.temporary_directory.name)
        module.app.config.update(TESTING=True, SECRET_KEY="application-route-test")
        self.client = module.app.test_client()
        authenticate(self.client, module)

    def csrf(self):
        with self.client.session_transaction() as state:
            return state["csrf_token"]

    def payload(self):
        return {
            "csrf_token": self.csrf(),
            "app_type": "n8n",
            "project": "n8n-production",
            "host_port": "18775",
            "image": "docker.n8n.io/n8nio/n8n:latest",
            "timezone": "Europe/Brussels",
        }

    def test_application_backup_choices_exclude_other_applications(self):
        backup_root = self.base / "choices"
        choices = []
        for app_type in ("glpi", "n8n", "teampasswordmanager"):
            folder = backup_root / app_type
            folder.mkdir(parents=True)
            database = folder / "database.sql.gz"
            database.write_bytes(b"backup")
            (folder / "manifest.json").write_text(json.dumps({
                "schema": 2 if app_type == "n8n" else 1,
                "application": app_type,
                "database": database.name,
            }), encoding="utf-8")
            choices.append((str(database), f"{app_type} backup"))

        n8n = module.classify_n8n_backup_choices(choices)
        tpm = module.classify_tpm_backup_choices(choices)

        self.assertEqual([Path(value).parent.name for value, _label in n8n], ["n8n"])
        self.assertEqual([Path(value).parent.name for value, _label in tpm], ["teampasswordmanager"])

    @patch.object(module, "assert_docker_port_free")
    def test_preview_is_review_first_and_does_not_write_files(self, _port):
        with patch.object(module, "BASE_PATH", self.base):
            response = self.client.post("/applications/create", data=self.payload())
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Review deployment", response.data)
        self.assertFalse((self.base / "n8n-production").exists())

    @patch.object(module, "assert_docker_port_free")
    @patch.object(module.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
    def test_confirmed_deployment_writes_private_owned_project(self, _run, _port):
        with patch.object(module, "BASE_PATH", self.base):
            preview = self.client.post("/applications/create", data=self.payload())
            self.assertEqual(preview.status_code, 200)
            with self.client.session_transaction() as state:
                token = state["pending_application_preview"]["token"]
            response = self.client.post(
                "/applications/create/execute",
                data={"csrf_token": self.csrf(), "preview_token": token},
            )
        self.assertEqual(response.status_code, 302)
        folder = self.base / "n8n-production"
        self.assertTrue((folder / ".builder-app.json").is_file())
        self.assertEqual((folder / ".env").stat().st_mode & 0o777, 0o600)
        compose = (folder / "docker-compose.yml").read_text(encoding="utf-8")
        environment = (folder / ".env").read_text(encoding="utf-8")
        encryption_line = next(line for line in environment.splitlines() if line.startswith("N8N_ENCRYPTION_KEY="))
        self.assertNotIn(encryption_line.split("=", 1)[1], compose)
        commands = [call.args[0] for call in _run.call_args_list]
        self.assertIn(["docker", "compose", "-f", "docker-compose.yml", "config", "--quiet"], commands)
        self.assertIn(["docker", "compose", "-f", "docker-compose.yml", "up", "-d"], commands)

    @patch.object(module, "assert_docker_port_free")
    def test_existing_directory_is_never_adopted(self, _port):
        (self.base / "n8n-production").mkdir()
        with patch.object(module, "BASE_PATH", self.base):
            response = self.client.post("/applications/create", data=self.payload())
        self.assertEqual(response.status_code, 302)

    @patch.object(module.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
    def test_lifecycle_requires_valid_owned_manifest(self, run):
        profile = module.get_profile("n8n")
        with patch.object(module, "BASE_PATH", self.base):
            module.write_private_application_files({
                "app_type": profile.key, "project": "n8n-production",
                "host_port": 18775, "image": profile.default_image,
                "timezone": "Europe/Brussels",
            })
            response = self.client.post("/applications/action", data={
                "csrf_token": self.csrf(), "project": "n8n-production", "action": "restart",
            })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(run.call_args.args[0], ["docker", "compose", "restart"])

    @patch.object(module.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
    def test_unowned_directory_cannot_receive_lifecycle_action(self, run):
        (self.base / "other-project").mkdir()
        with patch.object(module, "BASE_PATH", self.base):
            response = self.client.post("/applications/action", data={
                "csrf_token": self.csrf(), "project": "other-project", "action": "stop",
            })
        self.assertEqual(response.status_code, 302)
        run.assert_not_called()

    @patch.object(module, "assert_docker_port_free")
    def test_tpm_quarantine_restore_requires_matching_fixed_version(self, _port):
        backup_root = self.base / "backups"
        backup_root.mkdir()
        backup = backup_root / "tpm.sql"
        backup.write_text(
            "-- MySQL dump\nCREATE TABLE passwords (id int);\nINSERT INTO passwords VALUES (1);\n",
            encoding="utf-8",
        )
        payload = {
            "app_type": "teampasswordmanager",
            "project": "tpm-test",
            "host_port": "18776",
            "image": "teampasswordmanager/teampasswordmanager:12.158.302",
            "timezone": "Europe/Brussels",
            "deployment_mode": "quarantine",
            "database_backup": str(backup),
            "backup_version": "12.158.302",
        }
        with patch.object(module, "BASE_PATH", self.base), patch.object(module, "BACKUP_ROOT", backup_root):
            data = module.validate_application_request(payload)
        self.assertTrue(data["quarantine"])
        self.assertEqual(data["database_backup"], str(backup))

    @patch.object(module, "assert_docker_port_free")
    def test_quarantine_restore_is_fail_closed_for_incomplete_n8n_set(self, _port):
        payload = self.payload()
        payload.update({"deployment_mode": "quarantine", "database_backup": "/tmp/no.sql", "backup_version": "1.0"})
        with patch.object(module, "BASE_PATH", self.base):
            with self.assertRaisesRegex(ValueError, "Select an n8n database backup"):
                module.validate_application_request(payload)

    @patch.object(module, "assert_docker_port_free")
    def test_n8n_quarantine_accepts_different_target_image_for_compatibility_test(self, _port):
        backup_root = self.base / "backups"
        backup_root.mkdir()
        database = backup_root / "database.sql.gz"
        database.write_bytes(gzip.compress(b"CREATE TABLE credentials (id int);\n"))
        files = backup_root / "files.tar.gz"
        with tarfile.open(files, "w:gz") as archive:
            data = b"{}"
            info = tarfile.TarInfo("data/config")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        secrets = backup_root / "secrets.env"
        secrets.write_text("N8N_ENCRYPTION_KEY=test-key\n", encoding="utf-8")
        checksums = "".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n" for path in (database, files, secrets))
        (backup_root / "SHA256SUMS").write_text(checksums, encoding="utf-8")
        (backup_root / "manifest.json").write_text(json.dumps({
            "schema": 2, "application": "n8n", "application_version": "1.99.1",
            "database_version": "PostgreSQL 16", "database": database.name,
            "files": files.name, "secrets": secrets.name, "checksums": "SHA256SUMS",
        }), encoding="utf-8")
        payload = self.payload()
        payload.update({"deployment_mode": "quarantine", "database_backup": str(database),
                        "image": "docker.n8n.io/n8nio/n8n:2.0.0",
                        "database_image": "postgres:15-alpine"})
        with patch.object(module, "BASE_PATH", self.base), patch.object(module, "BACKUP_ROOT", backup_root):
            result = module.validate_application_request(payload)
        self.assertTrue(result["quarantine"])
        self.assertTrue(result["backup_inspection"]["complete_set"])
        self.assertEqual(result["backup_version"], "1.99.1")
        self.assertEqual(result["image"], "docker.n8n.io/n8nio/n8n:2.0.0")
        self.assertEqual(result["database_image"], "postgres:15-alpine")

    def test_quarantine_sql_rejects_server_level_statements(self):
        backup_root = self.base / "backups"
        backup_root.mkdir()
        backup = backup_root / "unsafe.sql"
        backup.write_text("CREATE TABLE ok (id int);\nGRANT ALL ON *.* TO attacker;\n", encoding="utf-8")
        with patch.object(module, "BACKUP_ROOT", backup_root):
            with self.assertRaisesRegex(ValueError, "server-level statements"):
                module.validate_quarantine_database_backup(backup)

    def test_quarantine_rejects_all_interface_management_binding(self):
        backup_root = self.base / "backups"
        backup_root.mkdir()
        backup = backup_root / "tpm.sql"
        backup.write_text("CREATE TABLE passwords (id int);\nINSERT INTO passwords VALUES (1);\n", encoding="utf-8")
        payload = {
            "app_type": "teampasswordmanager", "project": "tpm-test", "host_port": "18776",
            "image": "teampasswordmanager/teampasswordmanager:12.158.302", "timezone": "Europe/Brussels",
            "deployment_mode": "quarantine", "database_backup": str(backup),
            "backup_version": "12.158.302", "bind_address": "0.0.0.0",
        }
        with patch.object(module, "BASE_PATH", self.base), patch.object(module, "BACKUP_ROOT", backup_root):
            with self.assertRaisesRegex(ValueError, "may not bind to every NAS interface"):
                module.validate_application_request(payload)

    def test_tpm_backup_manifest_verifies_checksums_tables_and_version(self):
        backup_root = self.base / "backups"
        backup_root.mkdir()
        backup = backup_root / "tpm.sql"
        backup.write_text(
            "-- Server version 5.7.44\nCREATE TABLE passwords (id int);\nINSERT INTO passwords VALUES (1);\n",
            encoding="utf-8",
        )
        digest = hashlib.sha256(backup.read_bytes()).hexdigest()
        (backup_root / module.TPM_BACKUP_MANIFEST).write_text(json.dumps({
            "schema": 1, "application": "teampasswordmanager", "application_version": "12.158.302",
            "database": {"file": "tpm.sql", "sha256": digest, "engine": "mysql", "version": "5.7.44"},
            "tables": ["passwords"], "files": [],
        }), encoding="utf-8")
        with patch.object(module, "BACKUP_ROOT", backup_root):
            inspection = module.inspect_tpm_backup(backup)
        self.assertTrue(inspection["complete_set"])
        self.assertEqual(inspection["server_version"], "5.7.44")
        self.assertEqual(inspection["tables"], ["passwords"])

    def test_tpm_backup_manifest_fails_closed_on_checksum_mismatch(self):
        backup_root = self.base / "backups"
        backup_root.mkdir()
        backup = backup_root / "tpm.sql"
        backup.write_text("CREATE TABLE passwords (id int);\nINSERT INTO passwords VALUES (1);\n", encoding="utf-8")
        (backup_root / module.TPM_BACKUP_MANIFEST).write_text(json.dumps({
            "schema": 1, "application": "teampasswordmanager", "application_version": "12.158.302",
            "database": {"file": "tpm.sql", "sha256": "0" * 64, "engine": "mysql", "version": "5.7"},
            "tables": ["passwords"], "files": [],
        }), encoding="utf-8")
        with patch.object(module, "BACKUP_ROOT", backup_root):
            with self.assertRaisesRegex(ValueError, "checksum"):
                module.inspect_tpm_backup(backup)

    @patch.object(module.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
    def test_quarantine_archive_is_confirmed_and_recoverable(self, _run):
        data = {
            "app_type": "teampasswordmanager", "project": "tpm-archive", "host_port": 18777,
            "image": "teampasswordmanager/teampasswordmanager:12.158.302", "timezone": "Europe/Brussels",
            "quarantine": True, "bind_address": "127.0.0.1", "expires_at": "2099-01-01T00:00:00",
            "backup_inspection": {"sha256": "a" * 64, "complete_set": False, "tables": ["passwords"], "server_version": "5.7"},
        }
        with patch.object(module, "BASE_PATH", self.base):
            module.write_private_application_files(data)
            response = self.client.post("/applications/quarantine/archive", data={
                "csrf_token": self.csrf(), "project": "tpm-archive", "confirm_project": "tpm-archive",
            })
        self.assertEqual(response.status_code, 302)
        self.assertFalse((self.base / "tpm-archive").exists())
        archives = list((self.base / ".docker-app-manager-trash").iterdir())
        self.assertEqual(len(archives), 1)
        self.assertTrue((archives[0] / "archive.json").is_file())


if __name__ == "__main__":
    unittest.main()
