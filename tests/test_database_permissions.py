#!/usr/bin/env python3
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as module


class DatabasePermissionsTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.old_base = module.BASE_PATH
        module.BASE_PATH = Path(self.temporary_directory.name)

    def tearDown(self):
        module.BASE_PATH = self.old_base
        self.temporary_directory.cleanup()

    def test_prepare_makes_synology_bind_mount_writable(self):
        db_folder = module.project_dir("glpi-permissions-test") / "db"
        db_folder.mkdir(parents=True)
        db_folder.chmod(0o755)

        prepared = module.prepare_db_directory("glpi-permissions-test")

        self.assertEqual(prepared, db_folder)
        self.assertEqual(prepared.stat().st_mode & 0o777, 0o777)

    def test_prepare_rejects_database_symlink(self):
        project = module.project_dir("glpi-permissions-test")
        project.mkdir(parents=True)
        target = module.BASE_PATH / "somewhere-else"
        target.mkdir()
        (project / "db").symlink_to(target, target_is_directory=True)

        with self.assertRaisesRegex(RuntimeError, "symlink"):
            module.prepare_db_directory("glpi-permissions-test")

    def test_finalize_uses_initialized_mariadb_ownership_and_tightens_mode(self):
        db_folder = module.prepare_db_directory("glpi-permissions-test")
        mysql_folder = db_folder / "mysql"
        mysql_folder.mkdir()
        expected = mysql_folder.stat()

        message = module.finalize_db_directory_permissions("glpi-permissions-test")

        actual = db_folder.stat()
        self.assertEqual((actual.st_uid, actual.st_gid), (expected.st_uid, expected.st_gid))
        self.assertEqual(actual.st_mode & 0o777, 0o750)
        self.assertIn("tightened", message)


if __name__ == "__main__":
    unittest.main()
