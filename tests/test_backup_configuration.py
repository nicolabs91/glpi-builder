#!/usr/bin/env python3
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as module
from tests.auth_test_support import authenticate


class ImmediateThread:
    def __init__(self, target, args=(), **_kwargs):
        self.target = target
        self.args = args

    def start(self):
        self.target(*self.args)


class BackupConfigurationTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.base_path = self.root / "docker"
        self.backup_root = self.base_path / "_BACKUPS"
        self.data_root = self.backup_root / "GLPI_backup"
        self.task_dir = self.data_root / "_system"
        self.legacy_task_dir = self.backup_root / "Restore_Scripts" / "GLPI"
        self.scheduler_dir = self.backup_root / "Synology_task_scheduler"
        self.project = "glpi-production"
        project_path = self.base_path / self.project
        (project_path / "db").mkdir(parents=True)
        (project_path / "glpi").mkdir()
        (project_path / "plugins").mkdir()
        (project_path / ".env").write_text(
            "PROJECT_NAME=glpi-production\nGLPI_DB_NAME=glpi\nMARIADB_ROOT_PASSWORD=production-secret\n",
            encoding="utf-8",
        )
        source = Path(module.__file__).resolve().parent / "backup" / "GLPI_backup.sh"
        dispatcher_source = Path(module.__file__).resolve().parent / "backup" / "GLPI_backup_dispatcher.sh"
        self.patches = (
            patch.object(module, "BASE_PATH", self.base_path),
            patch.object(module, "BACKUP_ROOT", self.backup_root),
            patch.object(module, "BACKUP_DATA_ROOT", self.data_root),
            patch.object(module, "BACKUP_TASK_DIR", self.task_dir),
            patch.object(module, "LEGACY_BACKUP_TASK_DIR", self.legacy_task_dir),
            patch.object(module, "BACKUP_SCHEDULER_DIR", self.scheduler_dir),
            patch.object(module, "BACKUP_SCRIPT_SOURCE", source),
            patch.object(module, "BACKUP_SCRIPT_PATH", self.task_dir / "GLPI_backup.sh"),
            patch.object(module, "BACKUP_ENV_PATH", self.task_dir / "GLPI_backup.env"),
            patch.object(module, "BACKUP_CNF_PATH", self.task_dir / "GLPI_mysql_backup.cnf"),
            patch.object(module, "BACKUP_DISPATCHER_SOURCE", dispatcher_source),
            patch.object(module, "BACKUP_DISPATCHER_PATH", self.scheduler_dir / "GLPI_backup_dispatcher.sh"),
            patch.object(module, "BACKUP_PROJECTS_DIR", self.task_dir / "projects"),
            patch.object(module, "BACKUP_CREDENTIALS_DIR", self.task_dir / "credentials"),
            patch.object(module, "BACKUP_STATE_DIR", self.task_dir / "state"),
            patch.object(module, "BACKUP_LOCKS_DIR", self.task_dir / "locks"),
            patch.object(module, "database_container_for_project", return_value="glpi-prod-mdb1222"),
        )
        for context in self.patches:
            context.start()

    def tearDown(self):
        if module.MUTATION_LOCK.locked():
            module.MUTATION_LOCK.release()
        for context in reversed(self.patches):
            context.stop()
        self.temporary_directory.cleanup()

    def test_configuration_tracks_project_without_hardcoded_folder_name(self):
        messages = module.configure_scheduled_backup(self.project)

        config = module.read_simple_env_file(module.backup_schedule_path(self.project))
        self.assertEqual(config["PROJECT_NAME"], self.project)
        self.assertEqual(config["PROJECT_DIR"], str(self.base_path / self.project))
        self.assertEqual(config["DB_CONTAINER"], "glpi-prod-mdb1222")
        self.assertEqual(config["DB_NAME"], "glpi")
        self.assertEqual(config["RETENTION_DAYS"], "60")
        self.assertEqual(config["BACKUP_ROOT"], str(self.data_root))
        self.assertEqual(
            config["MYSQL_CNF"],
            str(module.BACKUP_CREDENTIALS_DIR / f"{self.project}.cnf"),
        )
        self.assertEqual(
            os.stat(config["MYSQL_CNF"]).st_mode & 0o777,
            0o600,
        )
        self.assertEqual(module.current_backup_source_project(), self.project)
        self.assertEqual(os.stat(module.backup_schedule_path(self.project)).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(module.BACKUP_SCRIPT_PATH).st_mode & 0o777, 0o750)
        self.assertEqual(os.stat(module.BACKUP_DISPATCHER_PATH).st_mode & 0o777, 0o750)
        for directory in (
            module.BACKUP_TASK_DIR,
            module.BACKUP_PROJECTS_DIR,
            module.BACKUP_CREDENTIALS_DIR,
            module.BACKUP_STATE_DIR,
            module.BACKUP_LOCKS_DIR,
        ):
            self.assertEqual(os.stat(directory).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(module.BACKUP_SCHEDULER_DIR).st_mode & 0o777, 0o750)
        self.assertEqual(
            module.BACKUP_DISPATCHER_PATH,
            self.backup_root / "Synology_task_scheduler" / "GLPI_backup_dispatcher.sh",
        )
        self.assertTrue(any("Task Scheduler command" in message for message in messages))

    def test_dispatcher_install_creates_missing_scheduler_directory(self):
        self.assertFalse(self.scheduler_dir.exists())

        module.install_backup_dispatcher()

        self.assertTrue(module.BACKUP_DISPATCHER_PATH.is_file())
        self.assertEqual(
            os.stat(module.BACKUP_DISPATCHER_PATH).st_mode & 0o777,
            0o750,
        )
        self.assertIn(
            "Managed by GLPI Builder",
            module.BACKUP_DISPATCHER_PATH.read_text(encoding="utf-8"),
        )

    def test_backup_runtime_rejects_managed_directory_symlink(self):
        self.task_dir.mkdir(parents=True)
        target = self.root / "outside-state"
        target.mkdir()
        (self.task_dir / "state").symlink_to(target, target_is_directory=True)

        with self.assertRaisesRegex(ValueError, "symlink"):
            module.install_backup_runtime()

    def test_existing_unmanaged_script_is_preserved_once(self):
        self.task_dir.mkdir(parents=True)
        module.BACKUP_SCRIPT_PATH.write_text("#!/bin/bash\necho legacy\n", encoding="utf-8")

        module.configure_scheduled_backup(self.project)
        legacy = self.task_dir / "GLPI_backup.pre-builder.sh"

        self.assertIn("echo legacy", legacy.read_text(encoding="utf-8"))
        self.assertIn("Managed by GLPI Builder", module.BACKUP_SCRIPT_PATH.read_text(encoding="utf-8"))

    def test_legacy_runtime_is_copied_and_rewritten_without_deletion(self):
        legacy_projects = self.legacy_task_dir / "projects"
        legacy_state = self.legacy_task_dir / "state"
        legacy_projects.mkdir(parents=True)
        legacy_state.mkdir()
        legacy_config = legacy_projects / f"{self.project}.env"
        legacy_config.write_text(
            "\n".join(
                [
                    f"PROJECT_NAME={self.project}",
                    f"BACKUP_ROOT={self.backup_root}",
                    f"MYSQL_CNF={self.legacy_task_dir / 'GLPI_mysql_backup.cnf'}",
                    "SCHEDULE_ENABLED=yes",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        legacy_cnf = self.legacy_task_dir / "GLPI_mysql_backup.cnf"
        legacy_cnf.write_text("[client]\npassword=legacy-test\n", encoding="utf-8")
        legacy_log = legacy_state / f"{self.project}.last.log"
        legacy_log.write_bytes(b"legacy state\\xff")

        module.install_backup_runtime()

        migrated = module.read_simple_env_file(module.backup_schedule_path(self.project))
        self.assertEqual(migrated["BACKUP_ROOT"], str(self.data_root))
        self.assertEqual(
            migrated["MYSQL_CNF"],
            str(module.project_backup_credential_path(self.project)),
        )
        self.assertEqual(
            (module.BACKUP_STATE_DIR / legacy_log.name).read_bytes(),
            b"legacy state\\xff",
        )
        self.assertTrue(legacy_config.exists())
        self.assertTrue(legacy_cnf.exists())
        self.assertTrue(legacy_log.exists())

    def test_backup_status_reports_readiness_and_latest_verified_backup(self):
        module.configure_scheduled_backup(self.project)
        backup = self.data_root / self.project / "2026-07-11_120000"
        backup.mkdir(parents=True)
        (backup / "manifest.json").write_text(
            '{"project":"glpi-production","created_at":"2026-07-11T12:00:00+0200",'
            '"database":"database.sql.gz","files":"files.tar.gz"}',
            encoding="utf-8",
        )
        (backup / "database.sql.gz").write_bytes(b"database")
        (backup / "files.tar.gz").write_bytes(b"files")
        (backup / "SHA256SUMS").write_text("checksums\n", encoding="utf-8")

        database = type("Database", (), {"status": "running", "reload": lambda self: None})()
        with patch.object(module, "get_container", return_value=database):
            status = module.scheduled_backup_status(self.project)

        self.assertTrue(status["selected"])
        self.assertTrue(status["ready"])
        self.assertEqual(status["issues"], [])
        self.assertEqual(status["latest"]["name"], backup.name)
        self.assertEqual(status["latest"]["created_at"], "2026-07-11T12:00:00+0200")
        self.assertEqual(status["latest"]["size_bytes"], 13)
        self.assertTrue(status["latest"]["checksum_manifest"])

    def test_backup_status_explains_missing_credentials(self):
        (self.base_path / self.project / ".env").write_text(
            "PROJECT_NAME=glpi-production\nGLPI_DB_NAME=glpi\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "root password"):
            module.configure_scheduled_backup(self.project)

    def test_projects_receive_distinct_private_credentials(self):
        second = "glpi-test"
        second_path = self.base_path / second
        (second_path / "db").mkdir(parents=True)
        (second_path / "glpi").mkdir()
        (second_path / "plugins").mkdir()
        (second_path / ".env").write_text(
            "PROJECT_NAME=glpi-test\nGLPI_DB_NAME=glpi\nMARIADB_ROOT_PASSWORD=test-secret\n",
            encoding="utf-8",
        )

        module.configure_scheduled_backup(self.project)
        module.configure_scheduled_backup(second)
        first_config = module.read_simple_env_file(module.backup_schedule_path(self.project))
        second_config = module.read_simple_env_file(module.backup_schedule_path(second))
        first_cnf = Path(first_config["MYSQL_CNF"])
        second_cnf = Path(second_config["MYSQL_CNF"])
        self.assertNotEqual(first_cnf, second_cnf)
        self.assertIn('password="production-secret"', first_cnf.read_text(encoding="utf-8"))
        self.assertIn('password="test-secret"', second_cnf.read_text(encoding="utf-8"))
        self.assertEqual(os.stat(first_cnf).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(second_cnf).st_mode & 0o777, 0o600)
        database = type("Database", (), {"status": "running", "reload": lambda self: None})()
        with patch.object(module, "get_container", return_value=database):
            self.assertTrue(module.scheduled_backup_status(self.project)["ready"])
            self.assertTrue(module.scheduled_backup_status(second)["ready"])

    def test_existing_schedule_migrates_from_global_to_project_credential(self):
        module.install_backup_runtime()
        legacy_global = self.task_dir / "GLPI_mysql_backup.cnf"
        legacy_global.write_text("[client]\npassword=old-shared-secret\n", encoding="utf-8")
        schedule = module.backup_schedule_path(self.project)
        schedule.write_text(
            "\n".join(
                [
                    f"PROJECT_NAME={self.project}",
                    f"PROJECT_DIR={self.base_path / self.project}",
                    "DB_CONTAINER=glpi-prod-mdb1222",
                    "DB_NAME=glpi",
                    f"BACKUP_ROOT={self.data_root}",
                    f"MYSQL_CNF={legacy_global}",
                    "CONTAINER_CNF=/tmp/GLPI_mysql_backup.cnf",
                    "RETENTION_DAYS=60",
                    "SCHEDULE_ENABLED=yes",
                    "SCHEDULE_KIND=daily",
                    "SCHEDULE_TIME=02:00",
                    "SCHEDULE_WEEKDAYS=7",
                    "INTERVAL_HOURS=24",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        migrated = module.migrate_project_backup_credentials()

        configured = module.read_simple_env_file(schedule)
        credential = module.project_backup_credential_path(self.project)
        self.assertEqual(migrated, [str(schedule)])
        self.assertEqual(configured["MYSQL_CNF"], str(credential))
        self.assertIn('password="production-secret"', credential.read_text(encoding="utf-8"))
        self.assertEqual(os.stat(credential).st_mode & 0o777, 0o600)
        self.assertIn("old-shared-secret", legacy_global.read_text(encoding="utf-8"))

    def test_project_credential_rejects_symlinked_environment(self):
        environment = self.base_path / self.project / ".env"
        outside = self.root / "outside.env"
        outside.write_text("MARIADB_ROOT_PASSWORD=outside-secret\n", encoding="utf-8")
        environment.unlink()
        environment.symlink_to(outside)

        with self.assertRaisesRegex(ValueError, "symlink"):
            module.ensure_project_backup_credential(self.project)

    def test_dispatcher_detection_accepts_scheduler_fallback_heartbeat(self):
        fallback = self.scheduler_dir / ".dispatcher.env"
        fallback.parent.mkdir(parents=True)
        fallback.write_text(
            f"LAST_HEARTBEAT={int(module.time.time())}\nSTATUS=idle\n",
            encoding="utf-8",
        )

        self.assertTrue(module.dispatcher_is_healthy())

    def test_manual_backup_route_starts_a_backup_progress_job(self):
        module.app.config.update(TESTING=True, SECRET_KEY="backup-route-test-secret")
        client = module.app.test_client()
        authenticate(client, module)
        with client.session_transaction() as flask_session:
            flask_session["csrf_token"] = "backup-csrf"

        def complete_job(job_token, _project):
            module.update_progress_job(job_token, 100, "Backup completed", status="completed")
            module.MUTATION_LOCK.release()

        with patch.object(module, "scheduled_backup_status", return_value={"ready": True, "issues": []}), \
             patch.object(module, "run_backup_job", side_effect=complete_job), \
             patch.object(module.threading, "Thread", ImmediateThread):
            response = client.post(
                "/run-backup",
                data={"csrf_token": "backup-csrf", "project": self.project},
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/progress/", response.headers["Location"])
        progress = client.get(response.headers["Location"])
        self.assertEqual(progress.status_code, 200)
        self.assertIn(b"Backup progress", progress.data)
        self.assertIn(b"Backup completed", progress.data)

    def test_bundled_script_has_valid_bash_syntax_and_safety_guards(self):
        script = Path(module.__file__).resolve().parent / "backup" / "GLPI_backup.sh"
        result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        source = script.read_text(encoding="utf-8")
        self.assertIn("GLPI_backup.env", source)
        self.assertIn("Another GLPI backup is already running", source)
        self.assertIn("TEMP_BACKUP_DIR", source)
        self.assertIn('tar -C "$PROJECT_DIR"', source)
        self.assertIn("SHA256SUMS", source)
        self.assertIn('find "$PROJECT_BACKUP_ROOT"', source)

    @unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0, "requires an isolated root test container")
    def test_backup_script_runs_end_to_end_with_portable_archive(self):
        isolated_root = Path("/volume1/docker/glpi-backup-script-test")
        isolated_backups = Path("/volume1/docker/_BACKUPS_SCRIPT_TEST")
        shutil.rmtree(isolated_root, ignore_errors=True)
        shutil.rmtree(isolated_backups, ignore_errors=True)
        try:
            (isolated_root / "glpi" / "config").mkdir(parents=True)
            (isolated_root / "glpi" / "files" / "_cache").mkdir(parents=True)
            (isolated_root / "plugins").mkdir(parents=True)
            (isolated_root / "glpi" / "config" / "config_db.php").write_text("config", encoding="utf-8")
            (isolated_root / "glpi" / "files" / "_cache" / "cache.bin").write_text("cache", encoding="utf-8")
            (isolated_root / "plugins" / "plugin.txt").write_text("plugin", encoding="utf-8")

            with tempfile.TemporaryDirectory() as temporary_directory:
                temporary = Path(temporary_directory)
                task_dir = temporary / "task"
                bin_dir = temporary / "bin"
                task_dir.mkdir()
                bin_dir.mkdir()
                script = task_dir / "GLPI_backup.sh"
                shutil.copy2(Path(module.__file__).resolve().parent / "backup" / "GLPI_backup.sh", script)
                script.chmod(0o750)
                cnf = task_dir / "GLPI_mysql_backup.cnf"
                cnf.write_text("[client]\nuser=root\n", encoding="utf-8")
                (task_dir / "GLPI_backup.env").write_text(
                    "\n".join([
                        "PROJECT_NAME=glpi-backup-script-test",
                        f"PROJECT_DIR={isolated_root}",
                        "DB_CONTAINER=glpi-backup-script-test-db",
                        "DB_NAME=glpi",
                        f"BACKUP_ROOT={isolated_backups}",
                        f"MYSQL_CNF={cnf}",
                        "CONTAINER_CNF=/tmp/GLPI_mysql_backup.cnf",
                        "RETENTION_DAYS=60",
                        "",
                    ]),
                    encoding="utf-8",
                )
                fake_docker = bin_dir / "docker"
                fake_docker.write_text(
                    "#!/bin/bash\n"
                    "set -e\n"
                    "case \"$1\" in\n"
                    "  info) exit 0 ;;\n"
                    "  inspect) if [ \"${2:-}\" = \"--format\" ]; then echo true; fi; exit 0 ;;\n"
                    "  cp) exit 0 ;;\n"
                    "  exec) if [ \"${3:-}\" = \"mariadb-dump\" ]; then echo 'CREATE TABLE glpi_test (id INT);'; fi; exit 0 ;;\n"
                    "esac\n",
                    encoding="utf-8",
                )
                fake_docker.chmod(0o750)
                environment = dict(os.environ)
                environment["PATH"] = f"{bin_dir}:{environment['PATH']}"
                result = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=environment)
                self.assertEqual(result.returncode, 0, result.stderr)

                backup_directories = list(
                    (isolated_backups / "glpi-backup-script-test").glob("20??-??-??_??????")
                )
                self.assertEqual(len(backup_directories), 1)
                backup = backup_directories[0]
                self.assertGreater((backup / "database.sql.gz").stat().st_size, 0)
                self.assertTrue((backup / "SHA256SUMS").is_file())
                self.assertIn(
                    '"project": "glpi-backup-script-test"',
                    (backup / "manifest.json").read_text(encoding="utf-8"),
                )
                self.assertIn("database.sql.gz", (backup / "SHA256SUMS").read_text(encoding="utf-8"))
                with tarfile.open(backup / "files.tar.gz", "r:gz") as archive:
                    members = set(archive.getnames())
                self.assertIn("glpi/config/config_db.php", members)
                self.assertIn("plugins/plugin.txt", members)
                self.assertNotIn("glpi/files/_cache/cache.bin", members)
        finally:
            shutil.rmtree(isolated_root, ignore_errors=True)
            shutil.rmtree(isolated_backups, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
