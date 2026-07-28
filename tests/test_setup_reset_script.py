import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class SetupResetScriptTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.volume = Path(self.temporary_directory.name) / "volume1"
        self.app_dir = self.volume / "docker/glpi-builder"
        self.config_dir = self.app_dir / "config"
        self.config_dir.mkdir(parents=True)
        self.auth_file = self.config_dir / "builder-auth.json"
        self.auth_file.write_text('{"secret":"preserve-me"}\n', encoding="utf-8")
        self.state_file = self.volume / "docker/.glpi-builder-auth-state"
        self.state_file.write_text("123\n", encoding="utf-8")
        self.script = Path(__file__).parents[1] / "reset_setup_on_synology.sh"

    def tearDown(self):
        self.temporary_directory.cleanup()

    def run_script(self, *arguments):
        environment = os.environ.copy()
        environment.update(
            GLPI_BUILDER_APP_DIR=str(self.app_dir),
            GLPI_BUILDER_TESTING="1",
            BUILDER_AUTH_STATE_PATH=str(self.state_file),
        )
        return subprocess.run(
            ["/bin/sh", str(self.script), *arguments],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )

    def test_requires_explicit_confirmation(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 2)
        self.assertTrue(self.auth_file.exists())

    def test_moves_credentials_and_replay_state_to_private_backup(self):
        result = self.run_script("--confirm-reset")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.auth_file.exists())
        self.assertFalse(self.state_file.exists())
        recovery_directories = list((self.config_dir / "recovery-backups").iterdir())
        self.assertEqual(len(recovery_directories), 1)
        recovery = recovery_directories[0]
        self.assertEqual(
            (recovery / "builder-auth.json").read_text(encoding="utf-8"),
            '{"secret":"preserve-me"}\n',
        )
        self.assertEqual((recovery / "totp-replay-state").read_text(encoding="utf-8"), "123\n")
        self.assertEqual(self.config_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(recovery.stat().st_mode & 0o777, 0o700)
        self.assertEqual((recovery / "builder-auth.json").stat().st_mode & 0o777, 0o600)

    def test_refuses_symlinked_auth_file(self):
        self.auth_file.unlink()
        target = self.config_dir / "real-auth.json"
        target.write_text("{}\n", encoding="utf-8")
        self.auth_file.symlink_to(target)
        result = self.run_script("--confirm-reset")
        self.assertEqual(result.returncode, 2)
        self.assertTrue(target.exists())

    def test_refuses_symlinked_recovery_root(self):
        external = Path(self.temporary_directory.name) / "external-recovery"
        external.mkdir()
        (self.config_dir / "recovery-backups").symlink_to(external, target_is_directory=True)
        result = self.run_script("--confirm-reset")
        self.assertEqual(result.returncode, 2)
        self.assertTrue(self.auth_file.exists())
        self.assertEqual(list(external.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
