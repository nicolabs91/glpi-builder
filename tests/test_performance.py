#!/usr/bin/env python3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as module


class PerformanceTest(unittest.TestCase):
    def tearDown(self):
        module.invalidate_dashboard_cache()

    def test_dashboard_snapshot_reuses_one_docker_inventory(self):
        containers = Mock()
        containers.list.return_value = []
        images = Mock()
        images.list.return_value = [
            type("Image", (), {"tags": ["glpi/glpi:test"]})(),
            type("Image", (), {"tags": ["mariadb:test"]})(),
        ]
        client = type("Client", (), {"containers": containers, "images": images})()

        module.invalidate_dashboard_cache()
        with patch.object(module, "docker_client", return_value=client):
            first = module.dashboard_docker_snapshot()
            second = module.dashboard_docker_snapshot()

        self.assertEqual(first, second)
        self.assertEqual(containers.list.call_count, 1)
        self.assertEqual(images.list.call_count, 1)

    def test_combined_backup_scan_classifies_files_in_one_result(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            backup_root = Path(temporary_directory)
            complete_folder = backup_root / "GLPI_Backup_example"
            complete_folder.mkdir()
            database = complete_folder / "database.sql.gz"
            archive = complete_folder / "glpi-files.tar.gz"
            unsupported_archive = complete_folder / "glpi-files.zip"
            database.write_bytes(b"database")
            archive.write_bytes(b"files")
            unsupported_archive.write_bytes(b"unsupported")

            unrelated_folder = backup_root / "OtherApp_Backup_example"
            unrelated_folder.mkdir()
            unrelated_database = unrelated_folder / "database.sql.gz"
            unrelated_archive = unrelated_folder / "config.tar.gz"
            unrelated_database.write_bytes(b"other database")
            unrelated_archive.write_bytes(b"other files")

            with patch.object(module, "BACKUP_ROOT", backup_root):
                result = module.scan_backup_choices(backup_root, include_dirs=True)

        database_paths = [path for path, _label in result["database"]]
        file_paths = [path for path, _label in result["files"]]
        self.assertEqual(database_paths, [str(database)])
        self.assertEqual(file_paths, [str(archive)])
        self.assertNotIn(str(unsupported_archive), file_paths)
        self.assertNotIn(str(complete_folder), file_paths)
        self.assertNotIn(str(unrelated_database), database_paths)
        self.assertNotIn(str(unrelated_archive), file_paths)
        self.assertNotIn(str(unrelated_folder), file_paths)

    def test_legacy_scan_files_hides_non_glpi_application_backups(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            backup_root = Path(temporary_directory)
            glpi_database = backup_root / "manual-glpi-database.sql"
            other_database = backup_root / "other-app-database.sql"
            glpi_database.write_bytes(b"glpi")
            other_database.write_bytes(b"other")

            with patch.object(module, "BACKUP_ROOT", backup_root):
                choices = module.scan_files(backup_root, module.DB_EXTENSIONS)

        paths = [path for path, _label in choices]
        self.assertEqual(paths, [str(glpi_database)])

if __name__ == "__main__":
    unittest.main()
