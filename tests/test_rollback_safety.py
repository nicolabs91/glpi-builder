#!/usr/bin/env python3
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RollbackSafetyTest(unittest.TestCase):
    def test_created_failed_rollback_container_is_removed_before_current_name_is_restored(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            app_dir = temporary / "app"
            bin_dir = temporary / "bin"
            state_dir = temporary / "state"
            (app_dir / "scripts").mkdir(parents=True)
            bin_dir.mkdir()
            state_dir.mkdir()
            (app_dir / ".last-install-state").write_text(
                "OLD_CONTAINER=builder-old\nIMAGE=unused\n", encoding="utf-8"
            )
            (app_dir / ".env").write_text(
                "BUILDER_BIND_IP=127.0.0.1\nBUILDER_PORT=5055\n", encoding="utf-8"
            )
            (app_dir / "scripts" / "provision_admin.py").write_text("# mocked\n", encoding="utf-8")

            self.write_executable(bin_dir / "sudo", "#!/bin/sh\nexec \"$@\"\n")
            self.write_executable(bin_dir / "python3", "#!/bin/sh\nexit 0\n")
            self.write_executable(bin_dir / "docker", self.fake_docker_script())

            environment = os.environ.copy()
            environment["PATH"] = str(bin_dir) + os.pathsep + environment["PATH"]
            environment["FAKE_DOCKER_STATE"] = str(state_dir)
            result = subprocess.run(
                ["sh", str(ROOT / "rollback_on_synology.sh"), str(app_dir)],
                capture_output=True,
                text=True,
                env=environment,
                timeout=20,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("authenticated current container was restored", result.stderr)
            calls = (state_dir / "calls").read_text(encoding="utf-8").splitlines()
            published_run = max(index for index, call in enumerate(calls) if call.startswith("run "))
            remove_failed = next(
                index for index, call in enumerate(calls)
                if index > published_run and call == "rm -f glpi-builder"
            )
            restore_name = next(
                index for index, call in enumerate(calls)
                if index > remove_failed
                and call.startswith("rename glpi-builder-pre-rollback-")
                and call.endswith(" glpi-builder")
            )
            restart = next(
                index for index, call in enumerate(calls)
                if index > restore_name and call == "start glpi-builder"
            )
            self.assertLess(published_run, remove_failed)
            self.assertLess(remove_failed, restore_name)
            self.assertLess(restore_name, restart)

    @staticmethod
    def write_executable(path, content):
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    @staticmethod
    def fake_docker_script():
        return r'''#!/bin/sh
set -eu
STATE="$FAKE_DOCKER_STATE"
printf '%s\n' "$*" >> "$STATE/calls"
command=$1
shift
case "$command" in
  inspect)
    echo 'sha256:authenticated-old-image'
    ;;
  run)
    count=0
    [ ! -f "$STATE/run-count" ] || count=$(cat "$STATE/run-count")
    count=$((count + 1))
    echo "$count" > "$STATE/run-count"
    if [ "$count" -eq 1 ]; then
      echo 'proof-container-id'
      exit 0
    fi
    # Model Docker creating the requested name before start fails.
    echo 'created' > "$STATE/published-created"
    exit 1
    ;;
  port)
    exit 0
    ;;
  exec)
    case "$*" in
      *"curl"*"/login"*) printf '200' ;;
      *"curl"*) printf '302|http://127.0.0.1:8080/login?next=/' ;;
      *) exit 0 ;;
    esac
    ;;
  rm|stop|rename|start)
    exit 0
    ;;
  *)
    echo "unexpected docker command: $command $*" >&2
    exit 2
    ;;
esac
'''


if __name__ == "__main__":
    unittest.main()
