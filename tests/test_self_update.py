#!/usr/bin/env python3
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import app as module
from tests.auth_test_support import authenticate


REQUIRED_FILES = {
    "app.py": 'APP_VERSION = "0.5.0-rc.21"\n',
    "app_ui.py": "VALUE = 1\n",
    "app_profiles.py": "VALUE = 1\n",
    "auth_security.py": "VALUE = 1\n",
    "Dockerfile": "FROM scratch\n",
    "docker-compose.container-manager.yml": "services: {}\n",
    "requirements.txt": "\n",
    "templates/glpi-compose.yml": "__PROJECT__\n__ENTRYPOINT__\n",
}


def make_update_zip(path, files=None):
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in (files or REQUIRED_FILES).items():
            archive.writestr(f"docker-app-manager/{name}", content)


class SelfUpdateValidationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.update_root = self.root / "updates"

    def tearDown(self):
        self.temp.cleanup()

    def inspect(self, archive, transaction="abcdefghijklmnop"):
        transaction_dir = self.update_root / "transactions" / transaction
        transaction_dir.mkdir(parents=True)
        destination = transaction_dir / "release.zip"
        destination.write_bytes(Path(archive).read_bytes())
        with patch.object(module, "BUILDER_UPDATE_ROOT", self.update_root), \
             patch.object(module.subprocess, "run", return_value=MagicMock(returncode=0, stderr="")):
            return module.inspect_update_zip(destination, transaction)

    def test_valid_release_is_staged_and_described(self):
        archive = self.root / "release.zip"
        make_update_zip(archive)
        manifest = self.inspect(archive)
        self.assertEqual(manifest["target_version"], "0.5.0-rc.21")
        self.assertEqual(len(manifest["sha256"]), 64)
        self.assertTrue(Path(manifest["package_root"], "Dockerfile").is_file())
        stored = json.loads((self.update_root / "transactions" / manifest["transaction_id"] / "status.json").read_text())
        self.assertEqual(stored["status"], "validated")

    def test_path_traversal_is_rejected_before_extraction(self):
        archive = self.root / "traversal.zip"
        make_update_zip(archive)
        with zipfile.ZipFile(archive, "a") as package:
            package.writestr("docker-app-manager/../../outside", "bad")
        with self.assertRaisesRegex(ValueError, "below docker-app-manager"):
            self.inspect(archive)
        self.assertFalse((self.root / "outside").exists())

    def test_symbolic_link_is_rejected(self):
        archive = self.root / "symlink.zip"
        make_update_zip(archive)
        info = zipfile.ZipInfo("docker-app-manager/link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(archive, "a") as package:
            package.writestr(info, "../../outside")
        with self.assertRaisesRegex(ValueError, "Symbolic links"):
            self.inspect(archive)

    def test_same_or_older_version_is_rejected(self):
        archive = self.root / "old.zip"
        files = dict(REQUIRED_FILES)
        files["app.py"] = 'APP_VERSION = "0.5.0-rc.9"\n'
        make_update_zip(archive, files)
        with self.assertRaisesRegex(ValueError, "not newer"):
            self.inspect(archive)

    def test_runner_contains_health_version_and_rollback_controls(self):
        transaction = "abcdefghijklmnop"
        transaction_dir = self.update_root / "transactions" / transaction
        transaction_dir.mkdir(parents=True)
        with patch.object(module, "BUILDER_UPDATE_ROOT", self.update_root):
            runner = module.write_update_runner(transaction, {"target_version": "0.5.0-rc.14"})
        source = runner.read_text()
        self.assertIn("--pull --no-cache", source)
        self.assertIn("rollback()", source)
        self.assertIn(".State.Health", source)
        self.assertIn("app.APP_VERSION", source)
        self.assertIn("config-preserved", source)

    def run_runner(self, fail_build=False):
        transaction = "abcdefghijklmnop"
        transaction_dir = self.update_root / "transactions" / transaction
        staging = transaction_dir / "staging" / "docker-app-manager"
        install = self.root / "docker-app-manager"
        fake_bin = self.root / "bin"
        staging.mkdir(parents=True)
        install.joinpath("config").mkdir(parents=True)
        fake_bin.mkdir()
        (fake_bin / "python").symlink_to(sys.executable)
        (install / "old.txt").write_text("old")
        (install / "config" / "builder-auth.json").write_text("persistent-auth")
        (install / "docker-compose.container-manager.yml").write_text("services: {}\n")
        (staging / "new.txt").write_text("new")
        (staging / "docker-compose.container-manager.yml").write_text("services: {}\n")
        status = {
            "transaction_id": transaction,
            "current_version": "0.5.0-rc.11",
            "target_version": "0.5.0-rc.12",
            "sha256": "a" * 64,
            "status": "validated",
        }
        (transaction_dir / "status.json").write_text(json.dumps(status))
        fake_docker = fake_bin / "docker"
        fake_docker.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = inspect ] && echo \"$*\" | grep -q '\\.Image'; then echo sha-old; exit 0; fi\n"
            "if [ \"$1\" = inspect ]; then echo healthy; exit 0; fi\n"
            "if [ \"$1\" = build ] && [ \"${FAIL_BUILD:-0}\" = 1 ]; then exit 9; fi\n"
            "if [ \"$1\" = exec ]; then echo \"$TARGET_VERSION\"; exit 0; fi\n"
            "exit 0\n"
        )
        fake_docker.chmod(0o700)
        with patch.object(module, "BUILDER_UPDATE_ROOT", self.update_root):
            runner = module.write_update_runner(transaction, status)
        env = {
            **os.environ,
            "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
            "UPDATE_ID": transaction[:12],
            "UPDATE_TX": str(transaction_dir),
            "INSTALL_ROOT": str(install),
            "BUILDER_CONTAINER": "docker-app-manager",
            "BUILDER_IMAGE": "docker-app-manager:container-manager",
            "TARGET_VERSION": "0.5.0-rc.12",
            "FAIL_BUILD": "1" if fail_build else "0",
        }
        result = subprocess.run(["/bin/sh", str(runner)], env=env, capture_output=True, text=True, timeout=30)
        return result, install, json.loads((transaction_dir / "status.json").read_text())

    def test_runner_replaces_program_but_preserves_config(self):
        result, install, status = self.run_runner()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(status["status"], "completed")
        self.assertTrue((install / "new.txt").is_file())
        self.assertFalse((install / "old.txt").exists())
        self.assertEqual((install / "config" / "builder-auth.json").read_text(), "persistent-auth")

    def test_failed_candidate_build_leaves_active_install_untouched(self):
        result, install, status = self.run_runner(fail_build=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(status["status"], "failed")
        self.assertTrue((install / "old.txt").is_file())
        self.assertFalse((install / "new.txt").exists())


class SelfUpdateUiTest(unittest.TestCase):
    def setUp(self):
        module.app.config.update(TESTING=True, SECRET_KEY="self-update-test")
        self.client = module.app.test_client()
        authenticate(self.client, module)

    def test_settings_show_two_step_update_form(self):
        response = self.client.get("/settings")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Update Docker App Manager", response.data)
        self.assertIn(b"Inspect update", response.data)
        self.assertIn(b'enctype="multipart/form-data"', response.data)

    def test_validated_update_requires_explicit_second_post(self):
        with self.client.session_transaction() as session:
            session["pending_builder_update"] = {
                "transaction_id": "abcdefghijklmnop",
                "target_version": "0.5.0-rc.12",
                "sha256": "a" * 64,
                "file_count": 12,
                "expanded_bytes": 2000,
            }
        response = self.client.get("/settings")
        self.assertIn(b"passed validation", response.data)
        self.assertIn(b"Update and restart Builder", response.data)
        self.assertNotIn(b"Inspect update", response.data)


if __name__ == "__main__":
    unittest.main()
