#!/usr/bin/env python3
from pathlib import Path

source = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
auth_source = (Path(__file__).resolve().parents[1] / "auth_security.py").read_text(encoding="utf-8")
provision_source = (Path(__file__).resolve().parents[1] / "scripts" / "provision_admin.py").read_text(encoding="utf-8")
installer_source = (Path(__file__).resolve().parents[1] / "install_on_synology.sh").read_text(encoding="utf-8")
rollback_source = (Path(__file__).resolve().parents[1] / "rollback_on_synology.sh").read_text(encoding="utf-8")
required = [
    "path_under_backup_root(backup_path)",
    "path_under_backup_root(source)",
    "validate_local_image(source.get(\"glpi_image\")",
    "MUTATION_LOCK.acquire(blocking=False)",
    'session["pending_create_preview"]',
    '@app.route("/create/execute", methods=["POST"])',
    '@app.route("/progress/<job_token>", methods=["GET"])',
    "target=run_create_job",
    '"GLPI_SKIP_AUTOINSTALL": "true"',
    'user="33:33"',
    "install_fresh_glpi(project, env)",
    "clear_plugin_data(project)",
    'cat /proc/1/comm',
    '@app.route("/healthz")',
    '@app.route("/set-backup-source", methods=["POST"])',
    '@app.route("/run-backup", methods=["POST"])',
    "subprocess.Popen(",
    "is_managed_glpi_project(",
    "atomic_write_text(backup_schedule_path(project)",
    "def install_backup_dispatcher():",
    "BACKUP_SCHEDULER_DIR.mkdir(parents=True, exist_ok=True)",
    "if not BUILDER_TEST_PREVIEW_MODE:",
    "def require_admin_authentication():",
    'request.endpoint in {"healthz", "favicon", "login", "setup", "ui_javascript"}',
    "authenticated_session_is_current()",
    "write_last_totp_counter(counter)",
    "LOGIN_RATE_MAX_BUCKETS = 1024",
    'SESSION_COOKIE_HTTPONLY=True',
    'SESSION_COOKIE_SAMESITE="Strict"',
]
missing = [item for item in required if item not in source]
if missing:
    raise SystemExit("Missing security controls: " + ", ".join(missing))
auth_required = [
    'PASSWORD_SCHEME = "pbkdf2_sha256"',
    "PASSWORD_ITERATIONS = 600_000",
    "hashlib.pbkdf2_hmac",
    "hashlib.sha1",
    "matching_totp_counter",
    "BUILDER_ADMIN_USERNAME is missing or invalid",
]
missing_auth = [item for item in auth_required if item not in auth_source]
if missing_auth:
    raise SystemExit("Missing authentication primitives: " + ", ".join(missing_auth))
provision_required = ["getpass.getpass", "matching_totp_counter", "os.chmod(path, 0o600)", "BUILDER_ADMIN_PASSWORD_HASH"]
missing_provision = [item for item in provision_required if item not in provision_source]
if missing_provision:
    raise SystemExit("Missing safe provisioning controls: " + ", ".join(missing_provision))
installer_required = [
    'CURRENT_SECRET=$(sed -n \'s/^FLASK_SECRET_KEY=//p\'',
    '[ "$CURRENT_SECRET" = "CHANGE_ME_RANDOM_64_HEX" ]',
    'FLASK_SECRET_KEY=$SECRET',
    'chmod 600 .env',
    'scripts/provision_admin.py" --env "$APP_DIR/.env" --check',
    'docker compose --project-name "$COMPOSE_PROJECT"',
    'up -d --no-build --force-recreate',
    'backup/GLPI_backup_dispatcher.sh" "$BACKUP_DISPATCHER"',
    'Fixed Task Scheduler command: /bin/bash $BACKUP_DISPATCHER',
    "previous container remains stopped",
]
missing_installer = [item for item in installer_required if item not in installer_source]
if missing_installer:
    raise SystemExit("Missing installer security controls: " + ", ".join(missing_installer))
rollback_required = [
    'CANDIDATE="glpi-builder-rollback-proof-',
    '[ -z "$(sudo docker port "$CANDIDATE")" ]',
    "unauthenticated dashboard access was not denied",
    "login page proof failed",
    'sudo docker rename "$CONTAINER" "$CURRENT_SAFE"',
    'sudo docker rm -f "$CONTAINER" >/dev/null 2>&1 || true',
    "authenticated current container was restored",
]
missing_rollback = [item for item in rollback_required if item not in rollback_source]
if missing_rollback:
    raise SystemExit("Missing authenticated rollback proof: " + ", ".join(missing_rollback))
print("OK: static security controls are present")
