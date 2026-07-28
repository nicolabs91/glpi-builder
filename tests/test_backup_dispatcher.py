#!/usr/bin/env python3
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as module


class BackupDispatcherTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.task = self.root / "GLPI_backup" / "_system"
        self.scheduler = self.root / "Synology_task_scheduler"
        self.projects = self.task / "projects"
        self.state = self.task / "state"
        self.locks = self.task / "locks"
        self.projects.mkdir(parents=True)
        self.state.mkdir()
        self.locks.mkdir()
        self.scheduler.mkdir()
        source_root = Path(module.__file__).resolve().parent / "backup"
        self.dispatcher = self.scheduler / "GLPI_backup_dispatcher.sh"
        self.dispatcher.write_text(
            (source_root / "GLPI_backup_dispatcher.sh").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        self.dispatcher.chmod(0o750)
        self.backup = self.task / "GLPI_backup.sh"
        self.backup.write_text(
            "#!/bin/bash\nset -eu\n. \"$GLPI_BACKUP_ENV\"\necho \"$PROJECT_NAME\" >> \"$BACKUP_ROOT/runs\"\n",
            encoding="utf-8",
        )
        self.backup.chmod(0o750)

    def tearDown(self):
        self.temp.cleanup()

    def write_schedule(self, project, **overrides):
        values = {
            "PROJECT_NAME": project,
            "BACKUP_ROOT": str(self.root),
            "SCHEDULE_ENABLED": "yes",
            "SCHEDULE_KIND": "interval",
            "SCHEDULE_TIME": "02:00",
            "SCHEDULE_WEEKDAYS": "7",
            "INTERVAL_HOURS": "6",
        }
        values.update(overrides)
        (self.projects / f"{project}.env").write_text(
            "".join(f"{key}={value}\n" for key, value in values.items()),
            encoding="utf-8",
        )

    def test_dispatcher_runs_all_due_projects_serially_and_records_state(self):
        self.write_schedule("glpi-one")
        self.write_schedule("glpi-two")
        subprocess.run(
            ["/bin/bash", str(self.dispatcher)],
            check=True,
        )
        self.assertEqual(
            (self.root / "runs").read_text(encoding="utf-8").splitlines(),
            ["glpi-one", "glpi-two"],
        )
        for project in ("glpi-one", "glpi-two"):
            state = module.read_simple_env_file(self.state / f"{project}.env")
            self.assertEqual(state["LAST_STATUS"], "success")
            self.assertTrue(state["LAST_SUCCESS"].isdigit())
        self.assertEqual(
            module.read_simple_env_file(self.state / "dispatcher.env")["STATUS"],
            "idle",
        )
        fallback = module.read_simple_env_file(self.scheduler / ".dispatcher.env")
        self.assertEqual(fallback["STATUS"], "idle")
        self.assertTrue(fallback["LAST_HEARTBEAT"].isdigit())

    def test_dispatcher_does_not_repeat_interval_before_it_is_due(self):
        self.write_schedule("glpi-one")
        subprocess.run(["/bin/bash", str(self.dispatcher)], check=True)
        subprocess.run(["/bin/bash", str(self.dispatcher)], check=True)
        self.assertEqual(
            (self.root / "runs").read_text(encoding="utf-8").splitlines(),
            ["glpi-one"],
        )

    def test_existing_dispatcher_lock_prevents_overlapping_run(self):
        self.write_schedule("glpi-one")
        (self.locks / "dispatcher.lock").mkdir()
        result = subprocess.run(
            ["/bin/bash", str(self.dispatcher)],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("already active", result.stdout)
        self.assertFalse((self.root / "runs").exists())

    def test_dispatcher_rejects_symlinked_state_directory(self):
        self.state.rmdir()
        outside = self.root / "outside-state"
        outside.mkdir()
        self.state.symlink_to(outside, target_is_directory=True)

        result = subprocess.run(
            ["/bin/bash", str(self.dispatcher)],
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unsafe symlink", result.stderr)

    def test_failed_run_records_error_and_preserves_last_success(self):
        self.write_schedule("glpi-one")
        previous_success = str(int(time.time()) - 86400)
        (self.state / "glpi-one.env").write_text(
            f"LAST_ATTEMPT=0\nLAST_SUCCESS={previous_success}\n",
            encoding="utf-8",
        )
        self.backup.write_text("#!/bin/bash\nexit 23\n", encoding="utf-8")
        subprocess.run(["/bin/bash", str(self.dispatcher)], check=True)
        state = module.read_simple_env_file(self.state / "glpi-one.env")
        self.assertEqual(state["LAST_STATUS"], "failed")
        self.assertEqual(state["LAST_EXIT_CODE"], "23")
        self.assertEqual(state["LAST_SUCCESS"], previous_success)

    def test_legacy_single_project_config_is_migrated(self):
        legacy = self.task / "GLPI_backup.env"
        legacy.write_text(
            "PROJECT_NAME=glpi-old\nBACKUP_ROOT=/volume1/docker/_BACKUPS\n"
            "PROJECT_DIR=/volume1/docker/glpi-old\nDB_CONTAINER=glpi-old-db\n"
            "DB_NAME=glpi\nMYSQL_CNF=/safe/config.cnf\n"
            "CONTAINER_CNF=/tmp/config.cnf\nRETENTION_DAYS=60\n",
            encoding="utf-8",
        )
        with (
            patch.object(module, "BACKUP_ENV_PATH", legacy),
            patch.object(module, "BACKUP_PROJECTS_DIR", self.projects),
        ):
            self.assertEqual(module.scheduled_backup_projects(), ["glpi-old"])
            migrated = module.read_simple_env_file(self.projects / "glpi-old.env")
        self.assertTrue(legacy.exists(), "legacy DSM tasks must keep working during migration")
        self.assertEqual(migrated["SCHEDULE_KIND"], "daily")
        self.assertEqual(migrated["SCHEDULE_TIME"], "02:00")
        self.assertEqual(os.stat(self.projects / "glpi-old.env").st_mode & 0o777, 0o600)

    def test_corrupt_schedule_falls_back_safely(self):
        schedule = self.projects / "glpi-safe.env"
        schedule.write_text(
            "PROJECT_NAME=glpi-safe\nSCHEDULE_ENABLED=yes\n"
            "SCHEDULE_KIND=broken\nSCHEDULE_TIME=99:99\n"
            "INTERVAL_HOURS=not-a-number\nRETENTION_DAYS=-1\n",
            encoding="utf-8",
        )
        with (
            patch.object(module, "BACKUP_PROJECTS_DIR", self.projects),
            patch.object(module, "BACKUP_STATE_DIR", self.state),
            patch.object(module, "BACKUP_ENV_PATH", self.task / "missing.env"),
        ):
            status = module.backup_schedule_status("glpi-safe")
        self.assertEqual(status["kind"], "daily")
        self.assertEqual(status["time"], "02:00")
        self.assertEqual(status["retention_days"], 60)


if __name__ == "__main__":
    unittest.main()
