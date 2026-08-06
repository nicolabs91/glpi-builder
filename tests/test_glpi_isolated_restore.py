import gzip
import hashlib
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as module


class GlpiIsolatedRestoreTest(unittest.TestCase):
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
