#!/usr/bin/env python3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as module


class RestoreModesTest(unittest.TestCase):
    def base_payload(self):
        return {
            "project": "glpi-mode-test",
            "glpi_image": "glpi/glpi:test",
            "mariadb_image": "mariadb:test",
            "host_port": "18777",
            "container_port": "8080",
            "tz": "Europe/Brussels",
            "cookie_samesite": "Lax",
            "cookie_secure": "Off",
        }

    def validation_context(self):
        return (
            patch.object(module, "validate_local_image", side_effect=lambda image, _kind: image),
            patch.object(module, "project_has_existing_state", return_value=False),
            patch.object(module, "get_container", return_value=None),
            patch.object(module, "assert_docker_port_free"),
        )

    def test_fresh_install_is_explicit_clean_mode(self):
        payload = self.base_payload()
        payload["operation_mode"] = "fresh"
        contexts = self.validation_context()
        with contexts[0], contexts[1], contexts[2], contexts[3]:
            data = module.validate_create_request(payload)

        self.assertTrue(data["fresh_install"])
        self.assertTrue(data["clean_db"])
        self.assertTrue(data["force_recreate"])
        self.assertTrue(data["skip_plugins"])
        self.assertEqual(data["db_backup"], "")
        self.assertEqual(data["file_backup"], "")

    def test_full_restore_requires_both_backups_and_can_skip_plugins(self):
        payload = self.base_payload()
        payload.update({
            "operation_mode": "restore",
            "db_backup": "/backups/database.sql.gz",
            "file_backup": "/backups/glpi-files.tar.gz",
            "skip_plugins": "yes",
        })
        contexts = self.validation_context()
        with contexts[0], contexts[1], contexts[2], contexts[3], \
             patch.object(module, "validate_backup_choice", side_effect=lambda value, *_args, **_kwargs: value):
            data = module.validate_create_request(payload)

        self.assertFalse(data["fresh_install"])
        self.assertFalse(data["clean_db"])
        self.assertTrue(data["restore_everything"])
        self.assertTrue(data["skip_plugins"])

    def test_fresh_prepare_removes_existing_config_and_plugins(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            old_base = module.BASE_PATH
            try:
                module.BASE_PATH = Path(temporary_directory)
                module.ensure_dirs("glpi-mode-test")
                config = module.project_dir("glpi-mode-test") / "glpi" / "config" / "config_db.php"
                plugin = module.project_dir("glpi-mode-test") / "plugins" / "legacy-plugin.php"
                config.parent.mkdir(parents=True, exist_ok=True)
                config.write_text("legacy", encoding="utf-8")
                plugin.write_text("legacy", encoding="utf-8")

                module.prepare_fresh_install("glpi-mode-test")

                self.assertFalse(config.exists())
                self.assertFalse(plugin.exists())
                self.assertTrue((module.project_dir("glpi-mode-test") / "glpi" / "files" / "_cache").is_dir())
            finally:
                module.BASE_PATH = old_base


if __name__ == "__main__":
    unittest.main()
