import os
import re
import time
import shutil
import secrets
import string
import tempfile
import zipfile
import tarfile
import stat
import hmac
import json
import gzip
import hashlib
import ipaddress
import threading
import subprocess
from datetime import datetime, timedelta
from html import escape as html_escape
from pathlib import Path
from urllib.parse import unquote, urlsplit

import docker
from docker.errors import NotFound, ContainerError
from flask import Flask, request, redirect, url_for, render_template_string, flash, session, g, jsonify, abort, make_response, has_request_context

from auth_security import (
    generate_totp_secret,
    hash_password,
    load_auth_config,
    matching_totp_counter,
    verify_password,
)
from app_ui import (
    ACTIVITY,
    APPLICATION_PREVIEW,
    APPLICATION_DETAIL,
    APPLICATION_WIZARD,
    BACKUPS,
    COMPOSE_VIEW,
    OVERVIEW,
    PROJECT_DETAIL,
    PROJECTS,
    SETTINGS,
    WIZARD,
    page_template,
)
from app_profiles import (
    MANIFEST_NAME,
    build_environment as build_application_environment,
    get_profile,
    manifest as build_application_manifest,
    profile_catalog,
    read_manifest as read_application_manifest,
    render_compose as render_application_compose,
    validate_image as validate_application_image,
    validate_database_image as validate_application_database_image,
    validate_port as validate_application_port,
    validate_project_name as validate_application_project,
)

APP_VERSION = "0.5.0-rc.6"
TPM_BACKUP_MANIFEST = "tpm-backup.json"
QUARANTINE_REPORT = "quarantine-report.json"
QUARANTINE_DEFAULT_DAYS = 14
BUILDER_TEST_PREVIEW_MODE = os.environ.get(
    "BUILDER_TEST_PREVIEW_MODE", "0"
).strip().lower() in {"1", "true", "yes", "on"}
APP_PORT = int(os.environ.get("APP_PORT", "8080"))
BASE_PATH = Path(os.environ.get("BASE_PATH", "/volume1/docker"))
BACKUP_ROOT = Path(os.environ.get("BACKUP_ROOT", "/volume1/docker/_BACKUPS"))
BACKUP_DATA_ROOT = Path(
    os.environ.get("BACKUP_DATA_ROOT", str(BACKUP_ROOT / "GLPI_backup"))
)
BACKUP_TASK_DIR = Path(
    os.environ.get("BACKUP_TASK_DIR", str(BACKUP_DATA_ROOT / "_system"))
)
LEGACY_BACKUP_TASK_DIR = BACKUP_ROOT / "Restore_Scripts" / "GLPI"
BACKUP_SCHEDULER_DIR = Path(
    os.environ.get(
        "BACKUP_SCHEDULER_DIR",
        str(BACKUP_ROOT / "Synology_task_scheduler"),
    )
)
BACKUP_SCRIPT_SOURCE = Path(__file__).resolve().parent / "backup" / "GLPI_backup.sh"
BACKUP_DISPATCHER_SOURCE = Path(__file__).resolve().parent / "backup" / "GLPI_backup_dispatcher.sh"
BACKUP_SCRIPT_PATH = BACKUP_TASK_DIR / "Application_backup.sh"
BACKUP_DISPATCHER_PATH = BACKUP_SCHEDULER_DIR / "Application_backup_dispatcher.sh"
BACKUP_ENV_PATH = BACKUP_TASK_DIR / "GLPI_backup.env"
BACKUP_PROJECTS_DIR = BACKUP_TASK_DIR / "projects"
BACKUP_CREDENTIALS_DIR = BACKUP_TASK_DIR / "credentials"
BACKUP_STATE_DIR = BACKUP_TASK_DIR / "state"
BACKUP_LOCKS_DIR = BACKUP_TASK_DIR / "locks"
BACKUP_CNF_PATH = BACKUP_TASK_DIR / "GLPI_mysql_backup.cnf"
TZ_DEFAULT = os.environ.get("TZ", "Europe/Brussels")
MAX_SCAN_ENTRIES = int(os.environ.get("MAX_SCAN_ENTRIES", "500"))
ALLOWED_GLPI_IMAGES = tuple(filter(None, os.environ.get("ALLOWED_GLPI_IMAGES", "glpi/glpi:,diouxx/glpi:").split(",")))
ALLOWED_DB_IMAGES = tuple(filter(None, os.environ.get("ALLOWED_DB_IMAGES", "mariadb:,mysql:").split(",")))
MUTATION_LOCK = threading.Lock()
PROGRESS_LOCK = threading.Lock()
PROGRESS_JOBS = {}
DASHBOARD_CACHE_LOCK = threading.Lock()
DASHBOARD_CACHE_SECONDS = 5
DASHBOARD_CACHE = {"expires_at": 0.0, "containers": (), "image_tags": ()}
DEFAULT_SESSION_COOKIE_SAMESITE = os.environ.get("DEFAULT_GLPI_SESSION_COOKIE_SAMESITE", "Lax")
DEFAULT_SESSION_COOKIE_SECURE = os.environ.get("DEFAULT_GLPI_SESSION_COOKIE_SECURE", "Off")
COOKIE_SAMESITE_CHOICES = ("Lax", "Strict", "None")
COOKIE_SECURE_CHOICES = ("Off", "On")
CREATE_PREVIEW_TTL_SECONDS = 10 * 60
PROGRESS_JOB_TTL_SECONDS = 6 * 60 * 60
OPERATION_MODES = ("restore", "fresh", "isolated")
BACKUP_STALE_DAYS = 30


def format_backup_interval(hours):
    """Present compatible hour-based schedule values in human units."""
    value = safe_int(hours, 24, 1)
    if value % (24 * 30) == 0:
        count, unit = value // (24 * 30), "month"
    elif value % (24 * 7) == 0:
        count, unit = value // (24 * 7), "week"
    elif value % 24 == 0:
        count, unit = value // 24, "day"
    else:
        count, unit = value, "hour"
    return f"Every {count} {unit}{'' if count == 1 else 's'}"

PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,50}$")
CONTAINER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,127}$")
LOG_FILE_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[a-z0-9_-]+\.log$")
SAFE_ACTION_RE = re.compile(r"[^a-z0-9_-]+")
SAFE_CHARS = string.ascii_letters + string.digits
DB_EXTENSIONS = (".sql", ".sql.gz", ".dump", ".dump.gz")
FILE_EXTENSIONS = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")
FILE_CHOICE_EXTENSIONS = (".tar.gz",)
APACHE_REQUEST_LINE_LIMIT = 32768
GLPI_INTERNAL_PORT = 8080
UI_JAVASCRIPT = r"""
(() => {
  const checked = document.querySelector('[data-last-checked]');
  const refresh = async () => {
    try {
      const response = await fetch('/api/status', {headers: {'Accept': 'application/json'}});
      if (!response.ok) return;
      const payload = await response.json();
      for (const project of payload.projects || []) {
        for (const element of document.querySelectorAll(`[data-project-status="${CSS.escape(project.name)}"]`)) {
          element.textContent = `${project.glpi} / ${project.database}`;
          element.classList.toggle('ready', project.glpi === 'running' && project.database === 'running');
          element.classList.toggle('warning', project.glpi !== 'running' || project.database !== 'running');
        }
      }
      if (checked) checked.textContent = `Last checked ${payload.checked_at}`;
    } catch (_) {}
  };
  document.querySelectorAll('[data-refresh-status]').forEach((button) => {
    button.addEventListener('click', refresh);
  });
  document.querySelectorAll('[data-dialog-open]').forEach((button) => {
    button.addEventListener('click', () => {
      const dialog = document.getElementById(button.dataset.dialogOpen);
      if (dialog && dialog.showModal) dialog.showModal();
    });
  });
  document.querySelectorAll('[data-dialog-close]').forEach((button) => {
    button.addEventListener('click', () => button.closest('dialog')?.close());
  });
  document.querySelectorAll('[data-schedule-editor]').forEach((editor) => {
    const frequency = editor.querySelector('[name="schedule_kind"]');
    const update = () => {
      const kind = frequency?.value || 'daily';
      editor.querySelectorAll('[data-for-frequency]').forEach((field) => {
        field.hidden = !field.dataset.forFrequency.split(' ').includes(kind);
      });
    };
    frequency?.addEventListener('change', update);
    update();
  });
  document.querySelectorAll('[data-default-image]').forEach((choice) => {
    choice.addEventListener('change', () => {
      if (!choice.checked) return;
      const image = choice.closest('form')?.querySelector('[name="image"]');
      if (image) image.value = choice.dataset.defaultImage || '';
    });
  });
  document.querySelectorAll('[data-copy-command]').forEach((button) => {
    button.addEventListener('click', async () => {
      const command = document.getElementById('dispatcher-command')?.textContent.trim();
      if (!command) return;
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(command);
        button.textContent = 'Copied';
      } else {
        const range = document.createRange();
        range.selectNodeContents(document.getElementById('dispatcher-command'));
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        button.textContent = 'Selected';
      }
      window.setTimeout(() => { button.textContent = 'Copy command'; }, 1800);
    });
  });
  if (checked) window.setInterval(refresh, 20000);
})();
"""

GLPI_WRITABLE_SUBDIRS = [
    "files/_cache",
    "files/_cron",
    "files/_dumps",
    "files/_graphs",
    "files/_lock",
    "files/_log",
    "files/_pictures",
    "files/_plugins",
    "files/_rss",
    "files/_sessions",
    "files/_tmp",
    "files/_uploads",
]

ENV_ORDER = [
    "PROJECT_NAME",
    "GLPI_IMAGE",
    "MARIADB_IMAGE",
    "GLPI_HTTP_PORT",
    "GLPI_CONTAINER_PORT",
    "GLPI_SESSION_COOKIE_SAMESITE",
    "GLPI_SESSION_COOKIE_SECURE",
    "MARIADB_ROOT_PASSWORD",
    "GLPI_DB_NAME",
    "GLPI_DB_USER",
    "GLPI_DB_PASSWORD",
    "TZ",
]

GLPI_ENTRY_COMMAND = """SAMESITE="${GLPI_SESSION_COOKIE_SAMESITE:-Lax}" &&
case "$SAMESITE" in Lax|Strict|None) ;; *) SAMESITE="Lax" ;; esac &&
SECURE="${GLPI_SESSION_COOKIE_SECURE:-Off}" &&
case "$SECURE" in On|Off) ;; *) SECURE="Off" ;; esac &&
if [ "$SAMESITE" = "None" ]; then SECURE="On"; fi &&
mkdir -p /tmp/glpi-builder-php &&
printf '%s\\n' '; Generated by GLPI Builder' "session.cookie_samesite = \\\"$SAMESITE\\\"" 'session.cookie_httponly = On' "session.cookie_secure = $SECURE" > /tmp/glpi-builder-php/99-glpi-builder-session.ini &&
for dir in /etc/php/*/apache2/conf.d /etc/php/*/fpm/conf.d /etc/php/*/cli/conf.d /usr/local/etc/php/conf.d /etc/php/conf.d; do
  if [ -d "$dir" ]; then
    cp /tmp/glpi-builder-php/99-glpi-builder-session.ini "$dir/99-glpi-builder-session.ini";
  fi;
done &&
mkdir -p /var/glpi/files/_cache /var/glpi/files/_cron /var/glpi/files/_dumps /var/glpi/files/_graphs /var/glpi/files/_lock /var/glpi/files/_log /var/glpi/files/_pictures /var/glpi/files/_plugins /var/glpi/files/_rss /var/glpi/files/_sessions /var/glpi/files/_tmp /var/glpi/files/_uploads /var/www/glpi/plugins &&
chown -R 33:33 /var/glpi /var/www/glpi/plugins /var/log/apache2 /var/run/apache2 /run/apache2 &&
chmod -R 775 /var/glpi /var/www/glpi/plugins &&
chmod -R 777 /var/log/apache2 /var/run/apache2 /run/apache2 &&
mkdir -p /etc/apache2/conf-enabled &&
printf '%s\\n' '# Generated by GLPI Builder' 'LimitRequestLine __REQUEST_LINE_LIMIT__' > /etc/apache2/conf-enabled/99-glpi-builder-request-limits.conf &&
sed -i 's/^Listen .*/Listen __GLPI_INTERNAL_PORT__/' /etc/apache2/ports.conf &&
sed -i -E 's|<VirtualHost \\*:80>|<VirtualHost *:__GLPI_INTERNAL_PORT__>|g' /etc/apache2/sites-available/*.conf &&
sed -i 's|/dev/stdout|/tmp/stdout.log|g' /etc/supervisor/supervisord.conf &&
sed -i 's|/dev/stderr|/tmp/stderr.log|g' /etc/supervisor/supervisord.conf &&
touch /tmp/supervisord.log /tmp/stdout.log /tmp/stderr.log &&
chmod 666 /tmp/supervisord.log /tmp/stdout.log /tmp/stderr.log &&
exec /opt/glpi/entrypoint.sh /usr/bin/supervisord -c /etc/supervisor/supervisord.conf"""
GLPI_ENTRY_COMMAND = GLPI_ENTRY_COMMAND.replace(
    "__REQUEST_LINE_LIMIT__", str(APACHE_REQUEST_LINE_LIMIT)
).replace("__GLPI_INTERNAL_PORT__", str(GLPI_INTERNAL_PORT))

AUTH_CONFIG_PATH = Path(os.environ.get("BUILDER_CONFIG_PATH", "/config/builder-auth.json"))


def read_persisted_auth():
    if not AUTH_CONFIG_PATH.is_file():
        return {}
    try:
        if AUTH_CONFIG_PATH.stat().st_mode & 0o077:
            raise ValueError("Persisted authentication configuration is not private.")
        data = json.loads(AUTH_CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Persisted authentication configuration is invalid.")
        return {str(key): str(value) for key, value in data.items()}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unable to load persisted authentication configuration: {exc}") from exc


def atomic_write_persisted_auth(data):
    AUTH_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(AUTH_CONFIG_PATH.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".builder-auth-", dir=AUTH_CONFIG_PATH.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, AUTH_CONFIG_PATH)
        os.chmod(AUTH_CONFIG_PATH, 0o600)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


try:
    PERSISTED_AUTH = read_persisted_auth()
    PERSISTED_AUTH_ERROR = ""
except ValueError as exc:
    PERSISTED_AUTH = {}
    PERSISTED_AUTH_ERROR = str(exc)

app = Flask(__name__)
app.jinja_env.filters["backup_interval"] = format_backup_interval
configured_flask_secret = (
    os.environ.get("FLASK_SECRET_KEY", "").strip()
    or PERSISTED_AUTH.get("FLASK_SECRET_KEY", "")
)
flask_secret_error = ""
if len(configured_flask_secret) < 32 or configured_flask_secret in {"CHANGE_ME_RANDOM_64_HEX", "CHANGE_ME"}:
    flask_secret_error = "FLASK_SECRET_KEY is missing, placeholder, or too short."
app.secret_key = configured_flask_secret or secrets.token_hex(32)
try:
    AUTH_CONFIG = load_auth_config({**os.environ, **PERSISTED_AUTH})
    AUTH_CONFIG_ERROR = "; ".join(filter(None, (PERSISTED_AUTH_ERROR, flask_secret_error)))
except ValueError as exc:
    AUTH_CONFIG = None
    AUTH_CONFIG_ERROR = "; ".join(filter(None, (PERSISTED_AUTH_ERROR, flask_secret_error, str(exc))))

SETUP_TOKEN = ""
if AUTH_CONFIG is None and not AUTH_CONFIG_PATH.exists() and not PERSISTED_AUTH_ERROR:
    SETUP_TOKEN = secrets.token_urlsafe(24)
    print(
        f"Docker App Manager first-time setup token: {SETUP_TOKEN}",
        flush=True,
    )
elif PERSISTED_AUTH_ERROR:
    print(
        "Docker App Manager authentication recovery required: "
        f"{PERSISTED_AUTH_ERROR} "
        "No setup token was generated because an authentication file still exists. "
        "Preserve credentials by correcting its permissions, or stop the project and "
        "run reset_setup_on_synology.sh --confirm-reset for a backed-up fresh setup.",
        flush=True,
    )

app.config.update(
    SESSION_COOKIE_NAME="glpi_builder_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=(
        AUTH_CONFIG.cookie_secure
        if AUTH_CONFIG
        else os.environ.get("BUILDER_SESSION_COOKIE_SECURE", "false").strip().lower()
        in {"1", "true", "yes", "on"}
    ),
)

LOGIN_RATE_LOCK = threading.Lock()
LOGIN_RATE_BUCKETS = {}
LOGIN_RATE_MAX_BUCKETS = 1024
LOGIN_RATE_WINDOW_SECONDS = 5 * 60
LOGIN_RATE_MAX_FAILURES = 5
LOGIN_RATE_BLOCK_SECONDS = 15 * 60
LOGIN_RATE_GLOBAL_MAX_FAILURES = 25
TOTP_REPLAY_LOCK = threading.Lock()
AUTH_STATE_PATH = Path(os.environ.get("BUILDER_AUTH_STATE_PATH", "/volume1/docker/.docker-app-manager-auth-state"))


def docker_client():
    return docker.from_env()


def close_docker_client(client):
    try:
        client.close()
    except Exception:
        pass


def invalidate_dashboard_cache():
    with DASHBOARD_CACHE_LOCK:
        DASHBOARD_CACHE.update(expires_at=0.0, containers=(), image_tags=())


def dashboard_docker_snapshot():
    """Return one short-lived Docker snapshot for an entire dashboard render."""
    now = time.monotonic()
    with DASHBOARD_CACHE_LOCK:
        if DASHBOARD_CACHE["expires_at"] > now:
            return {
                "containers": list(DASHBOARD_CACHE["containers"]),
                "image_tags": tuple(DASHBOARD_CACHE["image_tags"]),
            }
        containers = []
        image_tags = ()
        try:
            client = docker_client()
        except Exception:
            client = None
        if client is not None:
            try:
                containers = client.containers.list(all=True)
                image_tags = tuple(sorted({tag for image in client.images.list() for tag in (image.tags or [])}))
            except Exception:
                pass
            finally:
                close_docker_client(client)
        DASHBOARD_CACHE.update(
            expires_at=now + DASHBOARD_CACHE_SECONDS,
            containers=tuple(containers),
            image_tags=tuple(image_tags),
        )
        return {"containers": list(containers), "image_tags": tuple(image_tags)}


def esc(value):
    return html_escape(str(value), quote=True)


def safe_pre(value):
    return "<pre>" + esc(value) + "</pre>"


def safe_code(value):
    return "<code>" + esc(value) + "</code>"


def text_to_html(value):
    return esc(value).replace("\n", "<br>")


def tail_text(value, chars=5000):
    value = str(value or "")
    if len(value) <= chars:
        return value
    return value[-chars:]


def safe_password(length=30):
    return "".join(secrets.choice(SAFE_CHARS) for _ in range(length))


def login_rate_key():
    return str(request.remote_addr or "unknown")[:128]


def _prune_login_rate_buckets(now):
    expired = [
        key for key, bucket in LOGIN_RATE_BUCKETS.items()
        if bucket.get("blocked_until", 0) <= now
        and not [stamp for stamp in bucket.get("failures", ()) if now - stamp <= LOGIN_RATE_WINDOW_SECONDS]
    ]
    for key in expired:
        LOGIN_RATE_BUCKETS.pop(key, None)
    if len(LOGIN_RATE_BUCKETS) > LOGIN_RATE_MAX_BUCKETS:
        oldest = sorted(
            (key for key in LOGIN_RATE_BUCKETS if key != "__global__"),
            key=lambda key: LOGIN_RATE_BUCKETS[key].get("updated", 0),
        )
        for key in oldest[:len(LOGIN_RATE_BUCKETS) - LOGIN_RATE_MAX_BUCKETS]:
            LOGIN_RATE_BUCKETS.pop(key, None)


def login_rate_is_blocked(key, now=None):
    now = time.monotonic() if now is None else now
    with LOGIN_RATE_LOCK:
        _prune_login_rate_buckets(now)
        return (
            LOGIN_RATE_BUCKETS.get(key, {}).get("blocked_until", 0) > now
            or LOGIN_RATE_BUCKETS.get("__global__", {}).get("blocked_until", 0) > now
        )


def login_rate_record_failure(key, now=None):
    now = time.monotonic() if now is None else now
    with LOGIN_RATE_LOCK:
        _prune_login_rate_buckets(now)
        bucket = LOGIN_RATE_BUCKETS.setdefault(key, {"failures": [], "blocked_until": 0, "updated": now})
        bucket["failures"] = [stamp for stamp in bucket["failures"] if now - stamp <= LOGIN_RATE_WINDOW_SECONDS]
        bucket["failures"].append(now)
        bucket["updated"] = now
        if len(bucket["failures"]) >= LOGIN_RATE_MAX_FAILURES:
            bucket["blocked_until"] = now + LOGIN_RATE_BLOCK_SECONDS
        global_bucket = LOGIN_RATE_BUCKETS.setdefault(
            "__global__", {"failures": [], "blocked_until": 0, "updated": now}
        )
        global_bucket["failures"] = [
            stamp for stamp in global_bucket["failures"] if now - stamp <= LOGIN_RATE_WINDOW_SECONDS
        ]
        global_bucket["failures"].append(now)
        global_bucket["updated"] = now
        if len(global_bucket["failures"]) >= LOGIN_RATE_GLOBAL_MAX_FAILURES:
            global_bucket["blocked_until"] = now + LOGIN_RATE_BLOCK_SECONDS
        _prune_login_rate_buckets(now)


def login_rate_clear(key):
    with LOGIN_RATE_LOCK:
        LOGIN_RATE_BUCKETS.pop(key, None)


def read_last_totp_counter():
    try:
        if not AUTH_STATE_PATH.exists():
            return -1
        if AUTH_STATE_PATH.is_symlink() or not AUTH_STATE_PATH.is_file():
            raise ValueError("Authentication replay state is unsafe.")
        return int(AUTH_STATE_PATH.read_text(encoding="ascii").strip())
    except OSError as exc:
        raise ValueError("Authentication replay state cannot be read safely.") from exc


def write_last_totp_counter(counter):
    AUTH_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=AUTH_STATE_PATH.name + ".", dir=str(AUTH_STATE_PATH.parent))
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(str(int(counter)) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, AUTH_STATE_PATH)
        os.chmod(AUTH_STATE_PATH, 0o600)
    finally:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass


def safe_internal_next(value):
    value = str(value or "").strip()
    candidate = value
    for _ in range(3):
        decoded = unquote(candidate)
        if decoded == candidate:
            break
        candidate = decoded
    if not candidate or "\\" in candidate or re.search(r"[\x00-\x1f\x7f]", candidate):
        return url_for("index")
    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        return url_for("index")
    if parsed.path.startswith("/") and not parsed.path.startswith("//"):
        return candidate
    return url_for("index")


def authenticated_session_is_current(now=None):
    if not AUTH_CONFIG or not session.get("admin_authenticated"):
        return False
    now = int(time.time() if now is None else now)
    issued_at = int(session.get("admin_issued_at", 0) or 0)
    last_activity = int(session.get("admin_last_activity", 0) or 0)
    if issued_at <= 0 or last_activity <= 0:
        return False
    if now - last_activity > AUTH_CONFIG.session_timeout_seconds:
        return False
    if now - issued_at > AUTH_CONFIG.session_absolute_timeout_seconds:
        return False
    session["admin_last_activity"] = now
    session.modified = True
    return True


def validate_project(name):
    name = (name or "").strip().lower()
    if not PROJECT_RE.match(name):
        raise ValueError("Invalid project name. Use lowercase letters, numbers, _ or -. Minimum length is 3 characters.")
    return name


def validate_port(value, label):
    try:
        value = int(str(value).strip())
    except Exception:
        raise ValueError(f"{label} must be a number.")
    if value < 1 or value > 65535:
        raise ValueError(f"{label} must be between 1 and 65535.")
    return value


def validate_cookie_samesite(value, label="Cookie SameSite"):
    value = str(value or DEFAULT_SESSION_COOKIE_SAMESITE or "Lax").strip()
    lookup = {item.lower(): item for item in COOKIE_SAMESITE_CHOICES}
    normalized = lookup.get(value.lower())
    if not normalized:
        allowed = ", ".join(COOKIE_SAMESITE_CHOICES)
        raise ValueError(f"{label} must be one of these values: {allowed}.")
    return normalized


def validate_cookie_secure(value, label="Cookie Secure"):
    value = str(value or DEFAULT_SESSION_COOKIE_SECURE or "Off").strip()
    aliases = {
        "1": "On", "true": "On", "yes": "On", "on": "On",
        "0": "Off", "false": "Off", "no": "Off", "off": "Off",
    }
    normalized = aliases.get(value.lower())
    if not normalized:
        allowed = ", ".join(COOKIE_SECURE_CHOICES)
        raise ValueError(f"{label} must be one of these values: {allowed}.")
    return normalized


def normalize_env_defaults(env):
    env = dict(env or {})
    env["GLPI_CONTAINER_PORT"] = str(env.get("GLPI_CONTAINER_PORT") or "8080")
    env["GLPI_SESSION_COOKIE_SAMESITE"] = validate_cookie_samesite(env.get("GLPI_SESSION_COOKIE_SAMESITE"))
    env["GLPI_SESSION_COOKIE_SECURE"] = validate_cookie_secure(env.get("GLPI_SESSION_COOKIE_SECURE"))
    if env["GLPI_SESSION_COOKIE_SAMESITE"] == "None":
        env["GLPI_SESSION_COOKIE_SECURE"] = "On"
    return env


def cookie_samesite_for_display(env):
    try:
        return validate_cookie_samesite((env or {}).get("GLPI_SESSION_COOKIE_SAMESITE"))
    except Exception:
        return (env or {}).get("GLPI_SESSION_COOKIE_SAMESITE") or "invalid"


def cookie_secure_for_display(env):
    try:
        return normalize_env_defaults(env or {}).get("GLPI_SESSION_COOKIE_SECURE", "Off")
    except Exception:
        return (env or {}).get("GLPI_SESSION_COOKIE_SECURE") or "invalid"


def validate_db_identifier(value, label="Database name"):
    value = (value or "").strip()
    if not re.match(r"^[A-Za-z0-9_]+$", value):
        raise ValueError(f"{label} may only contain letters, numbers and _.")
    return value


def sql_identifier(value):
    value = validate_db_identifier(value)
    return "`" + value.replace("`", "``") + "`"


def sql_escape(value):
    return str(value).replace("'", "''")


def project_dir(project):
    return BASE_PATH / project


def env_file(project):
    return project_dir(project) / ".env"


def compose_file(project):
    return project_dir(project) / "docker-compose.yml"


SENSITIVE_YAML_KEY_RE = re.compile(
    r"^(\s*(?:-\s*)?[A-Za-z0-9_.-]*"
    r"(?:password|secret|token|api[_-]?key|private[_-]?key)"
    r"[A-Za-z0-9_.-]*\s*:\s*)(.*)$",
    re.IGNORECASE,
)
SENSITIVE_YAML_ENV_ASSIGNMENT_RE = re.compile(
    r"^(\s*-\s*[A-Za-z0-9_.-]*"
    r"(?:password|secret|token|api[_-]?key|private[_-]?key)"
    r"[A-Za-z0-9_.-]*\s*=\s*)(.*)$",
    re.IGNORECASE,
)
EMBEDDED_URL_CREDENTIAL_RE = re.compile(
    r"(?i)([a-z][a-z0-9+.-]*://)[^/@\s:]+:[^/@\s]+@"
)


def sanitized_compose_yaml(value):
    """Return Compose text suitable for authenticated display or download."""
    sanitized_lines = []
    redacted_block_indent = None
    for line in str(value).splitlines():
        indentation = len(line) - len(line.lstrip())
        if redacted_block_indent is not None:
            if not line.strip() or indentation > redacted_block_indent:
                continue
            redacted_block_indent = None
        match = (
            SENSITIVE_YAML_KEY_RE.match(line)
            or SENSITIVE_YAML_ENV_ASSIGNMENT_RE.match(line)
        )
        if match:
            candidate = match.group(2).strip().strip("\"'")
            if not (candidate.startswith("${") and candidate.endswith("}")):
                line = match.group(1) + '"[REDACTED]"'
                if re.fullmatch(r"[|>][+-]?[0-9]*", candidate):
                    redacted_block_indent = indentation
        line = EMBEDDED_URL_CREDENTIAL_RE.sub(r"\1[REDACTED]@", line)
        sanitized_lines.append(line)
    return "\n".join(sanitized_lines) + ("\n" if str(value).endswith("\n") else "")


def log_dir(project):
    return project_dir(project) / "_builder_logs"


def php_override_dir(project):
    return project_dir(project) / "php"


def php_override_file(project):
    return php_override_dir(project) / "99-glpi-builder-session.ini"


def write_php_session_override(project, env):
    env = normalize_env_defaults(env)
    samesite = env["GLPI_SESSION_COOKIE_SAMESITE"]
    secure = env["GLPI_SESSION_COOKIE_SECURE"]
    lines = [
        "; Generated by GLPI Builder",
        "; Persistent PHP session cookie override, important for SSO redirects.",
        f'session.cookie_samesite = "{samesite}"',
        "session.cookie_httponly = On",
        f"session.cookie_secure = {secure}",
    ]
    php_override_dir(project).mkdir(parents=True, exist_ok=True)
    path = php_override_file(project)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def php_session_override_text(env):
    env = normalize_env_defaults(env)
    return "\n".join([
        "; Runtime PHP session cookie override generated inside the GLPI container",
        f'session.cookie_samesite = "{env["GLPI_SESSION_COOKIE_SAMESITE"]}"',
        "session.cookie_httponly = On",
        f"session.cookie_secure = {env['GLPI_SESSION_COOKIE_SECURE']}",
    ]) + "\n"


def path_under_base(path):
    try:
        Path(path).resolve().relative_to(BASE_PATH.resolve())
        return True
    except Exception:
        return False


def path_under_backup_root(path):
    """Only expose backup files below the configured backup root."""
    try:
        Path(path).resolve().relative_to(BACKUP_ROOT.resolve())
        return True
    except Exception:
        return False


def validate_local_image(image, kind):
    image = str(image or "").strip()
    prefixes = ALLOWED_GLPI_IMAGES if kind == "glpi" else ALLOWED_DB_IMAGES
    if not image or not any(image.startswith(prefix) for prefix in prefixes):
        raise ValueError(f"Disallowed {kind} image: {image or 'empty'}.")
    client = docker_client()
    try:
        tags = {tag for item in client.images.list() for tag in (item.tags or [])}
    finally:
        close_docker_client(client)
    if image not in tags:
        raise ValueError(f"Docker image is not available locally: {image}")
    return image


def local_image_tags(kind, available_tags=None):
    prefixes = ALLOWED_GLPI_IMAGES if kind == "glpi" else ALLOWED_DB_IMAGES
    if available_tags is None:
        available_tags = dashboard_docker_snapshot()["image_tags"]
    tags = set(available_tags)
    return sorted(tag for tag in tags if any(tag.startswith(prefix) for prefix in prefixes))


def local_glpi_database_image_tags(available_tags=None):
    """Return database images supported by the current GLPI restore adapter."""
    if available_tags is None:
        available_tags = dashboard_docker_snapshot()["image_tags"]
    return sorted({tag for tag in available_tags if tag.startswith("mariadb:")})


def local_profile_image_tags(profile, available_tags=None):
    if available_tags is None:
        available_tags = dashboard_docker_snapshot()["image_tags"]
    return sorted({
        tag for tag in available_tags
        if any(tag.startswith(prefix) for prefix in profile.image_prefixes)
    })


def local_profile_database_image_tags(profile, available_tags=None):
    if available_tags is None:
        available_tags = dashboard_docker_snapshot()["image_tags"]
    return sorted({
        tag for tag in available_tags
        if any(tag.startswith(prefix) for prefix in profile.database_image_prefixes)
    })


def read_env(project):
    path = env_file(project)
    data = {}
    if not path.exists():
        return data
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            data[key] = value
    return data


def write_env(project, env):
    env = normalize_env_defaults(env)
    project_dir(project).mkdir(parents=True, exist_ok=True)
    lines = [
        f"# GLPI Builder Full Restore {APP_VERSION}",
        "# Passwords intentionally contain only letters and digits: no $, !, &, quotes, or spaces.",
    ]
    keys = list(ENV_ORDER) + [key for key in env.keys() if key not in ENV_ORDER]
    for key in keys:
        if key in env:
            lines.append(f"{key}={env[key]}")
    env_file(project).write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_simple_env_file(path):
    data = {}
    path = Path(path)
    if not path.exists() or not path.is_file():
        return data
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            data[key] = value
    return data


def is_managed_glpi_project(name, base_path=None):
    name = str(name or "").strip().lower()
    if not PROJECT_RE.fullmatch(name):
        return False
    root = Path(base_path) if base_path is not None else BASE_PATH
    folder = root / name
    environment_path = folder / ".env"
    compose_path = folder / "docker-compose.yml"
    try:
        if folder.is_symlink() or not folder.is_dir():
            return False
        if environment_path.is_symlink() or not environment_path.is_file():
            return False
        if compose_path.is_symlink() or not compose_path.is_file():
            return False
        env = read_simple_env_file(environment_path)
        required = (
            "PROJECT_NAME",
            "GLPI_IMAGE",
            "MARIADB_IMAGE",
            "GLPI_HTTP_PORT",
            "GLPI_CONTAINER_PORT",
            "MARIADB_ROOT_PASSWORD",
            "GLPI_DB_NAME",
            "GLPI_DB_USER",
            "GLPI_DB_PASSWORD",
        )
        if any(not env.get(key) for key in required):
            return False
        if env["PROJECT_NAME"] != name:
            return False
        if not any(env["GLPI_IMAGE"].startswith(prefix) for prefix in ALLOWED_GLPI_IMAGES):
            return False
        if not any(env["MARIADB_IMAGE"].startswith(prefix) for prefix in ALLOWED_DB_IMAGES):
            return False
        validate_port(env["GLPI_HTTP_PORT"], "GLPI_HTTP_PORT")
        if validate_port(env["GLPI_CONTAINER_PORT"], "GLPI_CONTAINER_PORT") != 8080:
            return False
        if any(not (folder / child).is_dir() for child in ("db", "glpi", "plugins")):
            return False
        compose_text = compose_path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return False

    required_compose_fragments = (
        f"container_name: {name}\n",
        f"container_name: {name}-db\n",
        f"/volume1/docker/{name}/db:/var/lib/mysql:rw",
        f"/volume1/docker/{name}/glpi:/var/glpi:rw",
        f"/volume1/docker/{name}/plugins:/var/www/glpi/plugins:rw",
    )
    return all(fragment in compose_text for fragment in required_compose_fragments)


def managed_project_issues(name, base_path=None):
    """Explain why a GLPI-looking project is not managed by this Builder.

    This is intentionally read-only.  Existing production Compose files and
    environment files must never be rewritten merely because they were found.
    """
    name = str(name or "").strip().lower()
    root = Path(base_path) if base_path is not None else BASE_PATH
    folder = root / name
    issues = []
    if not PROJECT_RE.fullmatch(name):
        return ["The Compose project name is not a valid Builder project name."]
    if folder.is_symlink() or not folder.is_dir():
        issues.append(f"Project directory {folder} was not found.")
        return issues

    environment_path = folder / ".env"
    compose_path = folder / "docker-compose.yml"
    if environment_path.is_symlink() or not environment_path.is_file():
        issues.append("Builder .env file is missing.")
        env = {}
    else:
        env = read_simple_env_file(environment_path)
    if compose_path.is_symlink() or not compose_path.is_file():
        issues.append("docker-compose.yml is missing from the project directory.")

    required = (
        "PROJECT_NAME", "GLPI_IMAGE", "MARIADB_IMAGE", "GLPI_HTTP_PORT",
        "GLPI_CONTAINER_PORT", "MARIADB_ROOT_PASSWORD", "GLPI_DB_NAME",
        "GLPI_DB_USER", "GLPI_DB_PASSWORD",
    )
    missing = [key for key in required if not env.get(key)]
    if missing:
        issues.append("Required Builder variables are missing: " + ", ".join(missing) + ".")
    if env.get("PROJECT_NAME") and env["PROJECT_NAME"] != name:
        issues.append(f"PROJECT_NAME is {env['PROJECT_NAME']!r}, expected {name!r}.")
    if env.get("GLPI_IMAGE") and not any(env["GLPI_IMAGE"].startswith(prefix) for prefix in ALLOWED_GLPI_IMAGES):
        issues.append("GLPI_IMAGE does not use an allowed image prefix.")
    if env.get("MARIADB_IMAGE") and not any(env["MARIADB_IMAGE"].startswith(prefix) for prefix in ALLOWED_DB_IMAGES):
        issues.append("MARIADB_IMAGE does not use an allowed image prefix.")
    for key in ("GLPI_HTTP_PORT", "GLPI_CONTAINER_PORT"):
        if env.get(key):
            try:
                port = validate_port(env[key], key)
                if key == "GLPI_CONTAINER_PORT" and port != 8080:
                    issues.append(f"GLPI_CONTAINER_PORT is {port}, expected 8080.")
            except ValueError as exc:
                issues.append(str(exc))
    for child in ("db", "glpi", "plugins"):
        if not (folder / child).is_dir():
            issues.append(f"Required persistent directory {child}/ is missing.")

    if compose_path.is_file() and not compose_path.is_symlink():
        try:
            compose_text = compose_path.read_text(encoding="utf-8", errors="replace")
            fragments = (
                (f"container_name: {name}\n", f"Container name {name} is not declared in the expected form."),
                (f"container_name: {name}-db\n", f"Container name {name}-db is not declared in the expected form."),
                (f"/volume1/docker/{name}/db:/var/lib/mysql:rw", "Database volume does not match the Builder layout."),
                (f"/volume1/docker/{name}/glpi:/var/glpi:rw", "GLPI data volume does not match the Builder layout."),
                (f"/volume1/docker/{name}/plugins:/var/www/glpi/plugins:rw", "Plugin volume does not match the Builder layout."),
            )
            issues.extend(message for fragment, message in fragments if fragment not in compose_text)
        except OSError as exc:
            issues.append(f"docker-compose.yml could not be read: {exc}.")
    return issues


def container_image_name(container):
    attrs = getattr(container, "attrs", {}) or {}
    configured = str(attrs.get("Config", {}).get("Image") or "").strip()
    if configured:
        return configured
    tags = getattr(getattr(container, "image", None), "tags", None) or []
    return str(tags[0]) if tags else ""


def container_image_references(container):
    """Return every usable image reference exposed by Docker.

    A container can keep a sha256 Config.Image after its original tag becomes
    dangling.  Docker SDK may still expose repo tags/digests elsewhere, so do
    not let the first (unhelpful) value hide the remaining evidence.
    """
    attrs = getattr(container, "attrs", {}) or {}
    try:
        image = getattr(container, "image", None)
    except docker.errors.DockerException:
        # Stopped rollback containers can outlive their image metadata after a
        # rebuild. Their cached Config.Image remains useful, and dashboard
        # discovery must not fail merely because Docker can no longer inspect
        # the deleted image object.
        image = None
    image_attrs = getattr(image, "attrs", {}) or {}
    candidates = [
        attrs.get("Config", {}).get("Image"),
        *(getattr(image, "tags", None) or []),
        *(image_attrs.get("RepoTags") or []),
        *(image_attrs.get("RepoDigests") or []),
    ]
    references = []
    for candidate in candidates:
        value = str(candidate or "").strip()
        if value and value not in references:
            references.append(value)
    return references


def container_environment_keys(container):
    attrs = getattr(container, "attrs", {}) or {}
    entries = attrs.get("Config", {}).get("Env") or []
    return {str(entry).partition("=")[0] for entry in entries if entry}


def container_mount_destinations(container):
    attrs = getattr(container, "attrs", {}) or {}
    return {
        str(mount.get("Destination") or "").rstrip("/")
        for mount in (attrs.get("Mounts") or [])
        if isinstance(mount, dict)
    }


def container_looks_like_glpi(container):
    references = container_image_references(container)
    if any(reference.startswith(prefix) for reference in references for prefix in ALLOWED_GLPI_IMAGES):
        return True

    # Read-only discovery may use structural evidence when an old image has
    # lost its tag (shown by Synology as glpi/glpi:<none>).  Requiring the GLPI
    # name plus an application-specific env key or mount avoids classifying an
    # arbitrary database/container as GLPI.
    name = str(getattr(container, "name", "") or "").lower()
    environment_keys = container_environment_keys(container)
    destinations = container_mount_destinations(container)
    return "glpi" in name and (
        bool(environment_keys & {"GLPI_DB_HOST", "GLPI_DB_NAME", "GLPI_DB_USER"})
        or bool(destinations & {"/var/glpi", "/var/www/glpi", "/var/www/glpi/plugins"})
    )


def inferred_compose_project(container, is_database=False):
    project = compose_project_for_container(container)
    if project:
        return project
    name = str(getattr(container, "name", "") or "").strip().lower()
    if is_database:
        if name.endswith("-db"):
            name = name[:-3]
        elif "-db-" in name:
            name = name.replace("-db-", "-", 1)
    return name if PROJECT_RE.fullmatch(name) else ""


def compose_project_for_container(container):
    attrs = getattr(container, "attrs", {}) or {}
    labels = attrs.get("Config", {}).get("Labels") or {}
    value = str(labels.get("com.docker.compose.project") or "").strip().lower()
    return value if PROJECT_RE.fullmatch(value) else ""


def current_backup_source_project():
    projects = scheduled_backup_projects()
    return projects[0] if projects else ""


def backup_schedule_path(project):
    return BACKUP_PROJECTS_DIR / f"{validate_project(project)}.env"


def backup_state_path(project):
    return BACKUP_STATE_DIR / f"{validate_project(project)}.env"


def dispatcher_heartbeat_paths():
    return (
        BACKUP_STATE_DIR / "dispatcher.env",
        BACKUP_SCHEDULER_DIR / ".dispatcher.env",
    )


def dispatcher_heartbeat():
    newest = {}
    newest_epoch = 0
    for path in dispatcher_heartbeat_paths():
        heartbeat = read_simple_env_file(path)
        epoch = safe_int(heartbeat.get("LAST_HEARTBEAT"), 0, 0)
        if epoch > newest_epoch:
            newest = heartbeat
            newest_epoch = epoch
    return newest, newest_epoch


def dispatcher_is_healthy(max_age_seconds=1200):
    _heartbeat, heartbeat_epoch = dispatcher_heartbeat()
    return bool(heartbeat_epoch and time.time() - heartbeat_epoch < max_age_seconds)


def safe_int(value, default=0, minimum=None, maximum=None):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    if minimum is not None and parsed < minimum:
        return default
    if maximum is not None and parsed > maximum:
        return default
    return parsed


def migrate_legacy_backup_config():
    legacy = read_simple_env_file(BACKUP_ENV_PATH)
    project = legacy.get("PROJECT_NAME", "")
    if not PROJECT_RE.fullmatch(project):
        return ""
    target = backup_schedule_path(project)
    if not target.is_file():
        values = dict(legacy)
        values.update({
            "SCHEDULE_ENABLED": "yes",
            "SCHEDULE_KIND": "daily",
            "SCHEDULE_TIME": "02:00",
            "SCHEDULE_WEEKDAYS": "7",
            "INTERVAL_HOURS": "24",
            "RETENTION_DAYS": values.get("RETENTION_DAYS", "60"),
        })
        text = "# Migrated and maintained by Docker App Manager.\n" + "".join(
            f"{key}={value}\n" for key, value in values.items()
            if re.fullmatch(r"[A-Z0-9_]+", key)
            and re.fullmatch(r"[A-Za-z0-9_./,:-]+", str(value))
        )
        atomic_write_text(target, text, 0o600)
    return project


def scheduled_backup_projects():
    migrate_legacy_backup_config()
    names = []
    if BACKUP_PROJECTS_DIR.is_dir():
        for path in sorted(BACKUP_PROJECTS_DIR.glob("*.env")):
            data = read_simple_env_file(path)
            name = data.get("PROJECT_NAME", "")
            if PROJECT_RE.fullmatch(name) and data.get("SCHEDULE_ENABLED", "yes") == "yes":
                names.append(name)
    if not names:
        legacy = read_simple_env_file(BACKUP_ENV_PATH).get("PROJECT_NAME", "")
        if PROJECT_RE.fullmatch(legacy):
            names.append(legacy)
    return names


def validate_backup_schedule(kind="daily", schedule_time="02:00", weekdays="7", interval_hours="24", retention_days="60"):
    kind = str(kind or "daily").strip().lower()
    if kind not in {"daily", "weekly", "interval"}:
        raise ValueError("Backup frequency must be daily, weekly or interval.")
    schedule_time = str(schedule_time or "02:00").strip()
    if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", schedule_time):
        raise ValueError("Backup start time must use HH:MM in 24-hour format.")
    weekday_values = sorted(set(filter(None, re.split(r"[, ]+", str(weekdays or "7")))))
    if any(value not in {"1", "2", "3", "4", "5", "6", "7"} for value in weekday_values):
        raise ValueError("Backup weekdays must be numbers 1 through 7.")
    interval = int(interval_hours)
    retention = int(retention_days)
    if interval not in {6, 12, 24, 48, 72, 168, 336, 720}:
        raise ValueError("Backup interval must use one of the supported day, week or month presets.")
    if retention < 1 or retention > 3650:
        raise ValueError("Backup retention must be between 1 and 3650 days.")
    return {
        "kind": kind,
        "time": schedule_time,
        "weekdays": ",".join(weekday_values) or "7",
        "interval_hours": str(interval),
        "retention_days": str(retention),
    }


def backup_schedule_status(project):
    project = validate_project(project)
    config = read_simple_env_file(backup_schedule_path(project))
    if not config and read_simple_env_file(BACKUP_ENV_PATH).get("PROJECT_NAME") == project:
        config = read_simple_env_file(BACKUP_ENV_PATH)
        config.update({"SCHEDULE_ENABLED": "yes", "SCHEDULE_KIND": "daily", "SCHEDULE_TIME": "02:00", "SCHEDULE_WEEKDAYS": "7", "INTERVAL_HOURS": "24"})
    state = read_simple_env_file(backup_state_path(project))
    enabled = config.get("SCHEDULE_ENABLED") == "yes"
    try:
        schedule = validate_backup_schedule(
            config.get("SCHEDULE_KIND", "daily"),
            config.get("SCHEDULE_TIME", "02:00"),
            config.get("SCHEDULE_WEEKDAYS", "7"),
            config.get("INTERVAL_HOURS", "24"),
            config.get("RETENTION_DAYS", "60"),
        )
    except (TypeError, ValueError):
        schedule = validate_backup_schedule()
    kind = schedule["kind"]
    schedule_time = schedule["time"]
    weekdays = schedule["weekdays"]
    interval_hours = safe_int(schedule["interval_hours"], 24, 1)
    now = datetime.now()
    next_run = None
    if enabled:
        hour, minute = (int(part) for part in schedule_time.split(":"))
        if kind == "daily":
            candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            next_run = candidate if candidate > now else candidate + timedelta(days=1)
        elif kind == "weekly":
            allowed = {int(value) for value in weekdays.split(",") if value.isdigit()}
            for offset in range(8):
                candidate = (now + timedelta(days=offset)).replace(hour=hour, minute=minute, second=0, microsecond=0)
                if candidate > now and candidate.isoweekday() in allowed:
                    next_run = candidate
                    break
        else:
            last = safe_int(state.get("LAST_ATTEMPT"), 0, 0)
            next_run = datetime.fromtimestamp(last) + timedelta(hours=interval_hours) if last else now
    return {
        "enabled": enabled,
        "kind": kind,
        "time": schedule_time,
        "weekdays": weekdays,
        "interval_hours": interval_hours,
        "retention_days": safe_int(schedule["retention_days"], 60, 1, 3650),
        "next_run": next_run.strftime("%Y-%m-%d %H:%M") if next_run else "",
        "last_status": state.get("LAST_STATUS", "Not run"),
        "last_attempt": datetime.fromtimestamp(int(state["LAST_ATTEMPT"])).strftime("%Y-%m-%d %H:%M") if state.get("LAST_ATTEMPT", "").isdigit() else "",
        "last_success": datetime.fromtimestamp(int(state["LAST_SUCCESS"])).strftime("%Y-%m-%d %H:%M") if state.get("LAST_SUCCESS", "").isdigit() else "",
        "dispatcher_healthy": dispatcher_is_healthy(),
    }


def database_container_for_project(project):
    project = validate_project(project)
    expected = f"{project}-db"
    if get_container(expected):
        return expected

    env = read_env(project)
    for key in ("DB_CONTAINER", "DB_CONTAINER_NAME", "MARIADB_CONTAINER"):
        candidate = str(env.get(key) or "").strip()
        if CONTAINER_RE.fullmatch(candidate) and get_container(candidate):
            return candidate

    db_path = (project_dir(project) / "db").resolve()
    glpi_path = (project_dir(project) / "glpi").resolve()
    containers = []
    try:
        containers = docker_client().containers.list(all=True)
        for container in containers:
            for mount in container.attrs.get("Mounts", []):
                source = mount.get("Source")
                destination = mount.get("Destination")
                if source and destination == "/var/lib/mysql" and Path(source).resolve() == db_path:
                    if CONTAINER_RE.fullmatch(container.name):
                        return container.name
    except Exception:
        pass

    for container in containers:
        try:
            mounts_glpi = any(
                mount.get("Source")
                and mount.get("Destination") == "/var/glpi"
                and Path(mount["Source"]).resolve() == glpi_path
                for mount in container.attrs.get("Mounts", [])
            )
            if not mounts_glpi:
                continue
            container_env = {}
            for item in container.attrs.get("Config", {}).get("Env", []) or []:
                if "=" in item:
                    key, value = item.split("=", 1)
                    container_env[key] = value
            candidate = container_env.get("GLPI_DB_HOST", "")
            if CONTAINER_RE.fullmatch(candidate) and get_container(candidate):
                return candidate
        except Exception:
            continue

    if compose_file(project).is_file():
        compose_text = compose_file(project).read_text(encoding="utf-8", errors="replace")
        candidates = re.findall(r"(?m)^\s+container_name:\s*([A-Za-z0-9_.-]+)\s*$", compose_text)
        for candidate in candidates:
            if CONTAINER_RE.fullmatch(candidate) and (candidate.endswith("-db") or "mdb" in candidate.lower()):
                return candidate
    return expected


def atomic_write_text(path, text, mode):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.chmod(mode)
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def ensure_managed_directory(path, mode):
    """Create one Builder-owned runtime directory with predictable permissions."""
    path = Path(path)
    if path.is_symlink():
        raise ValueError(f"Managed directory must not be a symlink: {path}")
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, mode)
    return path


def _copy_legacy_backup_file(source, destination, mode, *, rewrite_config=False):
    """Copy one legacy control file without overwriting its new equivalent."""
    source = Path(source)
    destination = Path(destination)
    if destination.exists() or not source.is_file() or source.is_symlink():
        return False
    if rewrite_config:
        text = source.read_text(encoding="utf-8", errors="strict")
        text = re.sub(
            r"(?m)^BACKUP_ROOT=.*$",
            f"BACKUP_ROOT={BACKUP_DATA_ROOT}",
            text,
        )
        text = re.sub(
            r"(?m)^MYSQL_CNF=.*$",
            f"MYSQL_CNF={BACKUP_CNF_PATH}",
            text,
        )
        atomic_write_text(destination, text, mode)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", dir=str(destination.parent)
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(source.read_bytes())
                handle.flush()
                os.fsync(handle.fileno())
            temporary_path.chmod(mode)
            os.replace(temporary_path, destination)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()
    return True


def migrate_legacy_backup_runtime():
    """Copy legacy scheduler configuration/state to the new layout safely."""
    if not LEGACY_BACKUP_TASK_DIR.is_dir() or LEGACY_BACKUP_TASK_DIR.is_symlink():
        return []
    migrated = []
    _copy_legacy_backup_file(
        LEGACY_BACKUP_TASK_DIR / "GLPI_mysql_backup.cnf",
        BACKUP_CNF_PATH,
        0o600,
    ) and migrated.append(str(BACKUP_CNF_PATH))
    _copy_legacy_backup_file(
        LEGACY_BACKUP_TASK_DIR / "GLPI_backup.env",
        BACKUP_ENV_PATH,
        0o600,
        rewrite_config=True,
    ) and migrated.append(str(BACKUP_ENV_PATH))
    for source_dir, destination_dir in (
        (LEGACY_BACKUP_TASK_DIR / "projects", BACKUP_PROJECTS_DIR),
        (LEGACY_BACKUP_TASK_DIR / "state", BACKUP_STATE_DIR),
    ):
        if not source_dir.is_dir() or source_dir.is_symlink():
            continue
        for source in source_dir.iterdir():
            if not source.is_file() or source.is_symlink():
                continue
            rewrite = source_dir.name == "projects" and source.suffix == ".env"
            mode = 0o600
            destination = destination_dir / source.name
            if _copy_legacy_backup_file(
                source,
                destination,
                mode,
                rewrite_config=rewrite,
            ):
                migrated.append(str(destination))
    return migrated


def install_backup_runtime():
    """Create the backup layout and install its managed scripts atomically."""
    try:
        BACKUP_DATA_ROOT.resolve().relative_to(BACKUP_ROOT.resolve())
        BACKUP_TASK_DIR.resolve().relative_to(BACKUP_DATA_ROOT.resolve())
        BACKUP_SCHEDULER_DIR.resolve().relative_to(BACKUP_ROOT.resolve())
    except Exception:
        raise ValueError(
            f"Backup runtime folders must be located below {BACKUP_ROOT}."
        )
    for source in (BACKUP_SCRIPT_SOURCE, BACKUP_DISPATCHER_SOURCE):
        if not source.is_file():
            raise ValueError(f"Bundled backup script is missing: {source}")
    ensure_managed_directory(BACKUP_TASK_DIR, 0o700)
    ensure_managed_directory(BACKUP_PROJECTS_DIR, 0o700)
    ensure_managed_directory(BACKUP_CREDENTIALS_DIR, 0o700)
    ensure_managed_directory(BACKUP_STATE_DIR, 0o700)
    ensure_managed_directory(BACKUP_LOCKS_DIR, 0o700)
    ensure_managed_directory(BACKUP_SCHEDULER_DIR, 0o750)
    migrate_legacy_backup_runtime()
    migrate_project_backup_credentials()
    if BACKUP_SCRIPT_PATH.exists():
        existing_header = BACKUP_SCRIPT_PATH.read_text(
            encoding="utf-8", errors="replace"
        )[:200]
        preserved = BACKUP_TASK_DIR / "GLPI_backup.pre-builder.sh"
        if (
            not re.search(r"Managed by GLPI (?:Project )?Builder", existing_header)
            and not preserved.exists()
        ):
            shutil.copy2(BACKUP_SCRIPT_PATH, preserved)
            preserved.chmod(0o700)
    atomic_write_text(
        BACKUP_SCRIPT_PATH,
        BACKUP_SCRIPT_SOURCE.read_text(encoding="utf-8"),
        0o750,
    )
    atomic_write_text(
        BACKUP_DISPATCHER_PATH,
        BACKUP_DISPATCHER_SOURCE.read_text(encoding="utf-8"),
        0o750,
    )


def install_backup_dispatcher():
    """Compatibility wrapper for callers that install the backup runtime."""
    install_backup_runtime()


def project_backup_credential_path(project):
    """Return the managed credential path for exactly one validated project."""
    project = validate_project(project)
    return BACKUP_CREDENTIALS_DIR / f"{project}.cnf"


def ensure_project_backup_credential(project, env=None):
    """Atomically create/update a private MariaDB option file for one project."""
    project = validate_project(project)
    folder = project_dir(project)
    environment_path = folder / ".env"
    try:
        folder.resolve().relative_to(BASE_PATH.resolve())
        BACKUP_CREDENTIALS_DIR.resolve().relative_to(BACKUP_TASK_DIR.resolve())
    except Exception:
        raise ValueError("Managed project or credential path escaped its configured root.")
    if folder.is_symlink() or environment_path.is_symlink():
        raise ValueError(f"Project environment must not use symlinks: {environment_path}")
    env = dict(env or read_env(project))
    password = str(env.get("MARIADB_ROOT_PASSWORD") or "")
    if not password:
        raise ValueError(f"MariaDB root password is missing from {project}/.env.")
    if any(character in password for character in ("\r", "\n", "\x00")):
        raise ValueError("MariaDB root password contains unsupported control characters.")
    ensure_managed_directory(BACKUP_CREDENTIALS_DIR, 0o700)
    destination = project_backup_credential_path(project)
    if destination.is_symlink():
        raise ValueError(f"Managed backup credential must not be a symlink: {destination}")
    escaped = password.replace("\\", "\\\\").replace('"', '\\"')
    atomic_write_text(
        destination,
        f'[client]\nuser=root\npassword="{escaped}"\n',
        0o600,
    )
    return destination


def migrate_project_backup_credentials():
    """Safely rewrite existing schedules to their own derived credential file."""
    if not BACKUP_PROJECTS_DIR.is_dir() or BACKUP_PROJECTS_DIR.is_symlink():
        return []
    migrated = []
    for schedule_path in BACKUP_PROJECTS_DIR.glob("*.env"):
        if schedule_path.is_symlink() or not schedule_path.is_file():
            continue
        try:
            project = validate_project(schedule_path.stem)
            configured = read_simple_env_file(schedule_path)
            if configured.get("PROJECT_NAME") != project:
                continue
            credential = ensure_project_backup_credential(project)
            if configured.get("MYSQL_CNF") == str(credential):
                continue
            configured["MYSQL_CNF"] = str(credential)
            atomic_write_text(
                schedule_path,
                "# Generated and maintained by Docker App Manager.\n"
                + "".join(f"{key}={value}\n" for key, value in configured.items()),
                0o600,
            )
            migrated.append(str(schedule_path))
        except (OSError, ValueError):
            # Invalid projects remain untouched and visibly fail readiness checks.
            continue
    return migrated


def configure_scheduled_backup(project, env=None, *, enabled=True, kind="daily", schedule_time="02:00", weekdays="7", interval_hours="24", retention_days="60"):
    project = validate_project(project)
    install_backup_runtime()
    migrate_legacy_backup_config()
    env = dict(env or read_env(project))
    if not env:
        raise ValueError(f"No .env file was found for {project}.")
    manifest = read_application_manifest(project_dir(project))
    app_type = str((manifest or {}).get("type") or "glpi")
    if app_type == "n8n":
        db_name = validate_db_identifier(env.get("POSTGRES_DB") or "n8n")
        data_paths = "data"
    elif app_type == "teampasswordmanager":
        db_name = validate_db_identifier(env.get("MYSQL_DATABASE") or "teampasswordmanager")
        data_paths = "application"
    else:
        app_type = "glpi"
        db_name = validate_db_identifier(env.get("GLPI_DB_NAME") or "glpi")
        data_paths = "glpi,plugins"
    db_container = database_container_for_project(project)
    if not CONTAINER_RE.fullmatch(db_container):
        raise ValueError(f"Invalid database container name: {db_container}")

    try:
        BACKUP_TASK_DIR.resolve().relative_to(BACKUP_DATA_ROOT.resolve())
        BACKUP_SCHEDULER_DIR.resolve().relative_to(BACKUP_ROOT.resolve())
    except Exception:
        raise ValueError(f"Backup task folders must be located below {BACKUP_ROOT}.")
    if not BACKUP_SCRIPT_SOURCE.is_file():
        raise ValueError(f"Bundled backup script is missing: {BACKUP_SCRIPT_SOURCE}")

    schedule = validate_backup_schedule(kind, schedule_time, weekdays, interval_hours, retention_days)
    previous = read_simple_env_file(backup_schedule_path(project))
    if not previous:
        previous = read_simple_env_file(BACKUP_ENV_PATH)
    mysql_cnf = ""
    if app_type == "glpi":
        mysql_cnf = str(ensure_project_backup_credential(project, env))
    container_cnf = previous.get("CONTAINER_CNF") or "/tmp/GLPI_mysql_backup.cnf"
    values = {
        "PROJECT_NAME": project,
        "APP_TYPE": app_type,
        "PROJECT_DIR": str(project_dir(project)),
        "DB_CONTAINER": db_container,
        "DB_NAME": db_name,
        "BACKUP_ROOT": str(BACKUP_DATA_ROOT),
        "MYSQL_CNF": mysql_cnf,
        "CONTAINER_CNF": container_cnf,
        "DATA_PATHS": data_paths,
        "RETENTION_DAYS": schedule["retention_days"],
        "SCHEDULE_ENABLED": "yes" if enabled else "no",
        "SCHEDULE_KIND": schedule["kind"],
        "SCHEDULE_TIME": schedule["time"],
        "SCHEDULE_WEEKDAYS": schedule["weekdays"],
        "INTERVAL_HOURS": schedule["interval_hours"],
        "APP_IMAGE": str(env.get("APP_IMAGE") or env.get("GLPI_IMAGE") or "unknown"),
        "DB_IMAGE": str(env.get("DATABASE_IMAGE") or env.get("MARIADB_IMAGE") or "unknown"),
        "N8N_ENCRYPTION_KEY": str(env.get("N8N_ENCRYPTION_KEY") or "") if app_type == "n8n" else "",
    }
    if any(
        value and not re.fullmatch(r"[A-Za-z0-9_./,:-]+", value)
        for value in values.values()
    ):
        raise ValueError("Backup configuration contains unsupported characters.")
    config_text = "# Generated and maintained by Docker App Manager.\n" + "".join(
        f"{key}={value}\n" for key, value in values.items()
    )
    ensure_managed_directory(BACKUP_PROJECTS_DIR, 0o700)
    ensure_managed_directory(BACKUP_STATE_DIR, 0o700)
    atomic_write_text(backup_schedule_path(project), config_text, 0o600)
    messages = [
        f"Backup configuration: {project}",
        (
            f"Backup schedule: {schedule['kind']} at {schedule['time']}"
            if enabled else "Scheduled backups disabled; Run backup now remains available."
        ),
        f"Backup environment: {backup_schedule_path(project)}",
        f"Task Scheduler command: /bin/bash {BACKUP_DISPATCHER_PATH}",
    ]
    return messages


def format_size(size_bytes):
    size = max(0, int(size_bytes or 0))
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024


def latest_successful_backup(project):
    project = validate_project(project)
    if not BACKUP_ROOT.is_dir():
        return None
    candidates = []
    search_roots = [
        BACKUP_DATA_ROOT / project,
        BACKUP_ROOT / project,
        BACKUP_ROOT,
    ]
    try:
        folders = []
        for root in search_roots:
            if root.is_dir():
                folders.extend(root.iterdir())
        for index, folder in enumerate(folders):
            if index >= MAX_SCAN_ENTRIES:
                break
            if folder.is_symlink() or not folder.is_dir():
                continue
            manifest = {}
            manifest_path = folder / "manifest.json"
            if manifest_path.is_file():
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if manifest.get("project") != project:
                    continue
                database_dump = folder / str(manifest.get("database") or "")
                files_archive = folder / str(manifest.get("files") or "")
                created_at = str(manifest.get("created_at") or "")
            elif folder.name.startswith("GLPI_Backup_"):
                info = read_simple_env_file(folder / "BACKUP_INFO")
                if info.get("PROJECT_NAME") != project:
                    continue
                database_dump = folder / "glpi-database.sql"
                files_archive = folder / "glpi-files.tar.gz"
                created_at = info.get("CREATED_AT") or ""
            else:
                continue
            if not database_dump.is_file() or not files_archive.is_file():
                continue
            if database_dump.stat().st_size <= 0 or files_archive.stat().st_size <= 0:
                continue
            candidates.append(
                (folder.stat().st_mtime, folder, created_at, database_dump, files_archive)
            )
    except OSError:
        return None
    if not candidates:
        return None
    _, folder, created_at, database_dump, files_archive = max(
        candidates, key=lambda item: item[0]
    )
    size_bytes = database_dump.stat().st_size + files_archive.stat().st_size
    return {
        "name": folder.name,
        "path": str(folder),
        "created_at": created_at or time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(folder.stat().st_mtime)),
        "size_bytes": size_bytes,
        "size_label": format_size(size_bytes),
        "checksum_manifest": (folder / "SHA256SUMS").is_file(),
    }


def scheduled_backup_status(project):
    project = validate_project(project)
    schedule = backup_schedule_status(project)
    selected = schedule["enabled"]
    configured = read_simple_env_file(backup_schedule_path(project))
    if not configured and read_simple_env_file(BACKUP_ENV_PATH).get("PROJECT_NAME") == project:
        configured = read_simple_env_file(BACKUP_ENV_PATH)
    status = {
        "selected": selected,
        "ready": False,
        "issues": [],
        "latest": latest_successful_backup(project),
        "schedule": schedule,
    }
    if not configured:
        return status

    if configured.get("PROJECT_NAME") != project:
        status["issues"].append("Backup environment does not point to this project.")
    if not BACKUP_SCRIPT_PATH.is_file():
        status["issues"].append("Managed backup script is missing.")
    if not backup_schedule_path(project).is_file() and not BACKUP_ENV_PATH.is_file():
        status["issues"].append("Backup environment file is missing.")
    app_type = configured.get("APP_TYPE", "glpi")
    if app_type == "glpi":
        try:
            mysql_cnf = ensure_project_backup_credential(project)
        except ValueError as error:
            mysql_cnf = project_backup_credential_path(project)
            status["issues"].append(str(error))
        if not mysql_cnf.is_file():
            status["issues"].append(f"MariaDB credential file is missing: {mysql_cnf}")
        elif mysql_cnf.is_symlink():
            status["issues"].append(f"MariaDB credential file must not be a symlink: {mysql_cnf}")
        elif mysql_cnf.stat().st_mode & 0o077:
            status["issues"].append(f"MariaDB credential file permissions are not private: {mysql_cnf}")
        elif configured.get("MYSQL_CNF") != str(mysql_cnf):
            schedule_path = backup_schedule_path(project)
            if schedule_path.is_file() and not schedule_path.is_symlink():
                configured["MYSQL_CNF"] = str(mysql_cnf)
                atomic_write_text(
                    schedule_path,
                    "# Generated and maintained by Docker App Manager.\n"
                    + "".join(f"{key}={value}\n" for key, value in configured.items()),
                    0o600,
                )
    required_paths = {
        "glpi": ("glpi", "plugins"),
        "n8n": ("data",),
        "teampasswordmanager": ("application",),
    }.get(app_type, ())
    if not required_paths:
        status["issues"].append(f"Unsupported backup adapter: {app_type}")
    for relative in required_paths:
        if not (project_dir(project) / relative).is_dir():
            status["issues"].append(f"Required {app_type} data directory is missing: {relative}")

    db_name = configured.get("DB_CONTAINER") or database_container_for_project(project)
    database = get_container(db_name) if CONTAINER_RE.fullmatch(db_name or "") else None
    if not database:
        status["issues"].append(f"Database container is missing: {db_name or 'unknown'}")
    else:
        try:
            database.reload()
        except Exception:
            pass
        if getattr(database, "status", "unknown") != "running":
            status["issues"].append(f"Database container is not running: {db_name}")

    status["ready"] = not status["issues"]
    return status


def backup_age(created_at):
    """Return backup age metadata without trusting locale-specific parsing."""
    if not created_at:
        return {"days": None, "label": "Unknown age", "stale": True}
    value = str(created_at).strip()
    value = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", value)
    parsed = None
    try:
        # Backup manifests use ISO 8601, commonly with a T separator and a
        # numeric timezone such as +0200.  fromisoformat also keeps legacy
        # space-separated timestamps working.
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        pass
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        if parsed is not None:
            break
        try:
            parsed = datetime.strptime(value, pattern)
            break
        except ValueError:
            continue
    if parsed is None:
        return {"days": None, "label": "Unknown age", "stale": True}
    now = datetime.now(parsed.tzinfo) if parsed.tzinfo is not None else datetime.now()
    days = max(0, int((now - parsed).total_seconds() // 86400))
    return {
        "days": days,
        "label": "Today" if days == 0 else f"{days} day{'s' if days != 1 else ''} old",
        "stale": days > BACKUP_STALE_DAYS,
    }


def backup_inventory(db_backups, file_backups, *, demo=False):
    """Describe restore sources and conservatively pair matching database/files."""
    rows = []
    for kind, choices in (("Database", db_backups), ("GLPI files", file_backups)):
        for value, label in choices:
            path = Path(value)
            size_bytes = 0
            modified = ""
            if demo:
                size_bytes = 184 * 1024 * 1024 if kind == "Database" else 612 * 1024 * 1024
                modified = "2026-07-27 19:45"
            else:
                try:
                    stat_result = path.stat()
                    size_bytes = stat_result.st_size if path.is_file() else 0
                    modified = datetime.fromtimestamp(stat_result.st_mtime).strftime("%Y-%m-%d %H:%M")
                except OSError:
                    pass
            name = path.name.lower()
            lowered_path = str(path).lower()
            manifest_application = ""
            inventory_manifest = path.parent / "manifest.json"
            if inventory_manifest.is_file() and not inventory_manifest.is_symlink():
                try:
                    manifest_application = str(json.loads(
                        inventory_manifest.read_text(encoding="utf-8")
                    ).get("application") or "").lower()
                except (OSError, ValueError, json.JSONDecodeError):
                    manifest_application = ""
            if manifest_application == "n8n" or "n8n" in lowered_path:
                application = "n8n"
            elif manifest_application == "teampasswordmanager" or "tpm" in lowered_path or "teampassword" in lowered_path or (path.parent / TPM_BACKUP_MANIFEST).is_file():
                application = "Team Password Manager"
            elif manifest_application == "glpi":
                application = "GLPI"
            else:
                application = "GLPI / unclassified"
            pair_key = re.sub(
                r"(?:[-_.]?(?:database|db|sql|files?|glpi-files?))?(?:\.(?:sql|dump|tar|gz|tgz|zip|bz2|xz))+$",
                "",
                name,
            ).strip("-_.") or path.parent.name.lower()
            rows.append({
                "kind": kind,
                "application": application,
                "value": value,
                "label": label,
                "size_label": format_size(size_bytes) if size_bytes else "Directory / unknown",
                "modified": modified or "Unknown",
                "pair_key": pair_key,
            })
    counts = {}
    for row in rows:
        counts.setdefault(row["pair_key"], set()).add(row["kind"])
    for row in rows:
        row["complete_pair"] = len(counts.get(row["pair_key"], ())) == 2
    return rows


def newest_local_image(current, available):
    repository, separator, current_tag = str(current or "").rpartition(":")
    if not separator or not repository:
        return ""

    def version_key(tag):
        parts = re.findall(r"[0-9]+", tag)
        return tuple(int(part) for part in parts) if parts else ()

    current_key = version_key(current_tag)
    candidates = []
    for image in available:
        candidate_repo, candidate_separator, candidate_tag = str(image).rpartition(":")
        if candidate_separator and candidate_repo == repository:
            candidate_key = version_key(candidate_tag)
            if candidate_key and candidate_key > current_key:
                candidates.append((candidate_key, image))
    return max(candidates, default=((), ""))[1]


def last_action_timestamp(project, action_fragment):
    for filename in list_logs(project, limit=80):
        if action_fragment not in filename:
            continue
        try:
            stamp = datetime.strptime(filename[:15], "%Y%m%d-%H%M%S")
            return stamp.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            continue
    return ""


def enrich_project_operational_metadata(project, available_images):
    project = dict(project)
    backup_status = dict(project.get("backup_status") or {})
    if "schedule" not in backup_status:
        backup_status["schedule"] = (
            backup_schedule_status(project["name"])
            if not project.get("simulated")
            else {
                "enabled": False, "kind": "daily", "time": "02:00",
                "weekdays": "7", "interval_hours": 24,
                "retention_days": 60, "next_run": "",
                "last_status": "Not run", "last_attempt": "",
                "last_success": "", "dispatcher_healthy": False,
            }
        )
    project["backup_status"] = backup_status
    latest = project.get("backup_status", {}).get("latest")
    project["backup_age"] = backup_age(latest.get("created_at") if latest else "")
    project["newer_glpi_image"] = newest_local_image(
        project.get("glpi_image"), available_images
    )
    project["newer_db_image"] = newest_local_image(
        project.get("mariadb_image"), available_images
    )
    drift = []
    if project.get("tz") and project["tz"] != TZ_DEFAULT:
        drift.append(f"Time zone differs from the Builder default ({TZ_DEFAULT}).")
    if project.get("cookie_samesite") != DEFAULT_SESSION_COOKIE_SAMESITE:
        drift.append("Cookie SameSite differs from the current Builder default.")
    if project.get("cookie_secure") != DEFAULT_SESSION_COOKIE_SECURE:
        drift.append("Cookie Secure differs from the current Builder default.")
    if not project.get("simulated") and not project.get("profile_managed"):
        drift.extend(managed_project_issues(project["name"])[:4])
    project["configuration_drift"] = drift
    project["contract_status"] = "Review" if drift else "Current"
    project["last_rebuild"] = (
        "2026-07-27 20:01" if project.get("simulated")
        else last_action_timestamp(project["name"], "rebuild")
    )
    return project


def build_env(project, glpi_image, mariadb_image, host_port, container_port, tz, clean_db, cookie_samesite=None, cookie_secure=None, isolated_restore=False):
    old = read_env(project) if env_file(project).exists() and not clean_db else {}
    db_password = old.get("GLPI_DB_PASSWORD") or safe_password()
    root_password = old.get("MARIADB_ROOT_PASSWORD") or safe_password()

    return {
        "PROJECT_NAME": project,
        "GLPI_IMAGE": glpi_image,
        "MARIADB_IMAGE": mariadb_image,
        "GLPI_HTTP_PORT": str(host_port),
        "GLPI_CONTAINER_PORT": str(container_port),
        "GLPI_SESSION_COOKIE_SAMESITE": validate_cookie_samesite(cookie_samesite or old.get("GLPI_SESSION_COOKIE_SAMESITE")),
        "GLPI_SESSION_COOKIE_SECURE": validate_cookie_secure(cookie_secure or old.get("GLPI_SESSION_COOKIE_SECURE")),
        "MARIADB_ROOT_PASSWORD": root_password,
        "GLPI_DB_NAME": old.get("GLPI_DB_NAME") or "glpi",
        "GLPI_DB_USER": old.get("GLPI_DB_USER") or "glpiuser",
        "GLPI_DB_PASSWORD": db_password,
        "TZ": tz,
        "BUILDER_QUARANTINE": "1" if isolated_restore else "0",
    }


def yaml_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def indent_text(text, spaces):
    prefix = " " * spaces
    return "\n".join(prefix + line if line else prefix for line in text.splitlines())


def write_compose(project, env):
    """Write project Compose using the custom v7 structure.

    This YAML intentionally stays very close to the proven v7 template. The
    GLPI image was sensitive to small entrypoint and volume changes, so SSO
    cookie settings are added only through .env and the entrypoint. Service and
    volume structure must remain unchanged.
    """
    env = normalize_env_defaults(env)
    entrypoint_script = indent_text(GLPI_ENTRY_COMMAND, 8)
    network_internal_line = "\n    internal: true" if env.get("BUILDER_QUARANTINE") == "1" else ""
    compose = f"""services:
  {project}-db:
    image: {env["MARIADB_IMAGE"]}
    container_name: {project}-db
    restart: unless-stopped

    env_file:
      - .env

    environment:
      MARIADB_ROOT_PASSWORD: ${{MARIADB_ROOT_PASSWORD}}
      MARIADB_DATABASE: ${{GLPI_DB_NAME}}
      MARIADB_USER: ${{GLPI_DB_USER}}
      MARIADB_PASSWORD: ${{GLPI_DB_PASSWORD}}

      GLPI_DB_NAME: ${{GLPI_DB_NAME}}
      GLPI_DB_USER: ${{GLPI_DB_USER}}
      GLPI_DB_PASSWORD: ${{GLPI_DB_PASSWORD}}
      TZ: ${{TZ}}

    volumes:
      - /volume1/docker/{project}/db:/var/lib/mysql:rw

    networks:
      - {project}-network


  {project}:
    image: {env["GLPI_IMAGE"]}
    container_name: {project}
    restart: unless-stopped
    user: "0:0"

    depends_on:
      - {project}-db

    ports:
      - "${{GLPI_HTTP_PORT}}:8080"

    volumes:
      - /volume1/docker/{project}/glpi:/var/glpi:rw
      - /volume1/docker/{project}/plugins:/var/www/glpi/plugins:rw

    env_file:
      - .env

    environment:
      GLPI_DB_HOST: {project}-db
      GLPI_DB_PORT: 3306
      GLPI_DB_NAME: ${{GLPI_DB_NAME}}
      GLPI_DB_USER: ${{GLPI_DB_USER}}
      GLPI_DB_PASSWORD: ${{GLPI_DB_PASSWORD}}

      GLPI_SKIP_AUTOINSTALL: "true"
      GLPI_SKIP_AUTOUPDATE: "true"
      GLPI_CRONTAB_ENABLED: "1"
      GLPI_SESSION_COOKIE_SAMESITE: ${{GLPI_SESSION_COOKIE_SAMESITE}}
      GLPI_SESSION_COOKIE_SECURE: ${{GLPI_SESSION_COOKIE_SECURE}}

      TZ: ${{TZ}}
      TIMEZONE: ${{TZ}}

    networks:
      - {project}-network

    entrypoint:
      - /bin/sh
      - -c
      - |
{entrypoint_script}


networks:
  {project}-network:
    name: {project}-network
    driver: bridge{network_internal_line}
"""
    compose_file(project).write_text(compose, encoding="utf-8")

def ensure_glpi_writable_dirs(project):
    glpi_dir = project_dir(project) / "glpi"
    plugins_dir = project_dir(project) / "plugins"
    glpi_dir.mkdir(parents=True, exist_ok=True)
    plugins_dir.mkdir(parents=True, exist_ok=True)
    for sub in GLPI_WRITABLE_SUBDIRS:
        (glpi_dir / sub).mkdir(parents=True, exist_ok=True)


def ensure_dirs(project):
    for folder in [
        project_dir(project),
        project_dir(project) / "db",
        project_dir(project) / "glpi",
        project_dir(project) / "plugins",
    ]:
        folder.mkdir(parents=True, exist_ok=True)
    ensure_glpi_writable_dirs(project)


def get_container(name):
    try:
        return docker_client().containers.get(name)
    except NotFound:
        return None


def remove_container(name):
    c = get_container(name)
    if not c:
        return False
    c.remove(force=True)
    return True


def ensure_network(project, internal=False):
    cli = docker_client()
    name = f"{project}-network"
    try:
        network = cli.networks.get(name)
        network.reload()
        actual = bool(network.attrs.get("Internal"))
        if actual != bool(internal):
            raise RuntimeError(f"Docker network {name} has internal={actual}; expected internal={bool(internal)}.")
        return network
    except NotFound:
        return cli.networks.create(name, driver="bridge", internal=bool(internal))


def ensure_container_network(project, container_name, internal=False):
    cli = docker_client()
    net = ensure_network(project, internal=internal)
    c = cli.containers.get(container_name)
    c.reload()
    networks = c.attrs.get("NetworkSettings", {}).get("Networks", {}) or {}
    if f"{project}-network" not in networks:
        net.connect(c, aliases=[container_name])
        c.reload()
    return c


def wait_db(project, seconds=180):
    deadline = time.time() + seconds
    last = ""
    while time.time() < deadline:
        try:
            db = docker_client().containers.get(f"{project}-db")
            result = db.exec_run(
                'sh -lc \'case "$(cat /proc/1/comm 2>/dev/null)" in '
                'mariadbd|mysqld) mariadb-admin ping -uroot -p"$MARIADB_ROOT_PASSWORD" --silent ;; '
                '*) exit 1 ;; esac\'',
                demux=True,
            )
            stdout, stderr = result.output if isinstance(result.output, tuple) else (result.output, b"")
            last = ((stdout or b"") + (stderr or b"")).decode("utf-8", errors="replace")
            if result.exit_code == 0:
                return True, "MariaDB is ready."
        except Exception as exc:
            last = str(exc)
        time.sleep(3)
    return False, f"MariaDB did not become ready. Last message: {last}"


def container_port_mappings_from_snapshot(container):
    network_ports = container.attrs.get("NetworkSettings", {}).get("Ports", {}) or {}
    configured_ports = container.attrs.get("HostConfig", {}).get("PortBindings", {}) or {}
    mappings = []
    seen = set()
    for ports in (network_ports, configured_ports):
        for private_port, host_entries in ports.items():
            for entry in host_entries or []:
                host_port = entry.get("HostPort")
                host_ip = entry.get("HostIp") or "0.0.0.0"
                key = (str(private_port), str(host_ip), str(host_port))
                if host_port and key not in seen:
                    seen.add(key)
                    mappings.append({
                        "private": str(private_port),
                        "host_ip": str(host_ip),
                        "host_port": str(host_port),
                        "mapping": f"{host_ip}:{host_port}->{private_port}",
                    })
    return mappings


def container_port_mappings(container):
    try:
        container.reload()
    except Exception:
        pass
    return container_port_mappings_from_snapshot(container)


def host_ports_for_container(name, private_port="8080/tcp"):
    c = get_container(name)
    if not c:
        return []
    values = []
    for mapping in container_port_mappings(c):
        if mapping["private"] == private_port:
            try:
                values.append(int(mapping["host_port"]))
            except Exception:
                pass
    return values


def docker_port_usage(host_port, exclude_containers=None):
    exclude_containers = set(exclude_containers or [])
    try:
        containers = docker_client().containers.list(all=True)
    except Exception:
        return None
    for container in containers:
        if container.name in exclude_containers:
            continue
        for mapping in container_port_mappings(container):
            try:
                if int(mapping["host_port"]) == int(host_port):
                    return {
                        "container": container.name,
                        "status": getattr(container, "status", "unknown"),
                        "mapping": mapping["mapping"],
                    }
            except Exception:
                continue
    return None


def configured_project_ports(exclude_projects=None):
    exclude_projects = set(exclude_projects or [])
    reservations = {}
    if not BASE_PATH.is_dir():
        return reservations
    try:
        for index, folder in enumerate(BASE_PATH.iterdir()):
            if index >= MAX_SCAN_ENTRIES:
                break
            if folder.name in exclude_projects:
                continue
            application_manifest = read_application_manifest(folder)
            if application_manifest:
                reservations.setdefault(int(application_manifest["port"]), folder.name)
                continue
            if not is_managed_glpi_project(folder.name):
                continue
            try:
                if not folder.is_dir() or folder.is_symlink():
                    continue
                value = read_env(folder.name).get("GLPI_HTTP_PORT", "")
                port = validate_port(value, "GLPI_HTTP_PORT")
            except Exception:
                continue
            reservations.setdefault(port, folder.name)
    except OSError:
        return reservations
    return reservations


def suggest_free_host_port(start=8775, end=65535, containers=None):
    start = validate_port(start, "Port search start")
    end = validate_port(end, "Port search end")
    if end < start:
        raise ValueError("Port search end must be greater than or equal to the start.")
    used_ports = set()
    use_snapshot = containers is not None
    if containers is None:
        try:
            containers = docker_client().containers.list(all=True)
        except Exception:
            containers = []
    for container in containers:
        mappings = (
            container_port_mappings_from_snapshot(container)
            if use_snapshot
            else container_port_mappings(container)
        )
        for mapping in mappings:
            try:
                used_ports.add(int(mapping["host_port"]))
            except Exception:
                continue
    used_ports.update(configured_project_ports())
    for candidate in range(start, end + 1):
        if candidate not in used_ports:
            return candidate
    raise ValueError(f"No free Docker host port is available between {start} and {end}.")


def assert_docker_port_free(host_port, exclude_containers=None, exclude_projects=None):
    usage = docker_port_usage(host_port, exclude_containers=exclude_containers)
    if usage:
        raise ValueError(
            f"Port {host_port} is already used by Docker container {usage['container']} "
            f"({usage['mapping']}, status: {usage['status']}). Choose a different host port."
        )
    project_exclusions = set(exclude_projects or []) | set(exclude_containers or [])
    project = configured_project_ports(exclude_projects=project_exclusions).get(int(host_port))
    if project:
        raise ValueError(
            f"Port {host_port} is already reserved by project {project} in its .env file. "
            "Choose a different host port."
        )


def project_has_existing_state(project):
    if get_container(project) or get_container(f"{project}-db"):
        return True
    folder = project_dir(project)
    if not folder.exists():
        return False
    try:
        for child in folder.iterdir():
            if child.name != "_builder_logs":
                return True
    except Exception:
        pass
    return False


def form_flag(source, name):
    value = source.get(name)
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def require_destructive_confirmation(project, source=None):
    source = source or request.form
    checkbox = form_flag(source, "confirm_destructive")
    typed = (source.get("confirm_project") or "").strip().lower()
    if not checkbox:
        raise ValueError(
            f"Project {project} already exists. Enable Overwrite existing project under Advanced settings."
        )
    if typed != project:
        raise ValueError("Type the exact project name to confirm the overwrite.")


def get_csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


@app.context_processor
def inject_csrf_token():
    return {
        "csrf_token": get_csrf_token(),
        "app_version": APP_VERSION,
        "test_preview_active": (
            BUILDER_TEST_PREVIEW_MODE
            and bool(session.get("test_preview_active"))
        ),
    }


def require_csrf():
    expected = session.get("csrf_token", "")
    supplied = request.form.get("csrf_token", "")
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        raise ValueError("The security token is missing or expired. Reload the page and try again.")


def validate_backup_choice(value, extensions, label, allow_dir=False):
    value = str(value or "").strip()
    if not value:
        return ""
    path = Path(value).resolve()
    if not path.exists():
        raise ValueError(f"{label} does not exist: {path}")
    if path.is_symlink():
        raise ValueError(f"{label} must not be a symlink.")
    if not path_under_backup_root(path):
        raise ValueError(f"{label} must be located below {BACKUP_ROOT}.")
    if path.is_dir():
        if allow_dir:
            return str(path)
        raise ValueError(f"{label} must be a file.")
    if not path.is_file() or not path.name.lower().endswith(extensions):
        allowed = ", ".join(extensions)
        raise ValueError(f"{label} has an unsupported format ({allowed}).")
    return str(path)


def inspect_glpi_backup_set(database_value, files_value):
    database = Path(database_value).resolve()
    files = Path(files_value).resolve()
    if database.parent != files.parent:
        raise ValueError("GLPI isolated restore requires database and files from the same backup set.")
    folder = database.parent
    manifest_path = folder / "manifest.json"
    checksums_path = folder / "SHA256SUMS"
    try:
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("GLPI isolated restore requires a valid Builder backup manifest.") from exc
    if manifest_data.get("application") not in {None, "", "glpi"}:
        raise ValueError("The selected backup manifest does not belong to GLPI.")
    manifest_database = _safe_backup_member(folder, manifest_data.get("database"))
    manifest_files = _safe_backup_member(folder, manifest_data.get("files"))
    if manifest_database != database or manifest_files != files:
        raise ValueError("Selected GLPI backup files do not match the backup manifest.")
    expected = {}
    try:
        for line in checksums_path.read_text(encoding="utf-8").splitlines():
            digest, name = line.strip().split(None, 1)
            expected[Path(name.lstrip("* ")).name] = digest.lower()
    except (OSError, ValueError) as exc:
        raise ValueError("GLPI isolated restore requires a valid SHA256SUMS file.") from exc
    for member in (database, files):
        if not hmac.compare_digest(expected.get(member.name, ""), sha256_file(member)):
            raise ValueError(f"GLPI backup checksum mismatch for {member.name}.")
    return {
        "manifest": manifest_data,
        "database": str(database),
        "files": str(files),
        "database_sha256": sha256_file(database),
        "files_sha256": sha256_file(files),
        "application_version": str(manifest_data.get("application_version") or "Unknown"),
        "database_version": str(manifest_data.get("database_version") or "Unknown"),
    }


def validate_create_request(source):
    project = validate_project(source.get("project"))
    glpi_image = validate_local_image(source.get("glpi_image"), "glpi")
    mariadb_image = validate_local_image(source.get("mariadb_image"), "database")
    if not mariadb_image.startswith("mariadb:"):
        raise ValueError("The current GLPI restore adapter supports locally installed mariadb:* images.")
    host_port = validate_port(source.get("host_port"), "Host port")
    container_port = validate_port(source.get("container_port"), "Container port")
    if container_port != 8080:
        raise ValueError(
            "This version follows the proven YAML and uses internal port 8080. "
            "Keep the container port set to 8080."
        )

    tz = str(source.get("tz") or TZ_DEFAULT).strip() or TZ_DEFAULT
    cookie_samesite = validate_cookie_samesite(source.get("cookie_samesite"))
    cookie_secure = validate_cookie_secure(source.get("cookie_secure"))
    operation_mode = str(source.get("operation_mode") or "restore").strip().lower()
    if operation_mode not in OPERATION_MODES:
        raise ValueError("Operation mode must be Full restore or Fresh installation.")
    fresh_install = operation_mode == "fresh"
    isolated_restore = operation_mode == "isolated"
    clean_db = fresh_install or isolated_restore
    force_recreate = True
    restore_everything = not fresh_install
    skip_plugins = fresh_install or form_flag(source, "skip_plugins")
    update_backup_source = form_flag(source, "update_backup_source")
    if isolated_restore:
        update_backup_source = False
    db_backup = (
        source.get("db_backup")
        or source.get("db_backup_manual")
        or source.get("db_backup_select")
        or ""
    )
    file_backup = (
        source.get("file_backup")
        or source.get("file_backup_manual")
        or source.get("file_backup_select")
        or ""
    )
    db_backup = validate_backup_choice(db_backup, DB_EXTENSIONS, "Database backup")
    file_backup = validate_backup_choice(
        file_backup,
        FILE_EXTENSIONS,
        "GLPI files backup",
        allow_dir=True,
    )

    if operation_mode in {"restore", "isolated"} and not db_backup:
        raise ValueError("Full restore requires a database backup.")
    if operation_mode in {"restore", "isolated"} and not file_backup:
        raise ValueError("Full restore requires a GLPI files/config backup.")
    if fresh_install and (db_backup or file_backup):
        raise ValueError("Fresh installation must not include backup selections. Clear both backup fields first.")

    existing_state = project_has_existing_state(project)
    if isolated_restore and existing_state:
        raise ValueError("Isolated restore requires a new unused project name.")
    backup_inspection = inspect_glpi_backup_set(db_backup, file_backup) if isolated_restore else None
    if existing_state and (clean_db or db_backup or file_backup):
        require_destructive_confirmation(project, source)

    if not force_recreate and get_container(project):
        current_ports = host_ports_for_container(project)
        if current_ports and host_port not in current_ports:
            raise ValueError(
                f"Project {project} already has a GLPI container on port {current_ports[0]}. "
                "Use Change port or select Recreate existing GLPI container."
            )

    exclude = {project} if force_recreate else set()
    assert_docker_port_free(host_port, exclude_containers=exclude)
    return {
        "project": project,
        "glpi_image": glpi_image,
        "mariadb_image": mariadb_image,
        "host_port": host_port,
        "container_port": container_port,
        "tz": tz,
        "cookie_samesite": cookie_samesite,
        "cookie_secure": cookie_secure,
        "operation_mode": operation_mode,
        "fresh_install": fresh_install,
        "isolated_restore": isolated_restore,
        "backup_inspection": backup_inspection,
        "clean_db": clean_db,
        "force_recreate": force_recreate,
        "restore_everything": restore_everything,
        "skip_plugins": skip_plugins,
        "update_backup_source": update_backup_source,
        "db_backup": db_backup,
        "file_backup": file_backup,
        "existing_state": existing_state,
        "confirm_destructive": form_flag(source, "confirm_destructive"),
        "confirm_project": str(source.get("confirm_project") or ""),
        "backup_root": str(source.get("backup_root") or BACKUP_ROOT),
    }


def build_create_plan(data):
    fresh_install = data["fresh_install"]
    isolated_restore = bool(data.get("isolated_restore"))
    destructive = bool(data["existing_state"])
    mode_title = "Fresh installation" if fresh_install else ("Isolated test restore" if isolated_restore else "Full restore")
    database_action = "delete all database storage and install an empty GLPI database" if fresh_install else "replace the GLPI database from the selected backup"
    files_action = "use only the original files from the GLPI image" if fresh_install else "restore GLPI config and files from backup"
    plugin_action = "start without plugins" if data["skip_plugins"] else "restore plugins from backup"
    backup_action = ("disabled for isolated test environments" if isolated_restore else ("set this project as the scheduled backup source" if data.get("update_backup_source") else "keep the current scheduled backup source"))
    steps = [
        "Repeat the preflight checks (images, backups and free port)",
        "Write project folders, .env and the locked compose configuration",
    ]
    if fresh_install:
        steps.extend([
            "Delete the database storage, GLPI config/files and plugin data",
            "Create a clean database container and run the one-time GLPI installer as www-data",
            "Verify config_db.php and save the complete action log",
        ])
    else:
        steps.extend([
            "Import the required database backup",
            "Restore the required GLPI files/config backup",
            "Remove plugin data when Restore without plugins is selected",
            "Reapply the GLPI container and save the complete action log",
        ])
    if data.get("update_backup_source"):
        steps.append("Update the scheduled backup script and backup.env for this project")
    return {
        "title": mode_title,
        "risk": "High - all existing project data will be deleted" if fresh_install else ("Contained - restored into a new project on an internal Docker network" if isolated_restore else ("High - existing project data will be replaced" if destructive else "Normal - required backups will be restored")),
        "destructive": destructive,
        "rows": [
            ("Project", data["project"]),
            ("Mode", mode_title),
            ("Web port", f"{data['host_port']}:8080"),
            ("GLPI image", data["glpi_image"]),
            ("Database image", data["mariadb_image"]),
            ("Database", database_action),
            ("GLPI files/config", files_action),
            ("Plugins", plugin_action),
            ("Scheduled backups", backup_action),
            ("Database backup", data["db_backup"] or "none"),
            ("GLPI files backup", data["file_backup"] or "none"),
            ("Cookie policy", f"SameSite={data['cookie_samesite']}, Secure={data['cookie_secure']}"),
            ("Time zone", data["tz"]),
        ],
        "steps": steps,
    }


def store_create_preview(data):
    token = secrets.token_urlsafe(32)
    session["pending_create_preview"] = {
        "token": token,
        "created_at": int(time.time()),
        "data": data,
    }
    session.modified = True
    return token


def consume_create_preview(token):
    pending = session.pop("pending_create_preview", None)
    session.modified = True
    if not pending or not token or not hmac.compare_digest(str(pending.get("token", "")), str(token)):
        raise ValueError("The execution preview is missing or invalid. Create a new preview.")
    created_at = int(pending.get("created_at") or 0)
    if created_at < int(time.time()) - CREATE_PREVIEW_TTL_SECONDS:
        raise ValueError("The execution preview has expired. Review the plan again.")
    return pending["data"]


def create_progress_job(project, backup_root, kind="restore"):
    kind = str(kind or "restore").strip().lower()
    if kind not in {"restore", "backup"}:
        raise ValueError("Progress job kind must be restore or backup.")
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    title = "Backup" if kind == "backup" else "Restore"
    job = {
        "token": token,
        "project": project,
        "kind": kind,
        "title": title,
        "backup_root": str(backup_root or BACKUP_ROOT),
        "status": "queued",
        "percent": 2,
        "stage": "Queued",
        "messages": [f"{title} job accepted and waiting to start."],
        "created_at": now,
        "updated_at": now,
        "finished_at": 0,
        "log_name": "",
        "error": "",
    }
    with PROGRESS_LOCK:
        cutoff = now - PROGRESS_JOB_TTL_SECONDS
        expired = [
            key for key, value in PROGRESS_JOBS.items()
            if value.get("finished_at") and value.get("finished_at", 0) < cutoff
        ]
        for key in expired:
            PROGRESS_JOBS.pop(key, None)
        PROGRESS_JOBS[token] = job
    return token


def update_progress_job(token, percent=None, stage=None, message=None, status=None, log_name=None, error=None):
    now = int(time.time())
    with PROGRESS_LOCK:
        job = PROGRESS_JOBS.get(token)
        if not job:
            return
        if percent is not None:
            job["percent"] = max(0, min(100, int(percent)))
        if stage is not None:
            job["stage"] = str(stage)
        if message:
            job["messages"].append(str(message))
            job["messages"] = job["messages"][-60:]
        if status is not None:
            job["status"] = status
        if log_name is not None:
            job["log_name"] = log_name
        if error is not None:
            job["error"] = str(error)
        job["updated_at"] = now
        if status in {"completed", "failed"}:
            job["finished_at"] = now


def progress_job_snapshot(token):
    with PROGRESS_LOCK:
        job = PROGRESS_JOBS.get(token)
        if not job:
            return None
        result = dict(job)
        result["messages"] = list(job["messages"])
        return result


def execute_scheduled_backup(project, on_line=None):
    project = validate_project(project)
    status = scheduled_backup_status(project)
    if not status["ready"]:
        details = "; ".join(status["issues"]) or "Scheduled backup is not ready."
        raise ValueError(details)

    environment = dict(os.environ)
    environment["GLPI_BACKUP_ENV"] = str(backup_schedule_path(project))
    process = subprocess.Popen(
        ["/bin/bash", str(BACKUP_SCRIPT_PATH)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=environment,
    )
    lines = []
    for raw_line in process.stdout or []:
        line = raw_line.rstrip()
        if not line:
            continue
        lines.append(line)
        lines = lines[-200:]
        if on_line:
            on_line(line)
    return_code = process.wait()
    if return_code != 0:
        raise RuntimeError("Manual backup failed:\n" + "\n".join(lines[-40:]))
    return lines


def run_backup_job(job_token, project):
    messages = []
    log_name = ""
    stage_map = (
        ("Starting GLPI backup", 10, "Starting backup"),
        ("Copying the credential file", 25, "Preparing database access"),
        ("Creating database dump", 42, "Creating database dump"),
        ("Creating portable GLPI files archive", 68, "Archiving GLPI files"),
        ("Removing GLPI backups older", 90, "Applying retention"),
        ("GLPI backup completed", 98, "Finalizing backup"),
    )
    try:
        update_progress_job(job_token, 5, "Checking backup readiness", "Validating backup configuration and database status.", status="running")

        def report_line(line):
            messages.append(line)
            percent, stage = 15, "Running backup"
            for marker, marker_percent, marker_stage in stage_map:
                if marker in line:
                    percent, stage = marker_percent, marker_stage
                    break
            update_progress_job(job_token, percent, stage, line)

        execute_scheduled_backup(project, on_line=report_line)
        latest = latest_successful_backup(project)
        if not latest:
            raise RuntimeError("Backup command completed but no verified project backup was found.")
        messages.extend([
            f"Backup folder: {latest['path']}",
            f"Backup size: {latest['size_label']}",
            f"Checksum manifest: {'available' if latest['checksum_manifest'] else 'missing'}",
        ])
        log_name = write_action_log(project, "manual-backup", messages)
        update_progress_job(
            job_token,
            100,
            "Backup completed",
            f"Backup completed successfully: {latest['name']} ({latest['size_label']}).",
            status="completed",
            log_name=log_name,
        )
    except Exception as exc:
        error_text = str(exc)
        try:
            log_name = write_action_log(project, "manual-backup-error", ["ERROR", error_text] + messages)
        except Exception:
            log_name = ""
        update_progress_job(
            job_token,
            stage="Backup failed",
            message="The manual backup failed. Review the error and action log.",
            status="failed",
            log_name=log_name,
            error=error_text,
        )
    finally:
        invalidate_dashboard_cache()
        MUTATION_LOCK.release()


def is_glpi_backup_path(path, root):
    """Return true only when a backup path is explicitly identifiable as GLPI."""
    try:
        relative = Path(path).resolve().relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return False
    return any("glpi" in part.lower() for part in relative.parts)


def scan_files(root, extensions, include_dirs=False):
    root = Path(root or BACKUP_ROOT).resolve()
    if not root.exists() or not root.is_dir() or not path_under_backup_root(root):
        return []
    files = []
    for index, item in enumerate(root.rglob("*")):
        if index >= MAX_SCAN_ENTRIES * 20:
            break
        if item.is_symlink():
            continue
        if not is_glpi_backup_path(item, root):
            continue
        if item.is_file() and item.name.lower().endswith(extensions):
            try:
                files.append((item.stat().st_mtime, item))
            except OSError:
                pass
        elif include_dirs and item.parent == root and item.is_dir():
            try:
                files.append((item.stat().st_mtime, item))
            except OSError:
                pass
    files.sort(key=lambda x: x[0], reverse=True)
    return [(str(path), f"{path} ({time.strftime('%Y-%m-%d %H:%M', time.localtime(mtime))})") for mtime, path in files[:MAX_SCAN_ENTRIES]]


def scan_backup_choices(root, include_dirs=False):
    """Collect database and file backup choices in one filesystem traversal."""
    root = Path(root or BACKUP_ROOT).resolve()
    result = {"database": [], "files": []}
    if not root.exists() or not root.is_dir() or not path_under_backup_root(root):
        return result

    for index, item in enumerate(root.rglob("*")):
        if index >= MAX_SCAN_ENTRIES * 20:
            break
        if item.is_symlink():
            continue
        if not is_glpi_backup_path(item, root):
            continue
        try:
            if item.is_file():
                entry = (item.stat().st_mtime, item)
                lowered = item.name.lower()
                if lowered.endswith(DB_EXTENSIONS):
                    result["database"].append(entry)
                if lowered.endswith(FILE_CHOICE_EXTENSIONS):
                    result["files"].append(entry)
        except OSError:
            continue

    for key in result:
        result[key].sort(key=lambda entry: entry[0], reverse=True)
        result[key] = [
            (str(path), f"{path} ({time.strftime('%Y-%m-%d %H:%M', time.localtime(mtime))})")
            for mtime, path in result[key][:MAX_SCAN_ENTRIES]
        ]
    return result


def write_action_log(project, action, messages):
    action = SAFE_ACTION_RE.sub("-", (action or "action").lower()).strip("-") or "action"
    folder = log_dir(project)
    folder.mkdir(parents=True, exist_ok=True)
    filename = datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{action}.log"
    path = folder / filename
    content = [
        f"Docker App Manager {APP_VERSION}",
        f"Project: {project}",
        f"Action: {action}",
        f"Time: {datetime.now().isoformat(timespec='seconds')}",
        "",
    ]
    if isinstance(messages, (list, tuple)):
        content.extend(str(m) for m in messages)
    else:
        content.append(str(messages))
    path.write_text("\n\n".join(content) + "\n", encoding="utf-8")
    return filename


def list_logs(project, limit=8):
    folder = log_dir(project)
    if not folder.exists():
        return []
    logs = []
    for item in folder.glob("*.log"):
        if LOG_FILE_RE.match(item.name):
            try:
                logs.append((item.stat().st_mtime, item.name))
            except OSError:
                pass
    logs.sort(reverse=True)
    return [name for _, name in logs[:limit]]


def latest_log(project):
    logs = list_logs(project, limit=1)
    return logs[0] if logs else ""


def flash_action_success(project, action, messages):
    log_name = write_action_log(project, action, messages)
    preview_items = []
    for msg in list(messages)[:12]:
        msg = str(msg)
        if len(msg) > 1200:
            msg = msg[:600] + "\n...\n" + msg[-600:]
        preview_items.append(text_to_html(msg))
    if len(messages) > 12:
        preview_items.append(f"Another {len(messages) - 12} lines are available in the log file.")
    preview_items.append(
        "Full log: "
        + f"<a href=\"{url_for('view_log', project=project, filename=log_name)}\">{esc(log_name)}</a>"
    )
    flash("<br>".join(preview_items), "ok")


def flash_error(message, project=None, action="error"):
    suffix = ""
    if project:
        try:
            log_name = write_action_log(project, action, ["ERROR", message])
            suffix = "<br>Error log: " + f"<a href=\"{url_for('view_log', project=project, filename=log_name)}\">{esc(log_name)}</a>"
        except Exception:
            suffix = ""
    lower = str(message).lower()
    if "port" in lower:
        cause = "The requested port is unavailable or outside the allowed range."
        next_step = "Choose another unused TCP port and review the project again."
    elif "backup" in lower or "archive" in lower:
        cause = "A restore source is missing, incomplete or outside the configured backup root."
        next_step = "Open Backups, verify the database/files pair, then retry."
    elif "image" in lower:
        cause = "The selected container image is not locally available or not allowed."
        next_step = "Load an allowed image on the Synology and select it again."
    elif "database" in lower or "mariadb" in lower:
        cause = "The database container or credentials did not pass validation."
        next_step = "Run database diagnostics and review the linked action log."
    else:
        cause = "A safety or runtime check prevented the requested action."
        next_step = "Review the details below and retry only after correcting the cause."
    html = (
        '<div class="error-summary"><strong>Action could not be completed</strong>'
        f"<span>{esc(cause)}</span><span><b>Next step:</b> {esc(next_step)}</span>"
        f"<details><summary>Technical details</summary>{text_to_html(message)}</details>"
        f"{suffix}</div>"
    )
    flash(html, "err")


def reset_db_user(project):
    env = read_env(project)
    db = docker_client().containers.get(f"{project}-db")

    root_pw = env["MARIADB_ROOT_PASSWORD"]
    user = sql_escape(env["GLPI_DB_USER"])
    pw = sql_escape(env["GLPI_DB_PASSWORD"])
    db_name = validate_db_identifier(env.get("GLPI_DB_NAME", "glpi"))

    sql = (
        f"CREATE USER IF NOT EXISTS '{user}'@'%' IDENTIFIED BY '{pw}'; "
        f"ALTER USER '{user}'@'%' IDENTIFIED BY '{pw}'; "
        f"GRANT ALL PRIVILEGES ON {sql_identifier(db_name)}.* TO '{user}'@'%'; "
        "FLUSH PRIVILEGES;"
    )

    result = db.exec_run(["mariadb", "-uroot", f"-p{root_pw}", "-e", sql], demux=True)
    stdout, stderr = result.output if isinstance(result.output, tuple) else (result.output, b"")
    output = ((stdout or b"") + (stderr or b"")).decode("utf-8", errors="replace")
    return result.exit_code == 0, output or "Database user password was synchronized with .env."


def restore_database(project, backup_file):
    env = read_env(project)
    backup_path = Path(backup_file).resolve()
    db_name = validate_db_identifier(env.get("GLPI_DB_NAME", "glpi"))

    if not backup_path.exists() or not backup_path.is_file():
        return False, f"Database backup does not exist: {backup_path}"
    if not path_under_backup_root(backup_path):
        return False, f"Database backup must be located below {BACKUP_ROOT}."
    if backup_path.is_symlink():
        return False, "Database backup must not be a symlink."
    if not backup_path.name.lower().endswith(DB_EXTENSIONS):
        return False, "Only .sql, .sql.gz, .dump and .dump.gz database backups are supported."

    command = r"""sh -lc '
set -eu

echo "Waiting for MariaDB TCP in the database container..."
for i in $(seq 1 60); do
  if mariadb -h "$DB_HOST" -uroot -p"$MARIADB_ROOT_PASSWORD" -e "SELECT 1" >/dev/null 2>&1; then
    echo "MariaDB TCP is available."
    break
  fi
  echo "Not available yet, attempt $i/60..."
  sleep 2
done

mariadb -h "$DB_HOST" -uroot -p"$MARIADB_ROOT_PASSWORD" -e "SELECT 1" >/dev/null

echo "Resetting database $GLPI_DB_NAME..."
mariadb -h "$DB_HOST" -uroot -p"$MARIADB_ROOT_PASSWORD" -e "
DROP DATABASE IF EXISTS \`$GLPI_DB_NAME\`;
CREATE DATABASE \`$GLPI_DB_NAME\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
"

echo "Importing database backup: $BACKUP_FILE"
case "$BACKUP_FILE" in
  *.gz)
    gzip -dc "$BACKUP_FILE" | mariadb -h "$DB_HOST" -uroot -p"$MARIADB_ROOT_PASSWORD" "$GLPI_DB_NAME"
    ;;
  *)
    mariadb -h "$DB_HOST" -uroot -p"$MARIADB_ROOT_PASSWORD" "$GLPI_DB_NAME" < "$BACKUP_FILE"
    ;;
esac

echo "Database restore completed."
' """

    try:
        output = docker_client().containers.run(
            env["MARIADB_IMAGE"],
            command=command,
            remove=True,
            detach=False,
            environment={
                "DB_HOST": "127.0.0.1",
                "MARIADB_ROOT_PASSWORD": env["MARIADB_ROOT_PASSWORD"],
                "GLPI_DB_NAME": db_name,
                "BACKUP_FILE": str(backup_path),
            },
            volumes={str(BASE_PATH): {"bind": str(BASE_PATH), "mode": "ro"}},
            network_mode=f"container:{project}-db",
            stdout=True,
            stderr=True,
        )
        text = output.decode("utf-8", errors="replace") if isinstance(output, (bytes, bytearray)) else str(output)
        ok, msg = reset_db_user(project)
        if not ok:
            return False, text + "\n" + msg
        return True, text + "\n" + msg
    except ContainerError as exc:
        text = ""
        stderr = getattr(exc, "stderr", None)
        stdout = getattr(exc, "stdout", None)
        if stderr:
            text += stderr.decode("utf-8", errors="replace") if isinstance(stderr, (bytes, bytearray)) else str(stderr)
        if stdout:
            text += "\n" + (stdout.decode("utf-8", errors="replace") if isinstance(stdout, (bytes, bytearray)) else str(stdout))
        return False, text or str(getattr(exc, "explanation", exc))
    except Exception as exc:
        return False, str(exc)


def empty_dir(path):
    path.mkdir(parents=True, exist_ok=True)
    for child in path.iterdir():
        if child.is_symlink() or child.is_file():
            child.unlink()
        elif child.is_dir():
            shutil.rmtree(child)
        else:
            raise ValueError(f"Unknown file type in target folder: {child}")


def copy_tree_contents(src, dst):
    src = Path(src)
    dst = Path(dst)
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.is_symlink():
            raise ValueError(f"Symlink in GLPI files backup was rejected: {item}")
        target = dst / item.name
        if item.is_dir():
            if target.exists():
                if target.is_symlink() or target.is_file():
                    target.unlink()
                else:
                    shutil.rmtree(target)
            copy_tree_contents(item, target)
        elif item.is_file():
            if target.exists():
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
        else:
            raise ValueError(f"Unknown file type in GLPI files backup: {item}")


def safe_member_target(dest, member_name, archive_type):
    if not member_name or "\x00" in member_name:
        raise ValueError(f"Unsafe path in {archive_type} archive.")
    pure = Path(member_name)
    if pure.is_absolute():
        raise ValueError(f"Absolute path in {archive_type} archive was rejected: {member_name}")
    dest_resolved = dest.resolve()
    target = (dest / member_name).resolve()
    try:
        target.relative_to(dest_resolved)
    except Exception:
        raise ValueError(f"Path outside the target folder in {archive_type} archive was rejected: {member_name}")
    return target


def safe_extract_zip(zip_path, dest):
    dest = Path(dest)
    with zipfile.ZipFile(zip_path) as z:
        for member in z.infolist():
            target = safe_member_target(dest, member.filename, "zip")
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError(f"Symlink in zip archive was rejected: {member.filename}")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            file_type = stat.S_IFMT(mode) if mode else 0
            if file_type and file_type != stat.S_IFREG:
                raise ValueError(f"Unsafe file type in zip archive was rejected: {member.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(member, "r") as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)


def safe_extract_tar(tar_path, dest):
    dest = Path(dest)
    with tarfile.open(tar_path) as t:
        for member in t.getmembers():
            target = safe_member_target(dest, member.name, "tar")
            if member.issym() or member.islnk():
                raise ValueError(f"Symlink or hardlink in tar archive was rejected: {member.name}")
            if member.isdev():
                raise ValueError(f"Device file in tar archive was rejected: {member.name}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise ValueError(f"Unsafe file type in tar archive was rejected: {member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            source = t.extractfile(member)
            if source is None:
                raise ValueError(f"Could not read tar archive member: {member.name}")
            with source, open(target, "wb") as dst:
                shutil.copyfileobj(source, dst)


def extract_source(source):
    source = Path(source).resolve()

    if not source.exists():
        raise ValueError(f"GLPI files backup does not exist: {source}")
    if not path_under_backup_root(source):
        raise ValueError(f"GLPI files backup must be located below {BACKUP_ROOT}.")
    if source.is_symlink():
        raise ValueError("GLPI files backup must not be a symlink.")

    if source.is_dir():
        return source, None

    if not source.name.lower().endswith(FILE_EXTENSIONS):
        raise ValueError("GLPI files backup must be a folder or a supported zip/tar archive.")

    tmp = Path(tempfile.mkdtemp(prefix="glpi_full_restore_"))
    if source.name.lower().endswith(".zip"):
        safe_extract_zip(source, tmp)
    else:
        safe_extract_tar(source, tmp)
    return tmp, tmp


def looks_like_var_glpi(path):
    if not path.is_dir() or path.is_symlink():
        return False
    wanted = {"config", "files", "marketplace", "logs"}
    existing = {p.name for p in path.iterdir() if p.is_dir() and not p.is_symlink()}
    return bool(wanted & existing)


def find_var_glpi_root(root):
    root = Path(root)

    candidates = [
        root / "var" / "glpi",
        root / "glpi",
        root,
    ]

    for cand in candidates:
        if cand.exists() and looks_like_var_glpi(cand):
            return cand

    for cand in root.rglob("glpi"):
        if cand.is_symlink():
            continue
        if looks_like_var_glpi(cand):
            return cand

    return None


def find_plugins_root(root):
    root = Path(root)

    candidates = [
        root / "var" / "www" / "glpi" / "plugins",
        root / "plugins",
        root / "glpi" / "plugins",
    ]

    for cand in candidates:
        if cand.exists() and cand.is_dir() and not cand.is_symlink():
            return cand

    for cand in root.rglob("plugins"):
        if cand.is_dir() and not cand.is_symlink():
            return cand

    return None


def patch_config_db(project):
    env = read_env(project)
    config_root = project_dir(project) / "glpi" / "config"
    if not config_root.exists():
        return "No config folder was found for patching config_db.php."

    replacements = {
        "dbhost": f"{project}-db",
        "dbuser": env["GLPI_DB_USER"],
        "dbpassword": env["GLPI_DB_PASSWORD"],
        "dbdefault": env["GLPI_DB_NAME"],
    }

    changed = []
    for file in config_root.rglob("config_db.php"):
        if file.is_symlink():
            raise ValueError(f"Symlink config_db.php was rejected: {file}")
        text = file.read_text(encoding="utf-8", errors="ignore")
        original = text

        for var, value in replacements.items():
            replacement = rf"\1'{value}';"
            text = re.sub(rf"(public\s+\${var}\s*=\s*)['\"][^'\"]*['\"]\s*;", replacement, text)
            text = re.sub(rf"(\${var}\s*=\s*)['\"][^'\"]*['\"]\s*;", replacement, text)

        if text != original:
            file.write_text(text, encoding="utf-8")
            changed.append(str(file))

    if changed:
        return "Patched config_db.php: " + ", ".join(changed)
    return "No config_db.php file was changed or found."


def clear_plugin_data(project):
    targets = [
        project_dir(project) / "plugins",
        project_dir(project) / "glpi" / "marketplace",
        project_dir(project) / "glpi" / "files" / "_plugins",
    ]
    for target in targets:
        empty_dir(target)
    return "Plugin directories were intentionally emptied. Database references to plugins may still exist."


def prepare_fresh_install(project):
    empty_dir(project_dir(project) / "glpi")
    empty_dir(project_dir(project) / "plugins")
    for folder in ["config", "marketplace", "logs"]:
        (project_dir(project) / "glpi" / folder).mkdir(parents=True, exist_ok=True)
    ensure_glpi_writable_dirs(project)
    return "Removed existing GLPI config, files, marketplace and plugin data for a fresh installation."


def restore_glpi_files(project, file_source, restore_plugins=True):
    root, tmp = extract_source(file_source)
    messages = []

    try:
        var_glpi = find_var_glpi_root(root)
        plugins = find_plugins_root(root)

        if not var_glpi:
            raise ValueError("No GLPI /var/glpi structure was found in the files backup. Expected config/files/marketplace/logs folders.")

        glpi_target = project_dir(project) / "glpi"
        plugins_target = project_dir(project) / "plugins"

        empty_dir(glpi_target)
        copy_tree_contents(var_glpi, glpi_target)
        messages.append(f"Restored /var/glpi from {var_glpi}")

        empty_dir(plugins_target)
        if not restore_plugins:
            messages.append(clear_plugin_data(project))
        elif plugins:
            copy_tree_contents(plugins, plugins_target)
            messages.append(f"Restored plugins from {plugins}")
        else:
            messages.append("No plugins folder was found in the backup; the plugins folder was emptied.")

        ensure_glpi_writable_dirs(project)
        messages.append(patch_config_db(project))
    finally:
        if tmp and tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)

    return messages


def fix_permissions(project):
    roots = [
        project_dir(project) / "glpi",
        project_dir(project) / "plugins",
    ]

    warnings = []
    for root in roots:
        root.mkdir(parents=True, exist_ok=True)
        for path in [root] + list(root.rglob("*")):
            if path.is_symlink():
                warnings.append(f"Skipped symlink: {path}")
                continue
            try:
                os.chown(path, 33, 33)
            except Exception as exc:
                warnings.append(f"chown failed for {path}: {exc}")
            try:
                os.chmod(path, 0o775 if path.is_dir() else 0o664)
            except Exception as exc:
                warnings.append(f"chmod failed for {path}: {exc}")

    msg = "Permissions were applied to the /var/glpi and plugins volumes."
    if warnings:
        msg += "\nLatest warnings:\n" + "\n".join(warnings[-20:])
    return msg


def prepare_db_directory(project):
    """Make a Synology bind mount writable for first-time DB initialization.

    Container Manager creates host folders as root with a restrictive umask.
    MariaDB may already be running as its unprivileged image user when it
    attempts to create ddl_recovery.log, so the image entrypoint cannot always
    repair the host directory itself.
    """
    db_folder = project_dir(project) / "db"
    if db_folder.is_symlink():
        raise RuntimeError(f"Refusing to use a symlink as database directory: {db_folder}")
    db_folder.mkdir(parents=True, exist_ok=True)
    os.chmod(db_folder, 0o777)
    return db_folder


def finalize_db_directory_permissions(project):
    """Replace bootstrap permissions with the UID/GID MariaDB initialized."""
    db_folder = project_dir(project) / "db"
    ownership_source = next(
        (path for path in (db_folder / "mysql", db_folder / "ibdata1") if path.exists()),
        None,
    )
    if ownership_source is None:
        return "Database directory bootstrap permissions remain active; MariaDB ownership was not detected."
    stat = ownership_source.stat()
    try:
        os.chown(db_folder, stat.st_uid, stat.st_gid)
        os.chmod(db_folder, 0o750)
    except OSError as exc:
        return f"MariaDB is running, but database directory permissions could not be tightened: {exc}"
    return "Database directory permissions were tightened after MariaDB initialization."


def create_db_container(project, env, clean_db):
    cli = docker_client()

    if clean_db:
        remove_container(project)
        remove_container(f"{project}-db")
        db_folder = project_dir(project) / "db"
        if db_folder.exists():
            shutil.rmtree(db_folder)

    prepare_db_directory(project)

    existing = get_container(f"{project}-db")
    if existing:
        try:
            existing.reload()
            if existing.status != "running":
                existing.start()
                return f"Existing database container was started: {project}-db"
        except Exception:
            pass
        return "The existing database container is being reused."

    if env.get("BUILDER_QUARANTINE") != "1":
        cli.images.pull(env["MARIADB_IMAGE"])
    cli.containers.run(
        env["MARIADB_IMAGE"],
        name=f"{project}-db",
        detach=True,
        restart_policy={"Name": "unless-stopped"},
        environment={
            "MARIADB_ROOT_PASSWORD": env["MARIADB_ROOT_PASSWORD"],
            "MARIADB_DATABASE": env["GLPI_DB_NAME"],
            "MARIADB_USER": env["GLPI_DB_USER"],
            "MARIADB_PASSWORD": env["GLPI_DB_PASSWORD"],
            "GLPI_DB_NAME": env["GLPI_DB_NAME"],
            "GLPI_DB_USER": env["GLPI_DB_USER"],
            "GLPI_DB_PASSWORD": env["GLPI_DB_PASSWORD"],
            "TZ": env["TZ"],
        },
        volumes={str(project_dir(project) / "db"): {"bind": "/var/lib/mysql", "mode": "rw"}},
        network=f"{project}-network",
    )
    return f"Created database container: {project}-db"


def create_glpi_container(project, env, force_recreate=True, pull_image=True):
    env = normalize_env_defaults(env)
    cli = docker_client()
    host_port = validate_port(env["GLPI_HTTP_PORT"], "GLPI_HTTP_PORT")

    existing = get_container(project)
    if existing and not force_recreate:
        current_ports = host_ports_for_container(project)
        if current_ports and host_port not in current_ports:
            raise RuntimeError(
                f"GLPI container {project} already uses port {current_ports[0]}, but .env requests port {host_port}. "
                "Select Recreate existing GLPI container or use Change port."
            )
        try:
            existing.reload()
            if existing.status != "running":
                existing.start()
                return f"Existing GLPI container was started: {project}"
        except Exception:
            pass
        return f"The existing GLPI container is being reused: {project}"

    assert_docker_port_free(host_port, exclude_containers={project})

    if force_recreate:
        remove_container(project)

    if pull_image:
        cli.images.pull(env["GLPI_IMAGE"])

    glpi_env = {
        "GLPI_DB_HOST": f"{project}-db",
        "GLPI_DB_PORT": "3306",
        "GLPI_DB_NAME": env["GLPI_DB_NAME"],
        "GLPI_DB_USER": env["GLPI_DB_USER"],
        "GLPI_DB_PASSWORD": env["GLPI_DB_PASSWORD"],
        "GLPI_SKIP_AUTOINSTALL": "true",
        "GLPI_SKIP_AUTOUPDATE": "true",
        "GLPI_CRONTAB_ENABLED": "0" if env.get("BUILDER_QUARANTINE") == "1" else "1",
        "GLPI_SESSION_COOKIE_SAMESITE": env["GLPI_SESSION_COOKIE_SAMESITE"],
        "GLPI_SESSION_COOKIE_SECURE": env["GLPI_SESSION_COOKIE_SECURE"],
        "TZ": env["TZ"],
        "TIMEZONE": env["TZ"],
    }

    cli.containers.run(
        env["GLPI_IMAGE"],
        name=project,
        detach=True,
        restart_policy={"Name": "unless-stopped"},
        user="0:0",
        entrypoint=["/bin/sh", "-c", GLPI_ENTRY_COMMAND],
        environment=glpi_env,
        volumes={
            str(project_dir(project) / "glpi"): {"bind": "/var/glpi", "mode": "rw"},
            str(project_dir(project) / "plugins"): {"bind": "/var/www/glpi/plugins", "mode": "rw"},
        },
        ports={"8080/tcp": host_port},
        network=f"{project}-network",
    )
    return f"Created GLPI container using the custom v7 YAML template: {project} ({host_port}:8080)"


def install_fresh_glpi(project, env):
    command = r'''exec php bin/console database:install \
  --db-host="$GLPI_DB_HOST" \
  --db-port="$GLPI_DB_PORT" \
  --db-name="$GLPI_DB_NAME" \
  --db-user="$GLPI_DB_USER" \
  --db-password="$GLPI_DB_PASSWORD" \
  --no-interaction --quiet'''
    try:
        output = docker_client().containers.run(
            env["GLPI_IMAGE"],
            command=["-lc", command],
            entrypoint="/bin/sh",
            working_dir="/var/www/glpi",
            user="33:33",
            remove=True,
            detach=False,
            environment={
                "GLPI_DB_HOST": f"{project}-db",
                "GLPI_DB_PORT": "3306",
                "GLPI_DB_NAME": env["GLPI_DB_NAME"],
                "GLPI_DB_USER": env["GLPI_DB_USER"],
                "GLPI_DB_PASSWORD": env["GLPI_DB_PASSWORD"],
            },
            volumes={
                str(project_dir(project) / "glpi"): {"bind": "/var/glpi", "mode": "rw"},
                str(project_dir(project) / "plugins"): {"bind": "/var/www/glpi/plugins", "mode": "rw"},
            },
            network=f"{project}-network",
            stdout=True,
            stderr=True,
        )
        config_file = project_dir(project) / "glpi" / "config" / "config_db.php"
        for _ in range(40):
            if config_file.is_file():
                break
            time.sleep(0.25)
        if not config_file.is_file():
            raise RuntimeError("Fresh installation completed without creating config_db.php.")
        text = output.decode("utf-8", errors="replace") if isinstance(output, (bytes, bytearray)) else str(output or "")
        return "Fresh GLPI database installation completed as www-data and config_db.php was verified." + ("\n" + text if text else "")
    except ContainerError as exc:
        stderr = getattr(exc, "stderr", b"") or b""
        stdout = getattr(exc, "stdout", b"") or b""
        details = (stdout + stderr).decode("utf-8", errors="replace") if isinstance(stdout, bytes) and isinstance(stderr, bytes) else str(exc)
        raise RuntimeError("Fresh GLPI database installation failed:\n" + tail_text(details, 5000))


def create_or_restore(
    project,
    env,
    clean_db,
    force_recreate,
    db_backup,
    file_backup,
    progress=None,
    fresh_install=False,
    skip_plugins=False,
    isolated_restore=False,
):
    messages = []
    report = progress or (lambda _percent, _stage, _message=None: None)

    report(18, "Preparing project", "Creating and checking the project folders.")
    ensure_dirs(project)
    if fresh_install:
        report(22, "Clearing GLPI data", "Removing existing GLPI config, files and plugins.")
        messages.append(prepare_fresh_install(project))
        messages.append(fix_permissions(project))
    report(26, "Preparing network", "Checking the isolated Docker network.")
    ensure_network(project, internal=isolated_restore)
    messages.append(f"Checked network: {project}-network")

    report(36, "Database container", "Checking or rebuilding the database container.")
    messages.append(create_db_container(project, env, clean_db))

    report(45, "Waiting for MariaDB", "Waiting until MariaDB accepts connections.")
    ensure_container_network(project, f"{project}-db", internal=isolated_restore)
    ok, msg = wait_db(project)
    messages.append(msg)
    if not ok:
        raise RuntimeError(msg)
    messages.append(finalize_db_directory_permissions(project))

    if db_backup:
        report(57, "Restoring database", "Importing the selected database backup. This can take several minutes.")
        ok, out = restore_database(project, db_backup)
        if not ok:
            raise RuntimeError("Database restore failed:\n" + tail_text(out, 5000))
        messages.append("Database backup restored successfully.")
        messages.append(out)
    else:
        report(57, "Preparing empty database", "Preparing database credentials for the fresh GLPI installation.")
        if fresh_install:
            messages.append("MariaDB created the empty GLPI database and application user from the locked environment settings.")
        else:
            ok, out = reset_db_user(project)
            messages.append(out)
            if not ok:
                raise RuntimeError(out)

    if file_backup:
        report(72, "Restoring GLPI files", "Extracting and copying the selected GLPI files backup.")
        messages.extend(restore_glpi_files(project, file_backup, restore_plugins=not skip_plugins))
    else:
        report(72, "Using image defaults", "No files backup is used; GLPI will create only its standard config and data.")
        messages.append("Fresh installation uses only the original GLPI image files and empty persistent directories.")

    if fresh_install:
        report(78, "Installing empty GLPI database", "Running the GLPI database installer once as www-data.")
        messages.append(install_fresh_glpi(project, env))

    report(84, "Applying permissions", "Applying the required permissions to persistent GLPI data.")
    ensure_glpi_writable_dirs(project)
    messages.append(fix_permissions(project))
    report(92, "Applying GLPI container", "Creating or updating the GLPI application container.")
    messages.append(create_glpi_container(
        project, env, force_recreate=force_recreate,
        pull_image=not isolated_restore,
    ))
    if fresh_install:
        messages.append("Initial GLPI credentials are glpi / glpi. Change this password immediately after signing in.")
    return messages


def verify_glpi_isolated_restore(project, data):
    network_name = f"{project}-network"
    network = docker_client().networks.get(network_name)
    network.reload()
    if not bool(network.attrs.get("Internal")):
        raise RuntimeError("GLPI isolated restore proof failed: Docker network is not internal.")
    for container_name in (project, f"{project}-db"):
        container = get_container(container_name)
        if not container:
            raise RuntimeError(f"GLPI isolated restore proof failed: missing container {container_name}.")
        container.reload()
        networks = set((container.attrs.get("NetworkSettings", {}).get("Networks", {}) or {}).keys())
        if networks != {network_name}:
            raise RuntimeError(f"GLPI isolated restore proof failed: {container_name} has unexpected networks.")
        if container.status != "running":
            raise RuntimeError(f"GLPI isolated restore proof failed: {container_name} is not running.")
    config_file = project_dir(project) / "glpi" / "config" / "config_db.php"
    if not config_file.is_file() or f"{project}-db" not in config_file.read_text(encoding="utf-8", errors="replace"):
        raise RuntimeError("GLPI isolated restore proof failed: config_db.php does not target the isolated database.")
    count = get_container(f"{project}-db").exec_run(
        ["sh", "-c", 'mariadb -N -uroot -p"$MARIADB_ROOT_PASSWORD" "$GLPI_DB_NAME" -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE()"']
    )
    if count.exit_code != 0:
        raise RuntimeError("GLPI isolated restore proof failed: database table check could not be completed.")
    try:
        output = count.output.decode("utf-8", "replace") if isinstance(count.output, bytes) else str(count.output)
        table_count = int(output.strip())
    except ValueError as exc:
        raise RuntimeError("GLPI isolated restore proof failed: invalid database table count.") from exc
    if table_count <= 0:
        raise RuntimeError("GLPI isolated restore proof failed: restored database has no tables.")
    report = {
        "schema": 1,
        "application": "glpi",
        "mode": "isolated-test-restore",
        "network_internal": True,
        "containers": [project, f"{project}-db"],
        "restored_tables": table_count,
        "source_application_version": (data.get("backup_inspection") or {}).get("application_version", "Unknown"),
        "source_database_version": (data.get("backup_inspection") or {}).get("database_version", "Unknown"),
        "target_application_image": data["glpi_image"],
        "target_database_image": data["mariadb_image"],
        "database_sha256": (data.get("backup_inspection") or {}).get("database_sha256", ""),
        "files_sha256": (data.get("backup_inspection") or {}).get("files_sha256", ""),
    }
    atomic_write_text(project_dir(project) / ".builder-quarantine-report.json", json.dumps(report, indent=2) + "\n", 0o600)
    return f"Isolated restore verified: internal network, dedicated containers, patched config and {table_count} restored tables."


def run_create_job(job_token, data):
    project = data["project"]
    messages = []
    log_name = ""
    try:
        update_progress_job(job_token, 5, "Starting", "Background restore started.", status="running")
        ensure_dirs(project)
        update_progress_job(job_token, 10, "Writing configuration", "Writing .env and the locked compose configuration.")
        env = build_env(
            project,
            data["glpi_image"],
            data["mariadb_image"],
            data["host_port"],
            data["container_port"],
            data["tz"],
            data["clean_db"],
            cookie_samesite=data["cookie_samesite"],
            cookie_secure=data["cookie_secure"],
            isolated_restore=bool(data.get("isolated_restore")),
        )
        validate_db_identifier(env["GLPI_DB_NAME"])
        write_env(project, env)
        write_compose(project, env)

        def report(percent, stage, message=None):
            update_progress_job(job_token, percent, stage, message)

        messages = create_or_restore(
            project,
            env,
            data["clean_db"],
            data["force_recreate"],
            data["db_backup"],
            data["file_backup"],
            progress=report,
            fresh_install=data["fresh_install"],
            skip_plugins=data["skip_plugins"],
            isolated_restore=bool(data.get("isolated_restore")),
        )
        if data.get("isolated_restore"):
            report(95, "Verifying isolation", "Checking network, containers, GLPI config and restored database tables.")
            messages.append(verify_glpi_isolated_restore(project, data))
        if data.get("update_backup_source"):
            report(96, "Updating scheduled backups", "Writing the backup script and backup.env for this project.")
            messages.extend(configure_scheduled_backup(project, env))
        messages.append(f"Project folder: {project_dir(project)}")
        messages.append(f"GLPI port: {data['host_port']}:8080")
        messages.append(f"Cookie SameSite: {env['GLPI_SESSION_COOKIE_SAMESITE']}")
        messages.append(f"Cookie Secure: {env['GLPI_SESSION_COOKIE_SECURE']}")
        update_progress_job(job_token, 98, "Saving action log", "Saving the complete action log.")
        action_name = "fresh-install" if data["fresh_install"] else ("isolated-restore" if data.get("isolated_restore") else "full-restore")
        log_name = write_action_log(project, action_name, messages)
        completion_message = (
            "Fresh installation completed. Sign in with glpi / glpi and change the password immediately."
            if data["fresh_install"]
            else ("The isolated GLPI compatibility test restore completed successfully." if data.get("isolated_restore") else "The full GLPI restore completed successfully.")
        )
        update_progress_job(
            job_token,
            100,
            "Completed",
            completion_message,
            status="completed",
            log_name=log_name,
        )
    except Exception as exc:
        error_text = str(exc)
        try:
            action_name = "fresh-install-error" if data.get("fresh_install") else ("isolated-restore-error" if data.get("isolated_restore") else "full-restore-error")
            log_name = write_action_log(project, action_name, ["ERROR", error_text] + messages)
        except Exception:
            log_name = ""
        update_progress_job(
            job_token,
            stage="Failed",
            message="The restore stopped safely. Review the error below and the action log.",
            status="failed",
            log_name=log_name,
            error=error_text,
        )
    finally:
        invalidate_dashboard_cache()
        MUTATION_LOCK.release()


def change_project_port(project, host_port):
    env = read_env(project)
    if not env:
        raise ValueError(f"No .env file was found for {project}. Create or restore the project first.")

    env = normalize_env_defaults(env)
    host_port = validate_port(host_port, "New host port")
    assert_docker_port_free(host_port, exclude_containers={project})

    old_env = dict(env)
    old_port = old_env.get("GLPI_HTTP_PORT", "unknown")

    env["GLPI_HTTP_PORT"] = str(host_port)
    env["GLPI_CONTAINER_PORT"] = "8080"

    ensure_dirs(project)
    ensure_network(project)

    try:
        docker_client().containers.get(f"{project}-db")
    except NotFound:
        raise ValueError(f"Database container {project}-db does not exist. The GLPI container cannot be recreated safely.")

    ensure_container_network(project, f"{project}-db")

    try:
        write_env(project, env)
        write_compose(project, env)
        message = create_glpi_container(project, env, force_recreate=True, pull_image=False)
        return [
            f"Changed port from {old_port} to {host_port}.",
            message,
            f"New mapping: {host_port}:8080",
        ]
    except Exception as exc:
        rollback_error = None
        try:
            write_env(project, old_env)
            write_compose(project, old_env)
            create_glpi_container(project, old_env, force_recreate=True, pull_image=False)
        except Exception as rollback_exc:
            rollback_error = rollback_exc

        if rollback_error:
            raise RuntimeError(
                f"Port change failed: {exc}. Rollback to the old port {old_port} also failed: {rollback_error}"
            )
        raise RuntimeError(
            f"Port change failed: {exc}. The old port {old_port} was restored and the GLPI container was recreated."
        )


def change_cookie_settings(project, cookie_samesite, cookie_secure=None):
    env = read_env(project)
    if not env:
        raise ValueError(f"No .env file was found for {project}. Create or restore the project first.")
    env = normalize_env_defaults(env)
    new_value = validate_cookie_samesite(cookie_samesite)
    new_secure = validate_cookie_secure(cookie_secure or env.get("GLPI_SESSION_COOKIE_SECURE"))
    old_env = dict(env)
    old_value = old_env.get("GLPI_SESSION_COOKIE_SAMESITE", DEFAULT_SESSION_COOKIE_SAMESITE)
    old_secure = old_env.get("GLPI_SESSION_COOKIE_SECURE", DEFAULT_SESSION_COOKIE_SECURE)

    env["GLPI_SESSION_COOKIE_SAMESITE"] = new_value
    env["GLPI_SESSION_COOKIE_SECURE"] = new_secure
    env["GLPI_CONTAINER_PORT"] = "8080"
    env = normalize_env_defaults(env)
    new_secure = env["GLPI_SESSION_COOKIE_SECURE"]

    ensure_dirs(project)
    ensure_network(project)

    try:
        docker_client().containers.get(f"{project}-db")
    except NotFound:
        raise ValueError(f"Database container {project}-db does not exist. The GLPI container cannot be recreated safely.")

    ensure_container_network(project, f"{project}-db")

    try:
        write_env(project, env)
        write_compose(project, env)
        message = create_glpi_container(project, env, force_recreate=True, pull_image=False)
        return [
            f"Changed Cookie SameSite from {old_value} to {new_value}.",
            f"Changed Cookie Secure from {old_secure} to {new_secure}.",
            "The PHP cookie override is generated from .env when the container starts.",
            message,
            "The database, database files, GLPI files and plugins were not removed.",
        ]
    except Exception as exc:
        rollback_error = None
        try:
            write_env(project, old_env)
            write_compose(project, old_env)
            create_glpi_container(project, old_env, force_recreate=True, pull_image=False)
        except Exception as rollback_exc:
            rollback_error = rollback_exc

        if rollback_error:
            raise RuntimeError(
                f"Changing the cookie settings failed: {exc}. Rollback to {old_value}/{old_secure} also failed: {rollback_error}"
            )
        raise RuntimeError(
            f"Changing the cookie settings failed: {exc}. The old setting {old_value}/{old_secure} was restored and the GLPI container was recreated."
        )



def rebuild_glpi(project):
    env = read_env(project)
    if not env:
        raise ValueError(f"No .env file was found for {project}.")
    env = normalize_env_defaults(env)
    host_port = validate_port(env.get("GLPI_HTTP_PORT"), "GLPI_HTTP_PORT")
    assert_docker_port_free(host_port, exclude_containers={project})
    ensure_dirs(project)
    ensure_network(project)
    try:
        docker_client().containers.get(f"{project}-db")
    except NotFound:
        raise ValueError(f"Database container {project}-db does not exist. Restore the project or start the database container first.")
    ensure_container_network(project, f"{project}-db")
    write_env(project, env)
    write_compose(project, env)
    message = create_glpi_container(project, env, force_recreate=True, pull_image=False)
    return [
        f"Recreated the GLPI container with existing port {host_port}.",
        f"Applied cookie settings: SameSite={env['GLPI_SESSION_COOKIE_SAMESITE']}, Secure={env['GLPI_SESSION_COOKIE_SECURE']}. The PHP override is generated when the container starts.",
        message,
        "The database, database files, GLPI files and plugins were not removed.",
    ]


def test_db(project):
    env = read_env(project)
    db = docker_client().containers.get(f"{project}-db")
    result = db.exec_run(
        'sh -lc \'mariadb -u"$GLPI_DB_USER" -p"$GLPI_DB_PASSWORD" "$GLPI_DB_NAME" -e "SELECT DATABASE(); SHOW TABLES;" | head -n 20\'',
        demux=True,
        environment={
            "GLPI_DB_NAME": env.get("GLPI_DB_NAME", "glpi"),
            "GLPI_DB_USER": env.get("GLPI_DB_USER", "glpiuser"),
            "GLPI_DB_PASSWORD": env.get("GLPI_DB_PASSWORD", ""),
        },
    )
    stdout, stderr = result.output if isinstance(result.output, tuple) else (result.output, b"")
    output = ((stdout or b"") + (stderr or b"")).decode("utf-8", errors="replace")
    return result.exit_code == 0, output


def mask(value):
    if not value:
        return ""
    value = str(value)
    return value[:3] + "*" * max(4, len(value) - 7) + value[-4:]


def container_env(name):
    c = get_container(name)
    if not c:
        return [f"{name}: container does not exist"]
    output = []
    for line in c.attrs.get("Config", {}).get("Env", []) or []:
        key, _, value = line.partition("=")
        if key.startswith(("GLPI_DB_", "MARIADB_", "DB_", "MYSQL_")) or key in ("TZ", "TIMEZONE", "GLPI_SKIP_AUTOINSTALL", "GLPI_SKIP_AUTOUPDATE", "GLPI_CRONTAB_ENABLED", "GLPI_SESSION_COOKIE_SAMESITE", "GLPI_SESSION_COOKIE_SECURE"):
            if "PASSWORD" in key:
                value = mask(value)
            output.append(f"{key}={value}")
    return output or ["No relevant environment entries found."]


def php_cookie_runtime_status(project):
    """Read effective PHP cookie settings in the running GLPI container.

    This is diagnostic only and does not attempt to change GLPI. An inactive
    container simply returns a textual status message.
    """
    c = get_container(project)
    if not c:
        return "GLPI container does not exist"
    try:
        c.reload()
        if c.status != "running":
            return f"GLPI container is not running: {c.status}"
    except Exception as exc:
        return f"Reading the container status failed: {exc}"

    php_code = (
        'echo "session.cookie_samesite=" . ini_get("session.cookie_samesite") . PHP_EOL;'
        'echo "session.cookie_httponly=" . ini_get("session.cookie_httponly") . PHP_EOL;'
        'echo "session.cookie_secure=" . ini_get("session.cookie_secure") . PHP_EOL;'
    )
    try:
        result = c.exec_run(["php", "-r", php_code], demux=True)
        out = b""
        err = b""
        if isinstance(result.output, tuple):
            out = result.output[0] or b""
            err = result.output[1] or b""
        else:
            out = result.output or b""
        text = (out + err).decode("utf-8", errors="replace").strip()
        if not text:
            text = "no output"
        return text
    except Exception as exc:
        return f"Runtime PHP cookie check failed: {exc}"


def container_status(name):
    c = get_container(name)
    if not c:
        return "not present"
    try:
        c.reload()
    except Exception:
        pass
    return getattr(c, "status", "unknown")


def discover_projects(containers=None):
    names = set()
    if BASE_PATH.exists() and BASE_PATH.is_dir():
        try:
            for child in BASE_PATH.iterdir():
                if is_managed_glpi_project(child.name):
                    names.add(child.name)
        except Exception:
            pass

    if containers is None:
        try:
            containers = docker_client().containers.list(all=True)
        except Exception:
            containers = []
    container_by_name = {container.name: container for container in containers}

    try:
        for c in containers:
            name = c.name
            if is_managed_glpi_project(name):
                names.add(name)
            elif name.endswith("-db"):
                base = name[:-3]
                if is_managed_glpi_project(base):
                    names.add(base)
    except Exception:
        pass

    backup_sources = set(scheduled_backup_projects())
    projects = []
    for name in sorted(names):
        env = read_env(name)
        glpi_container = container_by_name.get(name)
        db_container = container_by_name.get(f"{name}-db")
        glpi_status = getattr(glpi_container, "status", "not present") if glpi_container else "not present"
        db_status = getattr(db_container, "status", "not present") if db_container else "not present"
        snapshot_mappings = container_port_mappings_from_snapshot(glpi_container) if glpi_container else []
        active_ports = []
        for mapping in snapshot_mappings:
            if mapping["private"] == "8080/tcp":
                try:
                    active_ports.append(int(mapping["host_port"]))
                except Exception:
                    pass
        env_port = env.get("GLPI_HTTP_PORT", "")
        shown_port = str(active_ports[0]) if active_ports else env_port
        mappings = [mapping["mapping"] for mapping in snapshot_mappings]
        backup_state = scheduled_backup_status(name) if name in backup_sources else {
            "selected": False,
            "ready": False,
            "issues": [],
            "latest": None,
            "schedule": backup_schedule_status(name),
        }
        projects.append({
            "name": name,
            "env_port": env_port,
            "active_port": shown_port,
            "glpi_status": glpi_status,
            "db_status": db_status,
            "glpi_image": env.get("GLPI_IMAGE", ""),
            "mariadb_image": env.get("MARIADB_IMAGE", ""),
            "tz": env.get("TZ", ""),
            "cookie_samesite": cookie_samesite_for_display(env),
            "cookie_secure": cookie_secure_for_display(env),
            "path": str(project_dir(name)),
            "mappings": ", ".join(mappings) if mappings else "-",
            "latest_log": latest_log(name),
            "logs": list_logs(name, limit=5),
            "backup_source": name in backup_sources,
            "backup_status": backup_state,
        })
    return projects


def discover_unmanaged_glpi_projects(containers=None, managed_projects=None):
    """Find running/stopped GLPI Compose projects without granting management.

    Docker Compose labels and allowed GLPI/database images are used as positive
    evidence.  The result is display-only so a detected production stack cannot
    be mutated through routes that require the Builder contract.
    """
    if containers is None:
        try:
            containers = docker_client().containers.list(all=True)
        except Exception:
            containers = []
    managed_names = {
        item["name"] if isinstance(item, dict) else str(item)
        for item in (managed_projects or [])
    }
    groups = {}
    for container in containers:
        references = container_image_references(container)
        is_database = any(reference.startswith(prefix) for reference in references for prefix in ALLOWED_DB_IMAGES)
        is_glpi = not is_database and container_looks_like_glpi(container)
        if not (is_glpi or is_database):
            continue
        project = inferred_compose_project(container, is_database=is_database)
        if not PROJECT_RE.fullmatch(project) or project in managed_names:
            continue
        group = groups.setdefault(project, {"glpi": None, "database": None})
        if is_glpi:
            group["glpi"] = container
        elif is_database:
            group["database"] = container

    detected = []
    for name, group in sorted(groups.items()):
        # A database alone is too weak a signal and could be an unrelated stack.
        if group["glpi"] is None:
            continue
        glpi_container = group["glpi"]
        db_container = group["database"]
        mappings = container_port_mappings_from_snapshot(glpi_container)
        active_port = next((item["host_port"] for item in mappings), "")
        detected.append({
            "name": name,
            "path": str(project_dir(name)),
            "glpi_status": getattr(glpi_container, "status", "unknown"),
            "db_status": getattr(db_container, "status", "not detected") if db_container else "not detected",
            "glpi_image": container_image_name(glpi_container),
            "mariadb_image": container_image_name(db_container) if db_container else "",
            "active_port": active_port,
            "mappings": ", ".join(item["mapping"] for item in mappings) or "-",
            "issues": managed_project_issues(name),
        })
    return detected


CREATE_PREVIEW_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Execution plan · Docker App Manager</title>
<style>
:root{--bg:#f4f7fb;--card:#fff;--line:#dce4ee;--ink:#172033;--muted:#64748b;--brand:#2364d2;--danger:#b42318}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 10% 0,#eaf1ff 0,transparent 30%),var(--bg);color:var(--ink);font:15px system-ui,-apple-system,"Segoe UI",sans-serif}
header{background:#fff;color:#12233f;padding:18px;border-bottom:1px solid var(--line)}header div,main{max-width:960px;margin:auto}main{padding:30px 18px 50px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:22px;box-shadow:0 10px 32px #16345f0c;margin-bottom:18px}
h1,h2{margin-top:0}.risk{border-left:5px solid var(--brand);padding:12px 14px;background:#edf8f3;border-radius:8px}.risk.danger{border-color:var(--danger);background:#fff0ef}
dl{display:grid;grid-template-columns:minmax(170px,240px) 1fr;gap:0;border-top:1px solid var(--line)}dt,dd{margin:0;padding:10px 0;border-bottom:1px solid var(--line)}dt{font-weight:700;padding-right:15px}dd{overflow-wrap:anywhere}
.actions{display:flex;gap:10px;flex-wrap:wrap;align-items:center}button,.button{border:0;border-radius:8px;padding:11px 15px;background:var(--brand);color:#fff;font-weight:750;text-decoration:none;cursor:pointer}.secondary{background:#425466}
@media(max-width:640px){dl{grid-template-columns:1fr}dt{border-bottom:0;padding-bottom:0}dd{padding-top:4px}}
</style></head><body>
<header><div><h1>Review the execution plan</h1><div>{{ app_version }}</div></div></header>
<main>
{% if demo_only %}<section class="card"><p class="risk"><strong>Simulation only:</strong> confirming this plan adds a temporary demo project to your current preview session. Docker, files and backups are not changed.</p></section>{% endif %}
<section class="card"><h2>{{ plan.title }}</h2><p class="risk {{ 'danger' if plan.destructive else '' }}"><strong>Risk:</strong> {{ plan.risk }}</p>
<dl>{% for label,value in plan.rows %}<dt>{{ label }}</dt><dd>{{ value }}</dd>{% endfor %}</dl></section>
<section class="card"><div class="actions">{% if preview_only %}<strong>Preview only</strong>{% else %}<form method="post" action="{{ url_for('execute_create') }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><input type="hidden" name="preview_token" value="{{ preview_token }}"><button type="submit">{{ 'Add simulated project' if demo_only else 'Confirm plan and start' }}</button></form>{% endif %}<a class="button secondary" href="{{ url_for('new_project_page') }}">{{ 'Back' if preview_only else 'Back and edit' }}</a></div></section>
</main></body></html>"""


PROGRESS_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{% if job.status in ['queued', 'running'] %}<meta http-equiv="refresh" content="2">{% endif %}
<title>{{ job.title }} progress · {{ job.project }} · Docker App Manager</title>
<style>
:root{--bg:#f4f7fb;--card:#fff;--line:#dce4ee;--ink:#172033;--muted:#64748b;--brand:#2364d2;--danger:#b42318;--wait:#a15c00}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 10% 0,#eaf1ff 0,transparent 30%),var(--bg);color:var(--ink);font:15px system-ui,-apple-system,"Segoe UI",sans-serif}
header{background:#fff;color:#12233f;padding:18px;border-bottom:1px solid var(--line)}header div,main{max-width:960px;margin:auto}main{padding:30px 18px 50px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:22px;box-shadow:0 10px 32px #16345f0c;margin-bottom:18px}
h1,h2{margin-top:0}.meta{color:var(--muted)}.status{display:inline-block;border-radius:999px;padding:5px 10px;background:#edf8f3;color:#16643d;font-weight:750}.status.failed{background:#fff0ef;color:var(--danger)}.status.running,.status.queued{background:#fff7e6;color:var(--wait)}
progress{display:block;width:100%;height:24px;margin:16px 0;accent-color:var(--brand)}.percent{font-size:28px;font-weight:800}.timeline{list-style:none;padding:0;margin:0}.timeline li{padding:10px 0 10px 20px;border-left:3px solid #a9d8c3;position:relative}.timeline li:before{content:"";position:absolute;left:-7px;top:16px;width:11px;height:11px;border-radius:50%;background:var(--brand)}
.error{white-space:pre-wrap;background:#fff0ef;border:1px solid #f5b1ab;padding:12px;border-radius:8px;color:#7a271a}.actions{display:flex;gap:10px;flex-wrap:wrap}.button{display:inline-block;border-radius:8px;padding:10px 14px;background:var(--brand);color:#fff;font-weight:700;text-decoration:none}.secondary{background:#425466}
</style></head><body>
<header><div><h1>{{ job.title }} progress</h1><div>{{ app_version }}</div></div></header>
<main>
<section class="card"><span class="status {{ job.status }}">{{ job.status|capitalize }}</span><h2 style="margin-top:14px">{{ job.stage }}</h2><div class="percent">{{ job.percent }}%</div><progress max="100" value="{{ job.percent }}">{{ job.percent }}%</progress><p class="meta">Elapsed time: {{ elapsed }} seconds{% if job.status in ['queued','running'] %} · this page refreshes automatically{% endif %}</p></section>
{% if job.error %}<section class="card"><h2>Error</h2><div class="error">{{ job.error }}</div></section>{% endif %}
<section class="card"><h2>Activity</h2><ol class="timeline">{% for message in job.messages %}<li>{{ message }}</li>{% endfor %}</ol></section>
<section class="card"><div class="actions"><a class="button secondary" href="{{ url_for('index') }}#projects">Dashboard</a>{% if job.log_name %}<a class="button" href="{{ url_for('view_log', project=job.project, filename=job.log_name) }}">Open full log</a>{% endif %}</div></section>
</main></body></html>"""



@app.route("/create", methods=["POST"])
def create():
    backup_root = request.form.get("backup_root", BACKUP_ROOT)
    project_for_log = None
    try:
        require_csrf()
        data = (
            validate_demo_create_request(request.form)
            if test_demo_is_active()
            else validate_create_request(request.form)
        )
        project_for_log = data["project"]
        preview_token = store_create_preview(data)
        plan = build_create_plan(data)
        if test_demo_is_active():
            plan["risk"] = "None - simulation only; Docker and host storage remain unchanged"
            plan["destructive"] = False
            plan["steps"] = [
                "Validate the demo form and simulated inputs",
                "Add the project to this browser preview session",
                "Show project, backup, activity and YAML screens using demo data",
            ]
        return render_template_string(
            CREATE_PREVIEW_HTML,
            plan=plan,
            preview_token=preview_token,
            preview_only=False,
            demo_only=test_demo_is_active(),
        )
    except Exception as exc:
        flash_error(str(exc), project_for_log, "create-preview-error")
    return redirect(url_for("new_project_page"))


@app.route("/create/execute", methods=["POST"])
def execute_create():
    backup_root = BACKUP_ROOT
    project_for_log = None
    try:
        require_csrf()
        stored_data = consume_create_preview(request.form.get("preview_token"))
        backup_root = stored_data.get("backup_root") or BACKUP_ROOT
        if test_demo_is_active():
            data = validate_demo_create_request(stored_data)
            demo_projects = list(session.get("test_demo_projects", []))
            demo_projects.append({
                "name": data["project"],
                "port": data["host_port"],
                "glpi_image": data["glpi_image"],
                "mariadb_image": data["mariadb_image"],
                "backup_source": data["update_backup_source"],
            })
            session["test_demo_projects"] = demo_projects[-8:]
            flash(
                f"Simulated project {esc(data['project'])} was added to this local preview. "
                "No Docker container or host file was created.",
                "ok",
            )
            return redirect(url_for("project_detail_page", project=data["project"]))
        data = validate_create_request(stored_data)
        project = data["project"]
        project_for_log = project
        job_token = create_progress_job(project, backup_root)
        worker = threading.Thread(
            target=run_create_job,
            args=(job_token, data),
            name=f"glpi-restore-{project}",
            daemon=True,
        )
        g.mutation_lock_held = False
        try:
            worker.start()
        except Exception:
            g.mutation_lock_held = True
            raise
        return redirect(url_for("restore_progress", job_token=job_token))
    except Exception as exc:
        flash_error(str(exc), project_for_log, "create-restore-error")
    return redirect(url_for("new_project_page"))


@app.route("/progress/<job_token>", methods=["GET"])
def restore_progress(job_token):
    job = progress_job_snapshot(job_token)
    if not job:
        flash("This restore progress link is invalid or has expired.", "err")
        return redirect(url_for("index"))
    elapsed = max(0, (job["finished_at"] or int(time.time())) - job["created_at"])
    return render_template_string(PROGRESS_HTML, job=job, elapsed=elapsed)


@app.route("/change-port", methods=["POST"])
def change_port_route():
    project_for_log = None
    try:
        require_csrf()
        project = validate_project(request.form.get("project"))
        project_for_log = project
        host_port = validate_port(request.form.get("host_port"), "New host port")
        messages = change_project_port(project, host_port)
        flash_action_success(project, "change-port", messages)
    except Exception as exc:
        flash_error(str(exc), project_for_log, "change-port-error")
    return redirect(url_for("project_detail_page", project=project_for_log)) if project_for_log else redirect(url_for("projects_page"))


@app.route("/change-cookie", methods=["POST"])
def change_cookie_route():
    project_for_log = None
    try:
        require_csrf()
        project = validate_project(request.form.get("project"))
        project_for_log = project
        cookie_samesite = validate_cookie_samesite(request.form.get("cookie_samesite"))
        cookie_secure = validate_cookie_secure(request.form.get("cookie_secure"))
        messages = change_cookie_settings(project, cookie_samesite, cookie_secure)
        flash_action_success(project, "change-cookie-settings", messages)
    except Exception as exc:
        flash_error(str(exc), project_for_log, "change-cookie-settings-error")
    return redirect(url_for("project_detail_page", project=project_for_log)) if project_for_log else redirect(url_for("projects_page"))


@app.route("/set-backup-source", methods=["POST"])
def set_backup_source_route():
    project_for_log = None
    try:
        require_csrf()
        project = validate_project(request.form.get("project"))
        project_for_log = project
        schedule = validate_backup_schedule(
            request.form.get("schedule_kind", "daily"),
            request.form.get("schedule_time", "02:00"),
            request.form.get("schedule_weekdays", "7"),
            request.form.get("interval_hours", "24"),
            request.form.get("retention_days", "60"),
        )
        enabled = request.form.get("schedule_enabled", "yes") == "yes"
        if test_demo_is_active():
            overrides = dict(session.get("test_demo_schedule_overrides", {}))
            overrides[project] = {
                "enabled": enabled,
                "kind": schedule["kind"],
                "time": schedule["time"],
                "weekdays": schedule["weekdays"],
                "interval_hours": int(schedule["interval_hours"]),
                "retention_days": int(schedule["retention_days"]),
                "next_run": "Simulated after saving",
                "last_status": "Not run",
                "last_attempt": "",
                "last_success": "",
                "dispatcher_healthy": True,
            }
            session["test_demo_schedule_overrides"] = overrides
            flash("Simulated backup schedule saved for this preview session.", "ok")
            return redirect(url_for("project_detail_page", project=project))
        messages = configure_scheduled_backup(
            project,
            enabled=enabled,
            kind=schedule["kind"],
            schedule_time=schedule["time"],
            weekdays=schedule["weekdays"],
            interval_hours=schedule["interval_hours"],
            retention_days=schedule["retention_days"],
        )
        flash_action_success(project, "set-backup-source", messages)
    except Exception as exc:
        flash_error(str(exc), project_for_log, "set-backup-source-error")
    return redirect(url_for("project_detail_page", project=project_for_log)) if project_for_log else redirect(url_for("projects_page"))


@app.route("/run-backup", methods=["POST"])
def run_backup_now_route():
    project_for_log = None
    try:
        require_csrf()
        project = validate_project(request.form.get("project"))
        project_for_log = project
        status = scheduled_backup_status(project)
        if not status["ready"]:
            raise ValueError("Scheduled backup is not ready: " + "; ".join(status["issues"]))
        job_token = create_progress_job(project, BACKUP_ROOT, kind="backup")
        worker = threading.Thread(
            target=run_backup_job,
            args=(job_token, project),
            name=f"glpi-backup-{project}",
            daemon=True,
        )
        g.mutation_lock_held = False
        try:
            worker.start()
        except Exception:
            g.mutation_lock_held = True
            raise
        return redirect(url_for("restore_progress", job_token=job_token))
    except Exception as exc:
        flash_error(str(exc), project_for_log, "manual-backup-error")
    return redirect(url_for("project_detail_page", project=project_for_log)) if project_for_log else redirect(url_for("projects_page"))


@app.route("/rebuild-glpi", methods=["POST"])
def rebuild_glpi_route():
    project_for_log = None
    try:
        require_csrf()
        project = validate_project(request.form.get("project"))
        project_for_log = project
        if request.form.get("confirm_rebuild") != project:
            raise ValueError("Type the exact project name to confirm recreating the GLPI container.")
        messages = rebuild_glpi(project)
        flash_action_success(project, "rebuild-glpi", messages)
    except Exception as exc:
        flash_error(str(exc), project_for_log, "rebuild-glpi-error")
    return redirect(url_for("project_detail_page", project=project_for_log)) if project_for_log else redirect(url_for("projects_page"))


@app.route("/diagnose", methods=["POST"])
def diagnose():
    project_for_log = None
    try:
        require_csrf()
        project = validate_project(request.form.get("project"))
        project_for_log = project
        env = read_env(project)
        if not env:
            raise ValueError(f"No .env file was found for {project}")
        lines = []
        for key in ["PROJECT_NAME", "GLPI_IMAGE", "MARIADB_IMAGE", "GLPI_HTTP_PORT", "GLPI_CONTAINER_PORT", "GLPI_SESSION_COOKIE_SAMESITE", "GLPI_SESSION_COOKIE_SECURE", "GLPI_DB_NAME", "GLPI_DB_USER", "GLPI_DB_PASSWORD", "MARIADB_ROOT_PASSWORD", "TZ"]:
            if key in env:
                value = mask(env[key]) if "PASSWORD" in key else env[key]
                lines.append(f"{key}={value}")
        ports = []
        c = get_container(project)
        if c:
            ports = [m["mapping"] for m in container_port_mappings(c)]
        html = "<h3>.env</h3>" + safe_pre("\n".join(lines))
        html += f"<h3>{esc(project)}</h3>" + safe_pre("\n".join(container_env(project)))
        html += "<h3>Port mappings</h3>" + safe_pre("\n".join(ports) if ports else "no active port mapping found")
        html += "<h3>PHP cookie override written at startup</h3>" + safe_pre(php_session_override_text(env) + "\nGenerated from .env when the container starts and copied to PHP conf.d.")
        html += "<h3>Effective PHP cookie settings in the container</h3>" + safe_pre(php_cookie_runtime_status(project))
        html += f"<h3>{esc(project)}-db</h3>" + safe_pre("\n".join(container_env(f"{project}-db")))
        html += "<h3>docker-compose.yml</h3>" + safe_pre(compose_file(project).read_text(encoding="utf-8") if compose_file(project).exists() else "not found")
        flash(html, "msg")
        write_action_log(project, "diagnostics", ["Diagnostics viewed", "\n".join(lines)])
    except Exception as exc:
        flash_error(str(exc), project_for_log, "diagnostics-error")
    return redirect(url_for("project_detail_page", project=project_for_log)) if project_for_log else redirect(url_for("projects_page"))


@app.route("/testdb", methods=["POST"])
def testdb_route():
    project_for_log = None
    try:
        require_csrf()
        project = validate_project(request.form.get("project"))
        project_for_log = project
        ok, out = test_db(project)
        write_action_log(project, "database-test", [out])
        flash(safe_pre(out), "ok" if ok else "err")
    except Exception as exc:
        flash_error(str(exc), project_for_log, "database-test-error")
    return redirect(url_for("project_detail_page", project=project_for_log)) if project_for_log else redirect(url_for("projects_page"))


@app.route("/resetdb", methods=["POST"])
def resetdb_route():
    project_for_log = None
    try:
        require_csrf()
        project = validate_project(request.form.get("project"))
        project_for_log = project
        if request.form.get("confirm_resetdb") != "yes":
            raise ValueError("Select the confirmation box before resetting the database user.")
        if request.form.get("confirm_project") != project:
            raise ValueError("Type the exact project name to confirm database credential recovery.")
        ok, out = reset_db_user(project)
        write_action_log(project, "database-user-reset", [out])
        flash(text_to_html(out), "ok" if ok else "err")
    except Exception as exc:
        flash_error(str(exc), project_for_log, "database-user-reset-error")
    return redirect(url_for("project_detail_page", project=project_for_log)) if project_for_log else redirect(url_for("projects_page"))


@app.route("/logs/<project>/<filename>", methods=["GET"])
def view_log(project, filename):
    try:
        project = validate_project(project)
    except ValueError:
        abort(404)
    if not LOG_FILE_RE.match(filename):
        abort(404)
    path = log_dir(project) / filename
    if path.is_symlink() or not path.is_file() or not path_under_base(path):
        abort(404)
    text = path.read_text(encoding="utf-8", errors="replace")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Log {esc(filename)}</title>
<style>
body {{ font-family: system-ui, -apple-system, Segoe UI, sans-serif; max-width: 1100px; margin: 32px auto; padding: 0 16px; background: #f7f7f7; color: #222; }}
.card {{ background: white; border: 1px solid #ddd; border-radius: 10px; padding: 18px 20px; }}
pre {{ background: #f0f0f0; padding: 12px; border-radius: 6px; overflow: auto; white-space: pre-wrap; }}
a {{ color: #1f6feb; }}
</style>
</head>
<body>
<div class="card">
<p><a href="{url_for('index')}">&larr; Back</a></p>
<h1>Log {esc(filename)}</h1>
<p>Project: <code>{esc(project)}</code></p>
<pre>{esc(text)}</pre>
</div>
</body>
</html>"""


LOGIN_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in · Docker App Manager</title>
<style>
:root{color-scheme:light;--navy:#12233f;--blue:#2364d2;--blue-dark:#184da8;--ink:#172033;--muted:#64748b;--line:#dce3ec;--danger:#b42318}
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(circle at 15% 0,#eaf1ff 0,transparent 36%),#f4f7fb;color:var(--ink);font:15px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}.shell{width:min(960px,calc(100% - 32px));margin:48px auto}.brand{display:flex;align-items:center;gap:12px;margin-bottom:22px;color:var(--navy);font-weight:750;letter-spacing:-.01em}.brand-mark{display:grid;place-items:center;width:38px;height:38px;border-radius:11px;background:linear-gradient(145deg,#2e71df,#174ca7);box-shadow:0 8px 22px #1d5ecb3b;color:#fff;font-size:18px}.brand small{display:block;color:var(--muted);font-size:12px;font-weight:550;letter-spacing:.02em}
.layout{display:grid;grid-template-columns:minmax(0,.9fr) minmax(420px,1.1fr);overflow:hidden;background:#fff;border:1px solid #dbe3ee;border-radius:20px;box-shadow:0 24px 70px #16345f1c}.visual{display:flex;flex-direction:column;justify-content:space-between;min-height:560px;padding:44px;background:linear-gradient(155deg,#12233f,#183862);color:#fff}.eyebrow{display:inline-flex;align-items:center;gap:7px;width:max-content;padding:5px 10px;border:1px solid #ffffff2b;border-radius:999px;background:#ffffff12;color:#d8e6ff;font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.08em}.visual h1{max-width:360px;margin:22px 0 12px;font-size:34px;line-height:1.13;letter-spacing:-.035em}.visual p{margin:0;color:#c9d5e8}.security{display:grid;gap:13px;margin-top:36px}.security-item{display:flex;align-items:center;gap:11px;color:#dbe6f5;font-size:13px}.icon{display:grid;place-items:center;flex:0 0 28px;height:28px;border:1px solid #ffffff31;border-radius:8px;background:#ffffff12;font-size:13px}.visual-foot{padding-top:20px;border-top:1px solid #ffffff1c;color:#aebed5;font-size:12px}
.panel{display:flex;flex-direction:column;justify-content:center;padding:48px}.panel-head{margin-bottom:24px}.panel-head h2{margin:0 0 7px;color:var(--navy);font-size:25px;letter-spacing:-.025em}.panel-head p{margin:0;color:var(--muted);font-size:14px}.error{display:flex;gap:10px;margin:0 0 20px;padding:12px 14px;border:1px solid #f3b9b4;border-radius:10px;background:#fff5f4;color:var(--danger);font-size:14px;font-weight:600}.error:before{content:"!";display:grid;place-items:center;flex:0 0 20px;height:20px;border-radius:50%;background:var(--danger);color:#fff;font-size:12px}
.field{margin-top:16px}label{display:flex;justify-content:space-between;gap:10px;margin-bottom:7px;color:#29364a;font-size:13px;font-weight:700}.hint{color:#8491a4;font-weight:500}input{width:100%;height:44px;padding:0 13px;border:1px solid #cbd5e1;border-radius:9px;background:#fff;color:var(--ink);font:inherit;outline:none;transition:border-color .15s,box-shadow .15s}input:focus{border-color:var(--blue);box-shadow:0 0 0 3px #2364d21c}input::placeholder{color:#a1adbd}button{display:flex;align-items:center;justify-content:center;width:100%;height:46px;margin-top:24px;border:0;border-radius:10px;background:linear-gradient(180deg,var(--blue),var(--blue-dark));box-shadow:0 8px 20px #205fc332;color:#fff;font:700 14px system-ui;cursor:pointer}button:hover{filter:brightness(1.04)}button:focus-visible{outline:3px solid #2364d23d;outline-offset:2px}.footnote{margin:14px 0 0;text-align:center;color:#8491a4;font-size:12px}
.test-preview{margin-top:24px;padding:16px;border:1px solid #cfe0fb;border-radius:12px;background:#f4f8ff}.test-preview strong{display:block;color:#294369}.test-preview p{margin:4px 0 12px;color:#657994;font-size:12px}.preview-actions{display:grid;grid-template-columns:1fr 1fr;gap:9px}.preview-button{display:flex;align-items:center;justify-content:center;height:39px;border:1px solid #aac6ef;border-radius:9px;background:#fff;color:#24569f;font-size:12px;font-weight:750;text-decoration:none}.preview-button:hover{background:#edf5ff;text-decoration:none}.preview-button:focus-visible{outline:3px solid #2364d23d;outline-offset:2px}
@media(max-width:760px){.shell{margin:20px auto}.layout{grid-template-columns:1fr}.visual{min-height:auto;padding:30px}.visual h1{font-size:28px}.security{grid-template-columns:1fr 1fr}.visual-foot{display:none}.panel{padding:34px}}@media(max-width:520px){.shell{width:min(100% - 20px,960px);margin:10px auto}.brand{margin:15px 4px}.layout{border-radius:15px}.visual{padding:25px 22px}.visual h1{font-size:25px}.security{grid-template-columns:1fr}.panel{padding:30px 21px}}
</style></head><body><div class="shell"><div class="brand"><div class="brand-mark">D</div><div>Docker App Manager<small>Synology deployment console</small></div></div>
<main class="layout"><section class="visual"><div><span class="eyebrow">● Administrator access</span><h1>Welcome back.</h1><p>Sign in to manage deployments, restores and backups.</p><div class="security"><div class="security-item"><span class="icon">✓</span><span>Password-protected administration</span></div><div class="security-item"><span class="icon">⌁</span><span>Time-based two-factor authentication</span></div><div class="security-item"><span class="icon">◈</span><span>Short-lived secure sessions</span></div></div></div><div class="visual-foot">Authorized administrators only. Authentication attempts are rate-limited and audited.</div></section>
<section class="panel"><div class="panel-head"><h2>Sign in to Docker App Manager</h2><p>Enter your administrator credentials to continue.</p></div>
{% if error %}<div class="error" role="alert">Unable to sign in. Check the credentials and try again.</div>{% endif %}
<form method="post" action="{{ url_for('login') }}"><input type="hidden" name="csrf_token" value="{{ csrf_token }}"><input type="hidden" name="next" value="{{ next_path }}">
<div class="field"><label for="username">Username</label><input id="username" name="username" autocomplete="username" placeholder="Your administrator username" required autofocus></div>
<div class="field"><label for="password">Password</label><input id="password" type="password" name="password" autocomplete="current-password" placeholder="Your administrator password" required></div>
<div class="field"><label for="totp">Authenticator code <span class="hint">6 digits</span></label><input id="totp" name="totp" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" placeholder="000000" required></div>
<button type="submit">Sign in</button><p class="footnote">Your session automatically expires after inactivity.</p></form>
{% if test_preview_enabled %}<aside class="test-preview"><strong>Local test preview</strong><p>Inspect the interface without credentials. Builder changes remain disabled.</p><div class="preview-actions"><a class="preview-button" href="{{ url_for('test_preview_enter') }}">View Builder</a><a class="preview-button" href="{{ url_for('test_preview_setup') }}">View setup</a></div></aside>{% endif %}
</section></main></div></body></html>"""

AUTH_RECOVERY_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Authentication recovery · Docker App Manager</title>
<style>
body{margin:0;background:#f4f7fb;color:#172033;font:15px/1.55 system-ui,sans-serif}.card{width:min(720px,calc(100% - 32px));margin:64px auto;padding:34px;border:1px solid #dce3ec;border-radius:18px;background:#fff;box-shadow:0 20px 60px #16345f1c}h1{margin:0 0 10px;color:#12233f;font-size:27px}h2{margin:26px 0 8px;font-size:17px}p{color:#526176}.notice{padding:13px 15px;border:1px solid #f0c4bf;border-radius:10px;background:#fff6f5;color:#8f251c;font-weight:650}code{display:block;padding:12px;border:1px solid #d6deea;border-radius:8px;background:#f8fafc;color:#233b5d;overflow-wrap:anywhere;user-select:all}li+li{margin-top:7px}.foot{margin-top:24px;font-size:12px;color:#7b8798}
</style></head><body><main class="card"><h1>Authentication recovery required</h1>
<p class="notice">The existing administrator file could not be loaded. For safety, Docker App Manager did not generate a setup token and did not overwrite your credentials.</p>
<h2>First preserve the existing account</h2><p>Stop the project and correct the permissions of <strong>config</strong> to 700 and <strong>config/builder-auth.json</strong> to 600. Then start the project again.</p>
<h2>Start a completely new setup</h2><ol><li>Stop the <strong>docker-app-manager</strong> project.</li><li>Run the included script once as <strong>root</strong> in DSM Task Scheduler:</li></ol>
<code>/bin/sh /volume1/docker/docker-app-manager/reset_setup_on_synology.sh --confirm-reset</code>
<ol start="3"><li>Start the project.</li><li>Copy the new setup token from the container log and open <strong>/setup</strong>.</li></ol>
<p>The reset script moves the previous authentication and TOTP replay state into a private timestamped backup. It does not change GLPI projects or backups.</p>
<p class="foot">Diagnostic detail: {{ diagnostic }}</p></main></body></html>"""

SETUP_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Secure setup · Docker App Manager</title>
<style>
:root{color-scheme:light;--navy:#12233f;--blue:#2364d2;--blue-dark:#184da8;--ink:#172033;--muted:#64748b;--line:#dce3ec;--soft:#f5f8fc;--danger:#b42318}
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(circle at 15% 0,#eaf1ff 0,transparent 36%),#f4f7fb;color:var(--ink);font:15px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.shell{width:min(1040px,calc(100% - 32px));margin:48px auto}.brand{display:flex;align-items:center;gap:12px;margin-bottom:22px;color:var(--navy);font-weight:750;letter-spacing:-.01em}.brand-mark{display:grid;place-items:center;width:38px;height:38px;border-radius:11px;background:linear-gradient(145deg,#2e71df,#174ca7);box-shadow:0 8px 22px #1d5ecb3b;color:#fff;font-size:18px}.brand small{display:block;color:var(--muted);font-size:12px;font-weight:550;letter-spacing:.02em}
.layout{display:grid;grid-template-columns:minmax(0,.85fr) minmax(460px,1.15fr);overflow:hidden;background:#fff;border:1px solid #dbe3ee;border-radius:20px;box-shadow:0 24px 70px #16345f1c}.intro{padding:44px;background:linear-gradient(155deg,#12233f,#183862);color:#fff}.eyebrow{display:inline-flex;align-items:center;gap:7px;padding:5px 10px;border:1px solid #ffffff2b;border-radius:999px;background:#ffffff12;color:#d8e6ff;font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.08em}.intro h1{max-width:420px;margin:22px 0 12px;font-size:34px;line-height:1.13;letter-spacing:-.035em}.intro>p{margin:0;color:#c9d5e8}.steps{display:grid;gap:20px;margin-top:38px}.step{display:grid;grid-template-columns:34px 1fr;gap:13px}.step-number{display:grid;place-items:center;width:30px;height:30px;border:1px solid #ffffff38;border-radius:50%;background:#ffffff12;color:#fff;font-size:13px;font-weight:750}.step strong{display:block;margin:2px 0 3px}.step span{color:#b9c8dd;font-size:13px}.privacy{margin-top:38px;padding-top:20px;border-top:1px solid #ffffff1c;color:#aebed5;font-size:12px}
.panel{padding:40px 44px}.panel-head{margin-bottom:25px}.panel-head h2{margin:0 0 6px;color:var(--navy);font-size:23px;letter-spacing:-.02em}.panel-head p{margin:0;color:var(--muted);font-size:14px}.error{display:flex;gap:10px;margin:0 0 20px;padding:12px 14px;border:1px solid #f3b9b4;border-radius:10px;background:#fff5f4;color:var(--danger);font-size:14px;font-weight:600}.error:before{content:"!";display:grid;place-items:center;flex:0 0 20px;height:20px;border-radius:50%;background:var(--danger);color:#fff;font-size:12px}
.field{margin-top:16px}label{display:flex;justify-content:space-between;gap:10px;margin-bottom:7px;color:#29364a;font-size:13px;font-weight:700}.hint{color:#8491a4;font-weight:500}input{width:100%;height:44px;padding:0 13px;border:1px solid #cbd5e1;border-radius:9px;background:#fff;color:var(--ink);font:inherit;outline:none;transition:border-color .15s,box-shadow .15s}input:focus{border-color:var(--blue);box-shadow:0 0 0 3px #2364d21c}input::placeholder{color:#a1adbd}.row{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.mfa{margin:20px 0;padding:15px;border:1px solid #cfe0fb;border-radius:12px;background:#f4f8ff}.mfa-title{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;color:#294369;font-size:13px;font-weight:750}.badge{padding:3px 7px;border-radius:999px;background:#dceaff;color:#2156a8;font-size:11px}.secret{padding:11px 12px;border:1px dashed #9bb9e8;border-radius:8px;background:#fff;color:#15325d;font:600 14px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:.045em;overflow-wrap:anywhere;user-select:all}.mfa-help{margin:8px 0 0;color:#657994;font-size:12px}
.scheduler{margin:22px 0 4px;padding:16px;border:1px solid #dce3ec;border-radius:12px;background:#fbfcfe}.scheduler-head{display:flex;align-items:flex-start;justify-content:space-between;gap:14px}.scheduler h3{margin:0 0 4px;color:#23324a;font-size:15px}.scheduler p{margin:0;color:#65748a;font-size:12px}.scheduler-status{flex:0 0 auto;padding:4px 8px;border-radius:999px;background:#fff0d8;color:#8a5200;font-size:11px;font-weight:750}.scheduler-status.ready{background:#e7f7ed;color:#176b3a}.scheduler ol{margin:13px 0;padding-left:20px;color:#425169;font-size:12px}.scheduler li+li{margin-top:4px}.command-wrap{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px;align-items:stretch}.command{min-width:0;padding:10px 11px;border:1px solid #d6deea;border-radius:8px;background:#fff;color:#233b5d;font:600 11px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;overflow-wrap:anywhere;user-select:all}.copy-command{width:auto;height:auto;min-height:38px;margin:0;padding:0 12px;border:1px solid #aac6ef;border-radius:8px;background:#fff;box-shadow:none;color:#24569f;font-size:12px}.copy-command:hover{background:#edf5ff;filter:none}.scheduler-foot{display:flex;justify-content:space-between;gap:12px;margin-top:9px}.scheduler-foot a{color:#24569f;font-size:12px;font-weight:700}.scheduler-foot span{color:#8491a4;font-size:11px}
button{display:flex;align-items:center;justify-content:center;width:100%;height:46px;margin-top:23px;border:0;border-radius:10px;background:linear-gradient(180deg,var(--blue),var(--blue-dark));box-shadow:0 8px 20px #205fc332;color:#fff;font:700 14px system-ui;cursor:pointer}button:hover{filter:brightness(1.04)}button:focus-visible{outline:3px solid #2364d23d;outline-offset:2px}.footnote{margin:14px 0 0;text-align:center;color:#8491a4;font-size:12px}.preview-banner{margin:0 0 18px;padding:12px 14px;border:1px solid #eed09b;border-radius:10px;background:#fff8e8;color:#805000;font-size:13px}.preview-back{display:block;margin-top:12px;text-align:center;font-weight:700}fieldset{min-width:0;margin:0;padding:0;border:0}fieldset:disabled{opacity:.72}fieldset:disabled button{cursor:not-allowed;box-shadow:none}
@media(max-width:820px){.shell{margin:20px auto}.layout{grid-template-columns:1fr}.intro{padding:30px}.intro h1{font-size:28px}.steps{grid-template-columns:1fr 1fr}.privacy{display:none}.panel{padding:30px}}@media(max-width:560px){.shell{width:min(100% - 20px,1040px);margin:10px auto}.brand{margin:15px 4px}.layout{border-radius:15px}.intro{padding:25px 22px}.intro h1{font-size:25px}.steps{grid-template-columns:1fr;gap:14px;margin-top:24px}.panel{padding:26px 20px}.row{grid-template-columns:1fr}.scheduler-head,.scheduler-foot{display:block}.scheduler-status{display:inline-block;margin-top:9px}.command-wrap{grid-template-columns:1fr}.copy-command{min-height:40px}.scheduler-foot span{display:block;margin-top:5px}}
</style></head><body><div class="shell"><div class="brand"><div class="brand-mark">D</div><div>Docker App Manager<small>Synology deployment console</small></div></div>
<main class="layout"><section class="intro"><span class="eyebrow">● Secure onboarding</span><h1>Let’s secure your Builder.</h1>
<p>Complete this one-time setup before managing GLPI environments.</p>
<div class="steps"><div class="step"><div class="step-number">1</div><div><strong>Verify this instance</strong><span>Use the token shown in the container log.</span></div></div>
<div class="step"><div class="step-number">2</div><div><strong>Create your administrator</strong><span>Choose a unique username and strong password.</span></div></div>
<div class="step"><div class="step-number">3</div><div><strong>Enable two-factor authentication</strong><span>Add the secret to your authenticator and confirm a code.</span></div></div>
<div class="step"><div class="step-number">4</div><div><strong>Enable scheduled backups</strong><span>Create one DSM task for every project schedule.</span></div></div></div>
<div class="privacy">Your password is stored as a one-way hash. This onboarding page is permanently disabled after successful setup.</div></section>
<section class="panel"><div class="panel-head"><h2>Administrator setup</h2><p>All fields are required. This takes about one minute.</p></div>
{% if preview_only %}<div class="preview-banner"><strong>Read-only test preview.</strong> This screen cannot create or change an administrator.</div>{% endif %}
{% if error %}<div class="error" role="alert">{{ error }}</div>{% endif %}
<form method="post" action="{{ url_for('setup') if not preview_only else '#' }}"><fieldset {% if preview_only %}disabled{% endif %}><input type="hidden" name="csrf_token" value="{{ csrf_token }}">
<div class="field"><label for="setup_token">Instance setup token <span class="hint">Container log</span></label><input id="setup_token" name="setup_token" autocomplete="off" placeholder="Paste the one-time token" required autofocus></div>
<div class="field"><label for="username">Administrator username</label><input id="username" name="username" pattern="[A-Za-z0-9_.\-]{3,64}" autocomplete="username" placeholder="For example: builder-admin" required></div>
<div class="row"><div class="field"><label for="password">Password <span class="hint">14+ characters</span></label><input id="password" type="password" name="password" minlength="14" autocomplete="new-password" placeholder="Enter a strong password" required></div>
<div class="field"><label for="confirm_password">Confirm password</label><input id="confirm_password" type="password" name="confirm_password" minlength="14" autocomplete="new-password" placeholder="Repeat your password" required></div></div>
<div class="mfa"><div class="mfa-title"><span>Authenticator setup secret</span><span class="badge">Step 3</span></div><div class="secret" title="Select and copy this secret">{{ totp_secret }}</div><p class="mfa-help">Add this secret manually to Microsoft Authenticator, Google Authenticator or another TOTP app.</p></div>
<div class="field"><label for="totp">Six-digit verification code <span class="hint">Changes every 30 seconds</span></label><input id="totp" name="totp" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" placeholder="000000" required></div>
<section class="scheduler" aria-labelledby="scheduler-title"><div class="scheduler-head"><div><h3 id="scheduler-title">Scheduled backups <span class="badge">Optional now</span></h3><p>Create one Synology task. It safely handles every project and its own frequency.</p></div><span class="scheduler-status {{ 'ready' if dispatcher_healthy else '' }}">{{ 'Task detected' if dispatcher_healthy else 'Not detected yet' }}</span></div>
<ol><li>In DSM, open <strong>Control Panel → Task Scheduler</strong>.</li><li>Create a <strong>User-defined script</strong>, run it as <strong>root</strong> every <strong>5 minutes</strong>.</li><li>Paste this exact command and run the task once:</li></ol>
<div class="command-wrap"><code class="command" id="dispatcher-command">{{ dispatcher_command }}</code><button class="copy-command" type="button" data-copy-command>Copy command</button></div>
<div class="scheduler-foot"><a href="{{ request.path }}">Check task status</a><span>You can safely finish setup first and configure this later under Backups.</span></div></section>
<button type="submit">Complete secure setup&nbsp; →</button><p class="footnote">Setup can only be completed once for this installation.</p>
</fieldset></form>{% if preview_only %}<a class="preview-back" href="{{ url_for('login') }}">← Back to sign in</a>{% endif %}</section></main></div>
<script src="{{ url_for('ui_javascript') }}" defer></script></body></html>"""


@app.route("/setup", methods=["GET", "POST"])
def setup():
    global AUTH_CONFIG, AUTH_CONFIG_ERROR

    if AUTH_CONFIG is not None:
        abort(404)
    if AUTH_CONFIG_PATH.exists():
        return render_template_string(
            AUTH_RECOVERY_HTML,
            diagnostic=AUTH_CONFIG_ERROR or "The authentication file exists but setup is already disabled.",
        ), 503
    key = "setup:" + login_rate_key()
    if login_rate_is_blocked(key):
        return ("Too many setup attempts. Try again later.", 429, {"Retry-After": str(LOGIN_RATE_BLOCK_SECONDS)})
    secret = session.get("setup_totp_secret")
    if not secret:
        secret = generate_totp_secret()
        session["setup_totp_secret"] = secret
    error = ""
    if request.method == "POST":
        try:
            require_csrf()
            if not SETUP_TOKEN or not hmac.compare_digest(
                str(request.form.get("setup_token", "")),
                SETUP_TOKEN,
            ):
                raise ValueError("The setup token is invalid.")
            username = str(request.form.get("username", "")).strip()
            password = str(request.form.get("password", ""))
            if password != str(request.form.get("confirm_password", "")):
                raise ValueError("The passwords do not match.")
            password_hash = hash_password(password)
            candidate = {
                "BUILDER_ADMIN_USERNAME": username,
                "BUILDER_ADMIN_PASSWORD_HASH": password_hash,
                "BUILDER_ADMIN_TOTP_SECRET": secret,
                "BUILDER_SESSION_COOKIE_SECURE": os.environ.get(
                    "BUILDER_SESSION_COOKIE_SECURE", "false"
                ),
                "BUILDER_SESSION_TIMEOUT_SECONDS": os.environ.get(
                    "BUILDER_SESSION_TIMEOUT_SECONDS", "900"
                ),
                "BUILDER_SESSION_ABSOLUTE_TIMEOUT_SECONDS": os.environ.get(
                    "BUILDER_SESSION_ABSOLUTE_TIMEOUT_SECONDS", "28800"
                ),
                "FLASK_SECRET_KEY": secrets.token_hex(32),
            }
            config = load_auth_config(candidate)
            counter = matching_totp_counter(secret, request.form.get("totp"), window=1)
            if counter is None:
                raise ValueError("The authenticator code is invalid.")
            atomic_write_persisted_auth(candidate)
            AUTH_CONFIG = config
            AUTH_CONFIG_ERROR = ""
            app.secret_key = candidate["FLASK_SECRET_KEY"]
            login_rate_clear(key)
            now = int(time.time())
            session.clear()
            session.update(
                admin_authenticated=True,
                admin_issued_at=now,
                admin_last_activity=now,
                csrf_token=secrets.token_urlsafe(32),
                login_nonce=secrets.token_urlsafe(24),
            )
            write_last_totp_counter(counter)
            return redirect(url_for("index"))
        except ValueError as exc:
            login_rate_record_failure(key)
            error = str(exc)
    return render_template_string(
        SETUP_HTML,
        error=error,
        totp_secret=secret,
        preview_only=False,
        dispatcher_command=f"/bin/bash {BACKUP_DISPATCHER_PATH}",
        dispatcher_healthy=dispatcher_is_healthy(),
    )


@app.route("/test-preview/enter", methods=["GET"])
def test_preview_enter():
    if not BUILDER_TEST_PREVIEW_MODE:
        abort(404)
    session.clear()
    session["test_preview_active"] = True
    session["csrf_token"] = secrets.token_urlsafe(32)
    return redirect(url_for("index"))


@app.route("/test-preview/setup", methods=["GET"])
def test_preview_setup():
    if not BUILDER_TEST_PREVIEW_MODE:
        abort(404)
    return render_template_string(
        SETUP_HTML,
        error="",
        totp_secret="TEST-PREVIEW-SECRET-NOT-A-REAL-CREDENTIAL",
        preview_only=True,
        dispatcher_command=f"/bin/bash {BACKUP_DISPATCHER_PATH}",
        dispatcher_healthy=False,
    )


@app.route("/test-preview/exit", methods=["GET"])
def test_preview_exit():
    if not BUILDER_TEST_PREVIEW_MODE:
        abort(404)
    session.clear()
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if AUTH_CONFIG_ERROR or not AUTH_CONFIG:
        if not AUTH_CONFIG_PATH.exists():
            return redirect(url_for("setup"))
        return render_template_string(
            AUTH_RECOVERY_HTML,
            diagnostic=AUTH_CONFIG_ERROR or "The authentication file exists but could not be used.",
        ), 503
    next_path = safe_internal_next(request.values.get("next"))
    if request.method in {"GET", "HEAD"}:
        if authenticated_session_is_current():
            return redirect(next_path)
        return render_template_string(
            LOGIN_HTML,
            error=False,
            next_path=next_path,
            test_preview_enabled=BUILDER_TEST_PREVIEW_MODE,
        )

    key = login_rate_key()
    if login_rate_is_blocked(key):
        response = make_response(
            render_template_string(
                LOGIN_HTML,
                error=True,
                next_path=next_path,
                test_preview_enabled=BUILDER_TEST_PREVIEW_MODE,
            ),
            429,
        )
        response.headers["Retry-After"] = str(LOGIN_RATE_BLOCK_SECONDS)
        return response
    try:
        require_csrf()
        username_ok = hmac.compare_digest(str(request.form.get("username", "")), AUTH_CONFIG.username)
        password_ok = verify_password(str(request.form.get("password", "")), AUTH_CONFIG.password_hash)
        counter = matching_totp_counter(AUTH_CONFIG.totp_secret, request.form.get("totp"), window=1)
        with TOTP_REPLAY_LOCK:
            replay_ok = counter is not None and counter > read_last_totp_counter()
            if not (username_ok and password_ok and replay_ok):
                raise ValueError("invalid login")
            write_last_totp_counter(counter)
    except Exception:
        login_rate_record_failure(key)
        return render_template_string(
            LOGIN_HTML,
            error=True,
            next_path=next_path,
            test_preview_enabled=BUILDER_TEST_PREVIEW_MODE,
        ), 401

    login_rate_clear(key)
    now = int(time.time())
    session.clear()
    session["admin_authenticated"] = True
    session["admin_issued_at"] = now
    session["admin_last_activity"] = now
    session["csrf_token"] = secrets.token_urlsafe(32)
    session["login_nonce"] = secrets.token_urlsafe(24)
    return redirect(next_path)


@app.route("/logout", methods=["POST"])
def logout():
    try:
        require_csrf()
    except ValueError:
        abort(400)
    session.clear()
    return redirect(url_for("login"))


@app.before_request
def require_admin_authentication():
    if request.endpoint in {"healthz", "favicon", "login", "setup", "ui_javascript"}:
        return None
    if request.endpoint in {
        "test_preview_enter",
        "test_preview_setup",
        "test_preview_exit",
    }:
        return None
    if BUILDER_TEST_PREVIEW_MODE and session.get("test_preview_active"):
        if request.method != "GET" and request.endpoint not in {
            "create",
            "execute_create",
            "set_backup_source_route",
        }:
            return ("Changes are disabled in the local test preview.", 403)
        return None
    if AUTH_CONFIG_ERROR or not AUTH_CONFIG:
        return redirect(url_for("setup")) if not AUTH_CONFIG_PATH.exists() else (
            "Authentication configuration is invalid.", 503
        )
    if not authenticated_session_is_current():
        session.clear()
        next_path = request.full_path if request.method == "GET" else request.path
        return redirect(url_for("login", next=safe_internal_next(next_path)))
    return None


@app.before_request
def v11_single_mutation_guard():
    if request.method != "POST" or request.endpoint in {"login", "logout", "setup"}:
        return None
    if not MUTATION_LOCK.acquire(blocking=False):
        return ("Another administration action is already running. Wait until it completes.", 409)
    g.mutation_lock_held = True
    invalidate_dashboard_cache()
    return None


@app.teardown_request
def v11_release_mutation_lock(_error=None):
    if getattr(g, "mutation_lock_held", False):
        MUTATION_LOCK.release()


@app.route("/healthz")
def healthz():
    onboarding = AUTH_CONFIG is None and not AUTH_CONFIG_PATH.exists() and not PERSISTED_AUTH_ERROR
    healthy = (onboarding or (not AUTH_CONFIG_ERROR and AUTH_CONFIG is not None)) and BASE_PATH.is_dir() and BACKUP_ROOT.is_dir()
    client = None
    if healthy:
        try:
            client = docker_client()
            client.ping()
        except Exception:
            healthy = False
        finally:
            if client is not None:
                close_docker_client(client)
    return jsonify({
        "status": "ok" if healthy else "unhealthy",
        "mode": "setup" if onboarding else "ready",
    }), 200 if healthy else 503


@app.route("/favicon.ico")
def favicon():
    return ("", 204)


@app.route("/assets/app.js")
def ui_javascript():
    response = make_response(UI_JAVASCRIPT)
    response.headers["Content-Type"] = "application/javascript; charset=utf-8"
    return response


@app.route("/api/status")
def status_snapshot():
    _docker_snapshot, projects = professional_ui_snapshot()
    return jsonify({
        "checked_at": datetime.now().strftime("%H:%M:%S"),
        "projects": [
            {
                "name": project["name"],
                "glpi": project["glpi_status"],
                "database": project["db_status"],
            }
            for project in projects
        ],
    })


def test_demo_is_active():
    return (
        BUILDER_TEST_PREVIEW_MODE
        and has_request_context()
        and bool(session.get("test_preview_active"))
    )


def demo_project(
    name,
    port,
    *,
    glpi_image="glpi/glpi:11.0.8",
    mariadb_image="mariadb:11.4",
    glpi_status="running",
    db_status="running",
    backup_source=False,
    backup_ready=False,
    latest=None,
):
    issues = []
    if backup_source and not backup_ready:
        issues = ["The latest backup verification is intentionally failing in this demo."]
    return {
        "name": name,
        "env_port": str(port),
        "active_port": str(port),
        "glpi_status": glpi_status,
        "db_status": db_status,
        "glpi_image": glpi_image,
        "mariadb_image": mariadb_image,
        "tz": "Europe/Brussels",
        "cookie_samesite": "Lax",
        "cookie_secure": "Off",
        "path": f"/demo-only/{name}",
        "mappings": f"0.0.0.0:{port} → 8080/tcp (simulated)",
        "latest_log": "20260727-201500-demo-action.log",
        "logs": [
            "20260727-201500-demo-action.log",
            "20260727-194500-health-check.log",
        ],
        "backup_source": backup_source,
        "backup_status": {
            "selected": backup_source,
            "ready": backup_ready,
            "issues": issues,
            "latest": latest,
            "schedule": {
                "enabled": backup_source,
                "kind": "daily",
                "time": "02:00",
                "weekdays": "7",
                "interval_hours": 24,
                "retention_days": 60,
                "next_run": "2026-07-28 02:00" if backup_source else "",
                "last_status": "success" if backup_ready else ("failed" if backup_source else "Not run"),
                "last_attempt": "2026-07-27 02:00" if backup_source else "",
                "last_success": "2026-07-27 02:04" if backup_ready else "",
                "dispatcher_healthy": True,
            },
        },
        "simulated": True,
    }


def demo_preview_projects():
    projects = [
        demo_project(
            "demo-production",
            8088,
            backup_source=True,
            backup_ready=True,
            latest={
                "name": "demo-production-20260727.sql.gz",
                "created_at": "2026-07-27 19:45",
                "size_label": "184 MB",
                "checksum_manifest": True,
            },
        ),
        demo_project("demo-staging", 8089),
        demo_project(
            "demo-recovery",
            8090,
            glpi_status="exited",
            backup_source=True,
            backup_ready=False,
        ),
    ]
    for item in session.get("test_demo_projects", []):
        if not isinstance(item, dict):
            continue
        try:
            projects.append(
                demo_project(
                    validate_project(item.get("name")),
                    validate_port(item.get("port"), "Demo port"),
                    glpi_image=str(
                        item.get("glpi_image") or "glpi/glpi:11.0.8"
                    ),
                    mariadb_image=str(
                        item.get("mariadb_image") or "mariadb:11.4"
                    ),
                    backup_source=bool(item.get("backup_source")),
                    backup_ready=bool(item.get("backup_source")),
                )
            )
        except ValueError:
            continue
    overrides = session.get("test_demo_schedule_overrides", {})
    if isinstance(overrides, dict):
        for project in projects:
            override = overrides.get(project["name"])
            if not isinstance(override, dict):
                continue
            project["backup_source"] = bool(override.get("enabled"))
            project["backup_status"]["selected"] = project["backup_source"]
            project["backup_status"]["schedule"].update(override)
            project["backup_status"]["ready"] = project["backup_source"]
            project["backup_status"]["issues"] = []
    return projects


def demo_backup_choices():
    root = "/demo-only/backups"
    return {
        "database": [
            (f"{root}/demo-production.sql.gz", "demo-production.sql.gz · verified"),
            (f"{root}/demo-legacy.dump.gz", "demo-legacy.dump.gz · migration sample"),
        ],
        "files": [
            (f"{root}/demo-production-files.tar.gz", "demo-production-files.tar.gz · verified"),
            (f"{root}/demo-legacy-files.tar.gz", "demo-legacy-files.tar.gz · migration sample"),
        ],
    }


def validate_demo_create_request(source):
    operation_mode = str(source.get("operation_mode") or "restore").strip().lower()
    if operation_mode not in OPERATION_MODES:
        raise ValueError("Operation mode must be Full restore or Fresh installation.")
    fresh_install = operation_mode == "fresh"
    backup_choices = demo_backup_choices()
    db_backup = str(
        source.get("db_backup")
        or source.get("db_backup_select")
        or ""
    )
    file_backup = str(
        source.get("file_backup")
        or source.get("file_backup_select")
        or ""
    )
    if not fresh_install:
        if db_backup not in {value for value, _label in backup_choices["database"]}:
            raise ValueError("Select one of the simulated database backups.")
        if file_backup not in {value for value, _label in backup_choices["files"]}:
            raise ValueError("Select one of the simulated GLPI files backups.")
    else:
        db_backup = ""
        file_backup = ""
    project = validate_project(source.get("project"))
    if project in {item["name"] for item in demo_preview_projects()}:
        raise ValueError("That simulated project already exists.")
    host_port = validate_port(source.get("host_port"), "Host port")
    glpi_image = str(source.get("glpi_image") or "glpi/glpi:11.0.8").strip()
    mariadb_image = str(source.get("mariadb_image") or "mariadb:11.4").strip()
    if glpi_image != "glpi/glpi:11.0.8":
        raise ValueError("Select the simulated GLPI image shown in the wizard.")
    if mariadb_image not in {"mariadb:10.11", "mariadb:11.4"}:
        raise ValueError("Select the simulated database image shown in the wizard.")
    tz = str(source.get("tz") or TZ_DEFAULT).strip()
    if tz != TZ_DEFAULT:
        raise ValueError("The demo preview uses the configured default time zone.")
    return {
        "project": project,
        "glpi_image": glpi_image,
        "mariadb_image": mariadb_image,
        "host_port": host_port,
        "container_port": GLPI_INTERNAL_PORT,
        "tz": tz,
        "cookie_samesite": validate_cookie_samesite(source.get("cookie_samesite")),
        "cookie_secure": validate_cookie_secure(source.get("cookie_secure")),
        "operation_mode": operation_mode,
        "fresh_install": fresh_install,
        "clean_db": fresh_install,
        "force_recreate": True,
        "restore_everything": not fresh_install,
        "skip_plugins": fresh_install or form_flag(source, "skip_plugins"),
        "update_backup_source": form_flag(source, "update_backup_source"),
        "db_backup": db_backup,
        "file_backup": file_backup,
        "existing_state": False,
        "confirm_destructive": False,
        "confirm_project": "",
        "backup_root": "/demo-only/backups",
    }


def demo_compose_yaml(project):
    selected = next(
        (item for item in demo_preview_projects() if item["name"] == project),
        None,
    )
    if not selected:
        return ""
    return f"""# Simulated configuration — never sent to Docker
services:
  {project}-db:
    image: {selected["mariadb_image"]}
    container_name: {project}-db
    environment:
      MARIADB_ROOT_PASSWORD: ${{MARIADB_ROOT_PASSWORD}}
      MARIADB_PASSWORD: ${{GLPI_DB_PASSWORD}}
    volumes:
      - /demo-only/{project}/db:/var/lib/mysql:rw
  {project}:
    image: {selected["glpi_image"]}
    container_name: {project}
    ports:
      - "{selected["active_port"]}:8080"
    environment:
      GLPI_DB_PASSWORD: ${{GLPI_DB_PASSWORD}}
      GLPI_SESSION_COOKIE_SAMESITE: {selected["cookie_samesite"]}
      GLPI_SESSION_COOKIE_SECURE: {selected["cookie_secure"]}
    volumes:
      - /demo-only/{project}/glpi:/var/glpi:rw
"""


def discover_profile_applications():
    """Discover only projects carrying a valid Builder application manifest."""
    applications = []
    if not BASE_PATH.is_dir():
        return applications
    try:
        folders = list(BASE_PATH.iterdir())[:MAX_SCAN_ENTRIES]
    except OSError:
        return applications
    for folder in folders:
        if folder.is_symlink() or not folder.is_dir():
            continue
        data = read_application_manifest(folder)
        if not data:
            continue
        app_container = get_container(data["project"])
        db_container = get_container(f"{data['project']}-db")
        app_status = getattr(app_container, "status", "missing") if app_container else "missing"
        db_status = getattr(db_container, "status", "missing") if db_container else "missing"
        report = None
        report_path = folder / QUARANTINE_REPORT
        if report_path.is_file() and not report_path.is_symlink():
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                report = None
        expires_at = str(data.get("expires_at") or "")
        expired = False
        if expires_at:
            try:
                expired = datetime.fromisoformat(expires_at) <= datetime.now()
            except ValueError:
                expired = True
        scheduled = data["project"] in set(scheduled_backup_projects())
        backup_state = scheduled_backup_status(data["project"])
        applications.append({
            "name": data["project"],
            "app_type": data["type"],
            "app_label": data["name"],
            "env_port": str(data["port"]),
            "active_port": str(data["port"]),
            "glpi_status": app_status,
            "db_status": db_status,
            "glpi_image": data["image"],
            "mariadb_image": data.get("database", "Managed database"),
            "tz": read_env(data["project"]).get("TZ", TZ_DEFAULT),
            "cookie_samesite": DEFAULT_SESSION_COOKIE_SAMESITE,
            "cookie_secure": DEFAULT_SESSION_COOKIE_SECURE,
            "path": str(folder),
            "mappings": f"{data['port']} → {data['internal_port']}/tcp",
            "latest_log": None,
            "logs": list_logs(data["project"]),
            "backup_source": scheduled,
            "backup_status": backup_state,
            "profile_managed": True,
            "quarantine": bool(data.get("quarantine")),
            "quarantine_report": report,
            "expires_at": expires_at,
            "expired": expired,
        })
    return applications


def professional_ui_snapshot():
    docker_snapshot = dashboard_docker_snapshot()
    projects = []
    for item in discover_projects(docker_snapshot["containers"]):
        if isinstance(item, dict):
            projects.append(item)
            continue
        projects.append({
            name: getattr(item, name, default)
            for name, default in (
                ("name", ""),
                ("env_port", ""),
                ("active_port", ""),
                ("glpi_status", "unknown"),
                ("db_status", "unknown"),
                ("glpi_image", ""),
                ("mariadb_image", ""),
                ("tz", ""),
                ("cookie_samesite", "Lax"),
                ("cookie_secure", "Off"),
                ("path", ""),
                ("mappings", "-"),
                ("latest_log", None),
                ("logs", []),
                ("backup_source", False),
                ("backup_status", {"selected": False, "ready": False, "issues": [], "latest": None}),
            )
        })
    known_profile_names = {item["name"] for item in projects}
    projects.extend(
        item for item in discover_profile_applications()
        if item["name"] not in known_profile_names
    )
    if test_demo_is_active():
        docker_snapshot = dict(docker_snapshot)
        docker_snapshot["image_tags"] = tuple(sorted(set(
            docker_snapshot["image_tags"]
        ) | {"glpi/glpi:11.0.8", "mariadb:10.11", "mariadb:11.4"}))
        known = {item["name"] for item in projects}
        projects.extend(
            item for item in demo_preview_projects()
            if item["name"] not in known
        )
    projects = [
        enrich_project_operational_metadata(
            item, docker_snapshot["image_tags"]
        )
        for item in projects
    ]
    return docker_snapshot, projects


def render_professional_page(template, page_title, active_page, **context):
    return render_template_string(
        page_template(template),
        page_title=page_title,
        active_page=active_page,
        **context,
    )


def overview_findings(projects, unmanaged_projects):
    findings = []
    for project in projects:
        if project["glpi_status"] != "running":
            findings.append({
                "title": f"{project['name']}: {project.get('app_label') or 'application'} is {project['glpi_status']}",
                "detail": "Open the application to inspect its status and lifecycle controls.",
            })
        if project["db_status"] != "running":
            findings.append({
                "title": f"{project['name']}: database is {project['db_status']}",
                "detail": "Database-dependent operations may be unavailable.",
            })
        if project["backup_source"] and not project["backup_status"]["ready"]:
            findings.append({
                "title": f"{project['name']}: backup needs attention",
                "detail": "; ".join(project["backup_status"]["issues"]) or "Review backup readiness.",
            })
    if unmanaged_projects:
        findings.append({
            "title": f"{len(unmanaged_projects)} unmanaged GLPI environment(s) detected",
            "detail": "These environments are display-only and cannot be changed by Builder.",
        })
    return findings[:8]


@app.route("/", methods=["GET"])
def index():
    docker_snapshot, projects = professional_ui_snapshot()
    unmanaged = discover_unmanaged_glpi_projects(docker_snapshot["containers"], projects)
    running = sum(
        project["glpi_status"] == "running" and project["db_status"] == "running"
        for project in projects
    )
    issue_count = sum(project["glpi_status"] != "running" for project in projects)
    issue_count += sum(project["db_status"] != "running" for project in projects)
    scheduled = [project for project in projects if project["backup_source"]]
    ready_schedules = sum(project["backup_status"]["ready"] for project in scheduled)
    latest_values = [
        project["backup_status"]["latest"] for project in scheduled
        if project["backup_status"]["latest"]
    ]
    latest = max(latest_values, key=lambda item: item.get("created_at", "")) if latest_values else None
    stats = {
        "projects": len(projects),
        "running": running,
        "issues": issue_count,
        "backup_label": f"{ready_schedules}/{len(scheduled)}" if scheduled else "None",
        "backup_detail": (
            f"Latest: {latest['created_at']}" if latest else
            ("No verified backup created yet" if scheduled else "No schedules enabled")
        ),
        "images": len(docker_snapshot["image_tags"]),
    }
    return render_professional_page(
        OVERVIEW,
        "Overview",
        "overview",
        projects=projects,
        stats=stats,
        issues=overview_findings(projects, unmanaged),
        unmanaged_projects=unmanaged,
    )


@app.route("/projects", methods=["GET"])
def projects_page():
    docker_snapshot, projects = professional_ui_snapshot()
    return render_professional_page(
        PROJECTS,
        "Applications",
        "projects",
        projects=projects,
        unmanaged_projects=discover_unmanaged_glpi_projects(
            docker_snapshot["containers"], projects
        ),
    )


@app.route("/projects/new", methods=["GET"])
def new_project_page():
    backup_choices = (
        demo_backup_choices()
        if test_demo_is_active()
        else scan_backup_choices(BACKUP_ROOT, include_dirs=True)
    )
    docker_snapshot, _projects = professional_ui_snapshot()
    preflight = synology_preflight()
    return render_professional_page(
        WIZARD,
        "New project",
        "projects",
        backup_root=(
            "/demo-only/backups"
            if test_demo_is_active()
            else str(BACKUP_ROOT)
        ),
        db_backups=backup_choices["database"],
        file_backups=backup_choices["files"],
        glpi_images=local_image_tags("glpi", docker_snapshot["image_tags"]),
        db_images=local_glpi_database_image_tags(docker_snapshot["image_tags"]),
        suggested_host_port=suggest_free_host_port(
            containers=docker_snapshot["containers"]
        ),
        profiles=profile_catalog(),
        tz_default=TZ_DEFAULT,
        demo_mode=test_demo_is_active(),
        preflight=preflight,
    )


@app.route("/applications/new", methods=["GET"])
def new_application_page():
    docker_snapshot, _projects = professional_ui_snapshot()
    backup_choices = scan_backup_choices(BACKUP_ROOT, include_dirs=False)
    preflight = synology_preflight()
    requested_app = request.args.get("app", "").strip().lower()
    if requested_app == "glpi":
        glpi_choices = (
            demo_backup_choices() if test_demo_is_active()
            else scan_backup_choices(BACKUP_ROOT, include_dirs=True)
        )
        return render_professional_page(
            WIZARD, "Add application", "projects",
            backup_root="/demo-only/backups" if test_demo_is_active() else str(BACKUP_ROOT),
            db_backups=glpi_choices["database"], file_backups=glpi_choices["files"],
            glpi_images=local_image_tags("glpi", docker_snapshot["image_tags"]),
            db_images=local_glpi_database_image_tags(docker_snapshot["image_tags"]),
            suggested_host_port=suggest_free_host_port(containers=docker_snapshot["containers"]),
            profiles=profile_catalog(),
            tz_default=TZ_DEFAULT, demo_mode=test_demo_is_active(), preflight=preflight,
        )
    profiles = profile_catalog()
    selected_profile = next(
        (profile for profile in profiles if profile.key == requested_app),
        profiles[0],
    )
    return render_professional_page(
        APPLICATION_WIZARD,
        "Add application",
        "projects",
        profiles=profiles,
        selected_app=selected_profile.key,
        profile_images=local_profile_image_tags(selected_profile, docker_snapshot["image_tags"]),
        database_images=local_profile_database_image_tags(
            selected_profile, docker_snapshot["image_tags"]
        ),
        db_backups=classify_n8n_backup_choices(backup_choices["database"]),
        tpm_backups=classify_tpm_backup_choices(backup_choices["database"]),
        suggested_host_port=suggest_free_host_port(
            containers=docker_snapshot["containers"]
        ),
        suggested_bind_address=suggested_management_address(),
        quarantine_default_days=QUARANTINE_DEFAULT_DAYS,
        preflight=preflight,
        tz_default=TZ_DEFAULT,
    )


def suggested_management_address():
    host = request.host.split(":", 1)[0] if has_request_context() else ""
    try:
        address = ipaddress.ip_address(host)
        if not address.is_unspecified:
            return str(address)
    except ValueError:
        pass
    return "127.0.0.1"


def validate_bind_address(value, *, quarantine=False):
    value = str(value or ("127.0.0.1" if quarantine else "0.0.0.0")).strip()
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValueError("Management bind address must be one IPv4 or IPv6 address.") from exc
    if quarantine and address.is_unspecified:
        raise ValueError("Quarantine may not bind to every NAS interface; choose a management IP or 127.0.0.1.")
    return str(address)


def synology_preflight():
    """Read-only host checks; image architecture is finally proven by Docker pull."""
    checks = []
    machine = getattr(os.uname(), "machine", "unknown")
    checks.append({"name": "Host architecture", "status": "pass", "detail": machine})
    checks.append({
        "name": "Application root", "status": "pass" if BASE_PATH.is_dir() and os.access(BASE_PATH, os.W_OK) else "fail",
        "detail": str(BASE_PATH),
    })
    try:
        free = shutil.disk_usage(BASE_PATH if BASE_PATH.exists() else BASE_PATH.parent).free
        checks.append({
            "name": "Free storage", "status": "pass" if free >= 2 * 1024**3 else "fail",
            "detail": format_size(free) + " available",
        })
    except OSError as exc:
        checks.append({"name": "Free storage", "status": "fail", "detail": str(exc)})
    for name, command in (
        ("Docker engine", ["docker", "info", "--format", "{{.ServerVersion}}/{{.Architecture}}"]),
        ("Docker Compose", ["docker", "compose", "version", "--short"]),
    ):
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=15)
            checks.append({
                "name": name, "status": "pass" if result.returncode == 0 else "fail",
                "detail": tail_text(result.stdout or result.stderr, 200).strip() or "Unavailable",
            })
        except (OSError, subprocess.TimeoutExpired) as exc:
            checks.append({"name": name, "status": "fail", "detail": str(exc)})
    return {"ready": all(item["status"] == "pass" for item in checks), "checks": checks}


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_backup_member(root, name):
    candidate = Path(root) / str(name or "")
    if candidate.is_symlink():
        raise ValueError("Backup manifest may not reference symlinks.")
    target = candidate.resolve()
    try:
        target.relative_to(Path(root).resolve())
    except ValueError as exc:
        raise ValueError("Backup manifest references a file outside its backup set.") from exc
    if target.is_symlink() or not target.is_file() or not path_under_backup_root(target):
        raise ValueError("Backup manifest references a missing or unsafe file.")
    return target


def inspect_tpm_backup(value):
    path = Path(str(value or "").strip())
    if not path_under_backup_root(path) or path.is_symlink() or not path.is_file():
        raise ValueError("Select a database backup below the configured backup root.")
    if not path.name.lower().endswith((".sql", ".sql.gz")):
        raise ValueError("The TPM test restore adapter accepts only .sql or .sql.gz backups.")
    opener = gzip.open if path.name.lower().endswith(".gz") else open
    forbidden = re.compile(
        rb"\b(?:CREATE\s+USER|ALTER\s+USER|CREATE\s+DATABASE|DROP\s+DATABASE|USE\s+[`\w-]+|GRANT\s+|REVOKE\s+|INTO\s+OUTFILE|INTO\s+DUMPFILE|LOAD\s+DATA|INSTALL\s+PLUGIN|SHUTDOWN)\b",
        re.IGNORECASE,
    )
    create_tables, inserted_tables = set(), set()
    header = bytearray()
    carry = b""
    try:
        with opener(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                if len(header) < 65536:
                    header.extend(chunk[:65536 - len(header)])
                inspected = carry + chunk
                if b"\x00" in chunk or forbidden.search(inspected):
                    raise ValueError("The SQL backup contains binary or server-level statements that quarantine restore refuses.")
                create_tables.update(name.decode("utf-8", "replace") for name in re.findall(rb"CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+`?([A-Za-z0-9_]+)`?", inspected, re.I))
                inserted_tables.update(name.decode("utf-8", "replace") for name in re.findall(rb"INSERT\s+INTO\s+`?([A-Za-z0-9_]+)`?", inspected, re.I))
                carry = inspected[-256:]
    except (OSError, EOFError) as exc:
        raise ValueError("The database backup cannot be read safely.") from exc
    if not create_tables or not inserted_tables:
        raise ValueError("The SQL dump must contain both table definitions and restored data.")
    header_text = bytes(header).decode("utf-8", "replace")
    server_match = re.search(r"(?:Server version|Database server version:)\s*([^\r\n]+)", header_text, re.I)
    manifest_path = path.parent / TPM_BACKUP_MANIFEST
    manifest_data = None
    files = []
    if manifest_path.is_file() and not manifest_path.is_symlink():
        try:
            manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("The TPM backup manifest is unreadable or invalid JSON.") from exc
        if manifest_data.get("schema") != 1 or manifest_data.get("application") != "teampasswordmanager":
            raise ValueError("Unsupported TPM backup manifest contract.")
        database = manifest_data.get("database") or {}
        manifest_db = _safe_backup_member(path.parent, database.get("file"))
        if manifest_db != path.resolve():
            raise ValueError("The selected SQL file does not match the TPM backup manifest.")
        if not hmac.compare_digest(str(database.get("sha256") or "").lower(), sha256_file(path)):
            raise ValueError("The TPM database checksum does not match its manifest.")
        if str(database.get("engine") or "").lower() != "mysql":
            raise ValueError("This TPM adapter currently verifies only MySQL backup sets.")
        database_version = str(database.get("version") or "").strip()
        if not re.fullmatch(r"5\.7(?:\.\d+)?(?:[-+._A-Za-z0-9]*)?", database_version):
            raise ValueError("This TPM adapter currently verifies only MySQL 5.7 backup sets.")
        if server_match and not server_match.group(1).strip().startswith(database_version.split("-", 1)[0]):
            raise ValueError("SQL source server version does not match the TPM backup manifest.")
        expected_tables = set(manifest_data.get("tables") or [])
        if not expected_tables or not expected_tables.issubset(create_tables):
            raise ValueError("The SQL dump is missing tables declared by the TPM backup manifest.")
        for item in manifest_data.get("files") or []:
            extra = _safe_backup_member(path.parent, item.get("file"))
            role = str(item.get("role") or "").strip().lower()
            if role not in {"uploads"}:
                raise ValueError("Unsupported TPM application-file role in backup manifest.")
            if not extra.name.lower().endswith((".tar", ".tar.gz", ".tgz")):
                raise ValueError("TPM uploads must be supplied as a tar archive.")
            if not hmac.compare_digest(str(item.get("sha256") or "").lower(), sha256_file(extra)):
                raise ValueError("A TPM application-file checksum does not match its manifest.")
            files.append({"role": role, "path": str(extra), "sha256": sha256_file(extra)})
    return {
        "path": str(path.resolve()), "sha256": sha256_file(path),
        "tables": sorted(create_tables), "inserted_tables": sorted(inserted_tables),
        "server_version": server_match.group(1).strip() if server_match else "Unknown",
        "manifest": manifest_data, "manifest_path": str(manifest_path) if manifest_data else "",
        "files": files, "complete_set": bool(manifest_data),
    }


def inspect_n8n_backup(value):
    """Validate one complete Builder-generated n8n backup set."""
    selected = Path(str(value or "").strip())
    if not path_under_backup_root(selected) or selected.is_symlink() or not selected.is_file():
        raise ValueError("Select an n8n database backup below the configured backup root.")
    folder = selected.parent
    manifest_path = folder / "manifest.json"
    checksums_path = folder / "SHA256SUMS"
    try:
        metadata = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("n8n isolated restore requires a valid Builder backup manifest.") from exc
    if metadata.get("schema") != 2 or metadata.get("application") != "n8n":
        raise ValueError("n8n isolated restore requires backup manifest schema 2.")
    database = _safe_backup_member(folder, metadata.get("database"))
    files = _safe_backup_member(folder, metadata.get("files"))
    secrets_file = _safe_backup_member(folder, metadata.get("secrets"))
    if database != selected.resolve() or not database.name.endswith(".sql.gz"):
        raise ValueError("Selected n8n database dump does not match its manifest.")
    try:
        expected = {}
        for line in checksums_path.read_text(encoding="utf-8").splitlines():
            digest, name = line.split(None, 1)
            expected[name.lstrip(" *")] = digest.lower()
    except (OSError, ValueError) as exc:
        raise ValueError("n8n backup checksum inventory is missing or invalid.") from exc
    for member in (database, files, secrets_file):
        if not hmac.compare_digest(expected.get(member.name, ""), sha256_file(member)):
            raise ValueError(f"n8n backup checksum mismatch for {member.name}.")
    secret_values = read_simple_env_file(secrets_file)
    if not secret_values.get("N8N_ENCRYPTION_KEY"):
        raise ValueError("n8n backup set is missing its encryption key.")
    application_version = str(metadata.get("application_version") or "").strip()
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", application_version):
        raise ValueError("n8n backup manifest has no supported fixed application version.")
    return {
        "path": str(database), "sha256": sha256_file(database), "complete_set": True,
        "tables": [], "server_version": str(metadata.get("database_version") or "PostgreSQL 16"),
        "manifest": metadata, "files": [{"role": "n8n-data", "path": str(files)}],
        "secrets_path": str(secrets_file), "application_version": application_version,
    }


def classify_tpm_backup_choices(choices):
    result = []
    for value, label in choices:
        path = Path(value)
        if not path.name.lower().endswith((".sql", ".sql.gz")):
            continue
        text = f"{path.name} {path.parent.name}".lower()
        verified = (path.parent / TPM_BACKUP_MANIFEST).is_file()
        manifest_application = backup_manifest_application(path)
        likely = "tpm" in text or "team" in text
        if manifest_application not in {"", "teampasswordmanager"}:
            continue
        if not verified and manifest_application != "teampasswordmanager" and not likely:
            continue
        prefix = "Verified TPM set" if verified or manifest_application == "teampasswordmanager" else "TPM candidate"
        result.append((value, f"{prefix} · {label}"))
    return result


def backup_manifest_application(path):
    manifest_path = Path(path).parent / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return ""
    try:
        return str(json.loads(manifest_path.read_text(encoding="utf-8")).get("application") or "").strip().lower()
    except (OSError, json.JSONDecodeError):
        return ""


def classify_n8n_backup_choices(choices):
    result = []
    for value, label in choices:
        path = Path(value)
        if not path.name.lower().endswith(".sql.gz"):
            continue
        if backup_manifest_application(path) != "n8n":
            continue
        result.append((value, f"Verified n8n set · {label}"))
    return result


def ensure_application_images_available(profile, image, database_image):
    required = [image, validate_application_database_image(profile, database_image)]
    for required_image in required:
        present = subprocess.run(
            ["docker", "image", "inspect", required_image],
            capture_output=True, text=True, timeout=30,
        )
        if present.returncode == 0:
            continue
        raise RuntimeError(
            f"Required Docker image is not installed locally: {required_image}. "
            "Install the exact image in Container Manager first, then reopen the wizard."
        )


def validate_application_request(source):
    profile = get_profile(source.get("app_type"))
    project = validate_application_project(source.get("project"))
    host_port = validate_application_port(source.get("host_port"))
    image = validate_application_image(profile, source.get("image"))
    database_image = validate_application_database_image(
        profile, source.get("database_image") or profile.default_database_image
    )
    timezone = str(source.get("timezone") or TZ_DEFAULT).strip()
    deployment_mode = str(source.get("deployment_mode") or "fresh").strip().lower()
    if deployment_mode not in {"fresh", "quarantine"}:
        raise ValueError("Deployment mode must be Fresh installation or Isolated test restore.")
    quarantine = deployment_mode == "quarantine"
    database_backup = ""
    backup_version = ""
    backup_inspection = None
    bind_address = validate_bind_address(source.get("bind_address"), quarantine=quarantine)
    expires_at = ""
    if quarantine:
        if not profile.quarantine_restore:
            raise ValueError(f"{profile.name} has no verified isolated restore adapter yet.")
        database_backup = str(source.get("database_backup") or "").strip()
        backup_inspection = inspect_tpm_backup(database_backup) if profile.key == "teampasswordmanager" else inspect_n8n_backup(database_backup)
        backup_version = str(
            (backup_inspection.get("manifest") or {}).get("application_version")
            or backup_inspection.get("application_version")
            or "Unknown"
        ).strip()
        try:
            expiry_days = int(str(source.get("expiry_days") or QUARANTINE_DEFAULT_DAYS))
        except ValueError as exc:
            raise ValueError("Quarantine expiry must be a number of days.") from exc
        if not 1 <= expiry_days <= 90:
            raise ValueError("Quarantine expiry must be between 1 and 90 days.")
        expires_at = (datetime.now() + timedelta(days=expiry_days)).replace(microsecond=0).isoformat()
    if project_dir(project).exists():
        raise ValueError(
            f"Project directory already exists: {project_dir(project)}. "
            "Existing projects are never adopted or overwritten."
        )
    assert_docker_port_free(host_port)
    build_application_environment(
        profile, project, host_port, image, timezone,
        database_image=database_image, quarantine=quarantine,
        bind_address=bind_address, expires_at=expires_at,
    )
    return {
        "app_type": profile.key,
        "project": project,
        "host_port": host_port,
        "image": image,
        "database_image": database_image,
        "timezone": timezone,
        "quarantine": quarantine,
        "deployment_mode": deployment_mode,
        "database_backup": database_backup,
        "backup_version": backup_version,
        "backup_inspection": backup_inspection,
        "bind_address": bind_address,
        "expires_at": expires_at,
    }


def validate_quarantine_database_backup(value):
    return Path(inspect_tpm_backup(value)["path"])


@app.route("/applications/create", methods=["POST"])
def create_application():
    try:
        require_csrf()
        data = validate_application_request(request.form)
        token = secrets.token_urlsafe(32)
        session["pending_application_preview"] = {
            "token": token,
            "created_at": int(time.time()),
            "data": data,
        }
        session.modified = True
        return render_professional_page(
            APPLICATION_PREVIEW,
            "Review application",
            "projects",
            data=data,
            profile=get_profile(data["app_type"]),
            preview_token=token,
        )
    except Exception as exc:
        flash(str(exc), "err")
        return redirect(url_for("new_application_page"))


def consume_application_preview(token):
    pending = session.pop("pending_application_preview", None)
    session.modified = True
    if not pending or not hmac.compare_digest(str(pending.get("token", "")), str(token or "")):
        raise ValueError("The application preview is missing or invalid.")
    if int(pending.get("created_at") or 0) < int(time.time()) - CREATE_PREVIEW_TTL_SECONDS:
        raise ValueError("The application preview expired; review the deployment again.")
    return validate_application_request(pending["data"])


def write_private_application_files(data):
    profile = get_profile(data["app_type"])
    project = data["project"]
    folder = project_dir(project)
    if folder.exists():
        raise ValueError(f"Project directory already exists: {folder}")
    environment = build_application_environment(
        profile, project, data["host_port"], data["image"], data["timezone"],
        database_image=data.get("database_image") or profile.default_database_image,
        quarantine=bool(data.get("quarantine")),
        bind_address=data.get("bind_address") or "0.0.0.0",
        expires_at=data.get("expires_at") or "",
    )
    folder.mkdir(mode=0o700, parents=False, exist_ok=False)
    for volume in profile.volumes:
        volume_path = folder / volume
        volume_path.mkdir(mode=0o700)
        # Synology bind mounts are created by the manager container as root.
        # The official PostgreSQL/MySQL entrypoints and the application images
        # run as their own users, so they must be able to initialise an empty
        # host directory before they can apply their normal ownership.
        volume_path.chmod(0o777)
    env_text = "# Generated by Docker Application Manager. Keep private.\n" + "".join(
        f"{key}={value}\n" for key, value in environment.items()
    )
    atomic_write_text(folder / ".env", env_text, 0o600)
    atomic_write_text(
        folder / "docker-compose.yml",
        render_application_compose(profile, environment),
        0o600,
    )
    atomic_write_text(
        folder / MANIFEST_NAME,
        json.dumps({
            **build_application_manifest(profile, environment),
            "backup": ({
                "sha256": data["backup_inspection"]["sha256"],
                "complete_set": data["backup_inspection"]["complete_set"],
                "table_count": len(data["backup_inspection"]["tables"]),
                "source_server": data["backup_inspection"]["server_version"],
            } if data.get("backup_inspection") else None),
        }, indent=2) + "\n",
        0o600,
    )
    return profile


def application_database_diagnostics(project):
    """Return bounded, secret-free diagnostics for an unhealthy database."""
    database_service = f"{project}-db"
    try:
        result = subprocess.run(
            ["docker", "logs", "--tail", "40", database_service],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return "Database-container logs could not be read."
    output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
    output = re.sub(
        r"(?i)(password|passwd|pwd)(\s*[=:]\s*)([^\s'\"]+)",
        r"\1\2[redacted]",
        output,
    )
    return tail_text(output, 2500) or "No database-container logs were available."


def restore_quarantine_database(data):
    """Restore a validated SQL dump after its isolated database is healthy."""
    project = data["project"]
    folder = project_dir(project)
    database_service = f"{project}-db"
    started = subprocess.run(
        ["docker", "compose", "up", "-d", database_service], cwd=folder,
        capture_output=True, text=True, timeout=600,
    )
    if started.returncode:
        raise RuntimeError("Quarantine database start failed: " + tail_text(started.stderr, 3000))
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        health = subprocess.run(
            ["docker", "inspect", "--format={{.State.Health.Status}}", database_service],
            capture_output=True, text=True, timeout=15,
        )
        if health.returncode == 0 and health.stdout.strip() == "healthy":
            break
        time.sleep(2)
    else:
        raise RuntimeError("Quarantine database did not become healthy within 180 seconds.")
    backup = Path(data["backup_inspection"]["path"])
    opener = gzip.open if backup.name.lower().endswith(".gz") else open
    shell = ('exec psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
             if data["app_type"] == "n8n" else
             'exec mysql -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE"')
    command = ["docker", "compose", "exec", "-T", database_service, "sh", "-c", shell]
    process = subprocess.Popen(command, cwd=folder, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        with opener(backup, "rb") as source:
            shutil.copyfileobj(source, process.stdin, length=1024 * 1024)
        process.stdin.close()
        process.stdin = None
        stdout, stderr = process.communicate(timeout=900)
    except Exception:
        process.kill()
        process.wait()
        raise
    if process.returncode:
        raise RuntimeError("Quarantine database restore failed: " + tail_text(stderr.decode("utf-8", "replace"), 3000))


def restore_quarantine_application_files(data):
    inspection = data.get("backup_inspection") or {}
    for item in inspection.get("files") or []:
        if item.get("role") not in {"uploads", "n8n-data"}:
            raise RuntimeError("Unsupported application-file role reached restore execution.")
        destination = (project_dir(data["project"]) / "data" if item["role"] == "n8n-data"
                       else project_dir(data["project"]) / "application" / "site" / "uploads")
        destination.mkdir(mode=0o700, parents=True, exist_ok=True)
        safe_extract_tar(Path(item["path"]), destination)
    if data["app_type"] == "n8n":
        restored = read_simple_env_file(Path(inspection["secrets_path"]))
        environment_path = project_dir(data["project"]) / ".env"
        environment = read_simple_env_file(environment_path)
        environment["N8N_ENCRYPTION_KEY"] = restored["N8N_ENCRYPTION_KEY"]
        atomic_write_text(environment_path, "# Generated by Docker Application Manager. Keep private.\n" + "".join(f"{key}={value}\n" for key, value in environment.items()), 0o600)


def verify_quarantine_restore(data):
    project = data["project"]
    folder = project_dir(project)
    network = f"{project}-network"
    internal = subprocess.run(
        ["docker", "network", "inspect", network, "--format", "{{.Internal}}"],
        capture_output=True, text=True, timeout=30,
    )
    if internal.returncode or internal.stdout.strip().lower() != "true":
        raise RuntimeError("Quarantine proof failed: Docker network is not internal.")
    attached = subprocess.run(
        ["docker", "inspect", project, "--format", "{{json .NetworkSettings.Networks}}"],
        capture_output=True, text=True, timeout=30,
    )
    try:
        networks = json.loads(attached.stdout) if attached.returncode == 0 else {}
    except json.JSONDecodeError:
        networks = {}
    if set(networks) != {network}:
        raise RuntimeError("Quarantine proof failed: application is attached to an unexpected Docker network.")
    database_service = f"{project}-db"
    count_shell = ('exec psql -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=\'public\'"'
                   if data["app_type"] == "n8n" else
                   'exec mysql -N -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE" -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE()"')
    table_count = subprocess.run(
        ["docker", "compose", "exec", "-T", database_service, "sh", "-c", count_shell],
        cwd=folder, capture_output=True, text=True, timeout=60,
    )
    try:
        restored_tables = int(table_count.stdout.strip()) if table_count.returncode == 0 else 0
    except ValueError:
        restored_tables = 0
    expected_tables = len((data.get("backup_inspection") or {}).get("tables") or [])
    if not restored_tables or (expected_tables and restored_tables < expected_tables):
        raise RuntimeError("Quarantine proof failed: restored table count is incomplete.")
    inspection = data.get("backup_inspection") or {}
    report = {
        "schema": 1,
        "application": data["app_type"],
        "project": project,
        "created_at": datetime.now().replace(microsecond=0).isoformat(),
        "expires_at": data.get("expires_at") or "",
        "management_bind_address": data.get("bind_address"),
        "backup_sha256": inspection.get("sha256"),
        "backup_complete_set": bool(inspection.get("complete_set")),
        "source_server": inspection.get("server_version"),
        "source_tables": expected_tables,
        "restored_tables": restored_tables,
        "verified_application_archives": len(inspection.get("files") or []),
        "proof": {
            "dedicated_database": True,
            "new_credentials": True,
            "internal_network": True,
            "only_expected_network_attached": True,
            "management_interface_restricted": data.get("bind_address") not in {"0.0.0.0", "::"},
            "external_integrations_removed": False,
            "external_integrations_blocked_by_network": True,
        },
        "warning": "This copy still contains sensitive production data and integration settings.",
    }
    atomic_write_text(folder / QUARANTINE_REPORT, json.dumps(report, indent=2) + "\n", 0o600)
    return report


@app.route("/applications/create/execute", methods=["POST"])
def execute_application():
    project = None
    try:
        require_csrf()
        data = consume_application_preview(request.form.get("preview_token"))
        project = data["project"]
        preflight = synology_preflight()
        if not preflight["ready"]:
            failed = ", ".join(item["name"] for item in preflight["checks"] if item["status"] != "pass")
            raise RuntimeError("NAS preflight failed: " + failed)
        profile = get_profile(data["app_type"])
        ensure_application_images_available(profile, data["image"], data["database_image"])
        assert_docker_port_free(data["host_port"])
        profile = write_private_application_files(data)
        validation = subprocess.run(
            ["docker", "compose", "-f", "docker-compose.yml", "config", "--quiet"],
            cwd=project_dir(project), capture_output=True, text=True, timeout=60,
        )
        if validation.returncode:
            raise RuntimeError("Compose validation failed: " + tail_text(validation.stderr, 2000))
        if data.get("quarantine"):
            restore_quarantine_database(data)
            restore_quarantine_application_files(data)
        deployment = subprocess.run(
            ["docker", "compose", "-f", "docker-compose.yml", "up", "-d"],
            cwd=project_dir(project), capture_output=True, text=True, timeout=600,
        )
        if deployment.returncode:
            details = application_database_diagnostics(project)
            raise RuntimeError(
                "Application deployment failed: " + tail_text(deployment.stderr, 1800)
                + " Database log: " + details
            )
        report = verify_quarantine_restore(data) if data.get("quarantine") else None
        write_action_log(project, "application-deploy", [
            f"Application profile: {profile.name}",
            f"Image: {data['image']}",
            f"Web port: {data['host_port']}",
            "Mode: isolated test restore; external network blocked." if data.get("quarantine") else "Mode: fresh installation.",
            "Compose validation passed and containers were started.",
            (f"Quarantine proof passed: {report['restored_tables']} database tables restored." if report else "Fresh deployment preflight passed."),
        ])
        invalidate_dashboard_cache()
        flash(f"{profile.name} project {project} was deployed.", "ok")
        return redirect(url_for("project_detail_page", project=project))
    except Exception as exc:
        if project and project_dir(project).is_dir():
            try:
                subprocess.run(
                    ["docker", "compose", "down"], cwd=project_dir(project),
                    capture_output=True, text=True, timeout=120,
                )
            except Exception:
                pass
            try:
                write_action_log(project, "application-deploy-failed", [tail_text(exc, 3000)])
            except Exception:
                pass
        flash(str(exc), "err")
        return redirect(url_for("new_application_page"))


@app.route("/applications/action", methods=["POST"])
def application_lifecycle():
    project = None
    try:
        require_csrf()
        project = validate_application_project(request.form.get("project"))
        action = str(request.form.get("action") or "").strip().lower()
        commands = {
            "start": (["docker", "compose", "start"], "started"),
            "stop": (["docker", "compose", "stop"], "stopped"),
            "restart": (["docker", "compose", "restart"], "restarted"),
        }
        folder = project_dir(project)
        data = read_application_manifest(folder)
        if not data:
            raise ValueError("This is not a valid profile-managed application.")
        if data.get("quarantine") and action in {"start", "restart"}:
            try:
                if not data.get("expires_at") or datetime.fromisoformat(data["expires_at"]) <= datetime.now():
                    raise ValueError("This quarantine has expired and cannot be started; archive it and create a new test restore.")
            except ValueError as exc:
                if "expired" in str(exc):
                    raise
                raise ValueError("Quarantine expiry metadata is invalid; start is blocked.") from exc
        if action == "update":
            if data.get("quarantine"):
                raise ValueError("Quarantine projects cannot be updated in place; create a new version-matched test restore instead.")
            pull = subprocess.run(
                ["docker", "compose", "pull"], cwd=folder,
                capture_output=True, text=True, timeout=600,
            )
            if pull.returncode:
                raise RuntimeError("Image pull failed: " + tail_text(pull.stderr, 3000))
            command, past = ["docker", "compose", "up", "-d"], "updated"
        elif action in commands:
            command, past = commands[action]
        else:
            raise ValueError("Unsupported application lifecycle action.")
        result = subprocess.run(
            command, cwd=folder, capture_output=True, text=True, timeout=600,
        )
        if result.returncode:
            raise RuntimeError(f"Application {action} failed: " + tail_text(result.stderr, 3000))
        write_action_log(project, f"application-{action}", [
            f"Profile-managed application {past}.",
            f"Application type: {data['name']}",
        ])
        invalidate_dashboard_cache()
        flash(f"{data['name']} project {project} was {past}.", "ok")
    except Exception as exc:
        flash(str(exc), "err")
    return redirect(url_for("project_detail_page", project=project)) if project else redirect(url_for("projects_page"))


@app.route("/applications/quarantine/archive", methods=["POST"])
def archive_quarantine():
    project = None
    try:
        require_csrf()
        project = validate_application_project(request.form.get("project"))
        if not hmac.compare_digest(str(request.form.get("confirm_project") or ""), project):
            raise ValueError("Type the exact project name to archive this quarantine.")
        folder = project_dir(project)
        data = read_application_manifest(folder)
        if not data or not data.get("quarantine"):
            raise ValueError("Only a valid quarantine project can be archived here.")
        stopped = subprocess.run(
            ["docker", "compose", "down"], cwd=folder,
            capture_output=True, text=True, timeout=180,
        )
        if stopped.returncode:
            raise RuntimeError("Quarantine containers could not be stopped: " + tail_text(stopped.stderr, 2000))
        trash_root = BASE_PATH / ".docker-app-manager-trash"
        trash_root.mkdir(mode=0o700, exist_ok=True)
        destination = trash_root / f"{project}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        folder.rename(destination)
        audit = {
            "project": project, "archived_at": datetime.now().replace(microsecond=0).isoformat(),
            "recoverable_path": str(destination),
        }
        atomic_write_text(destination / "archive.json", json.dumps(audit, indent=2) + "\n", 0o600)
        invalidate_dashboard_cache()
        flash(f"Quarantine {project} was stopped and moved to recoverable archive storage.", "ok")
        return redirect(url_for("projects_page"))
    except Exception as exc:
        flash(str(exc), "err")
        return redirect(url_for("project_detail_page", project=project)) if project else redirect(url_for("projects_page"))


@app.route("/applications/<project>/quarantine-proof.json", methods=["GET"])
def download_quarantine_report(project):
    project = validate_application_project(project)
    folder = project_dir(project)
    manifest = read_application_manifest(folder)
    report_path = folder / QUARANTINE_REPORT
    if not manifest or not manifest.get("quarantine") or report_path.is_symlink() or not report_path.is_file():
        abort(404)
    response = make_response(report_path.read_text(encoding="utf-8"))
    response.headers["Content-Type"] = "application/json"
    response.headers["Content-Disposition"] = f'attachment; filename="{project}-quarantine-proof.json"'
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/projects/<project>", methods=["GET"])
def project_detail_page(project):
    project = validate_project(project)
    _docker_snapshot, projects = professional_ui_snapshot()
    selected = next((item for item in projects if item["name"] == project), None)
    if not selected:
        abort(404)
    if selected.get("profile_managed"):
        return render_professional_page(
            APPLICATION_DETAIL,
            project,
            "projects",
            project=selected,
            profile=get_profile(selected["app_type"]),
        )
    return render_professional_page(
        PROJECT_DETAIL,
        project,
        "projects",
        project=selected,
    )


@app.route("/projects/<project>/compose", methods=["GET"])
def project_compose_page(project):
    project = validate_project(project)
    _docker_snapshot, projects = professional_ui_snapshot()
    if not any(item["name"] == project for item in projects):
        abort(404)
    selected = next(item for item in projects if item["name"] == project)
    if selected.get("simulated") and test_demo_is_active():
        compose_yaml = sanitized_compose_yaml(demo_compose_yaml(project))
    else:
        path = compose_file(project)
        if not path.is_file():
            abort(404)
        compose_yaml = sanitized_compose_yaml(path.read_text(encoding="utf-8"))
    if request.args.get("download") == "1":
        response = make_response(compose_yaml)
        response.headers["Content-Type"] = "application/yaml; charset=utf-8"
        response.headers["Content-Disposition"] = (
            f'attachment; filename="{project}-docker-compose.sanitized.yml"'
        )
        return response
    return render_professional_page(
        COMPOSE_VIEW,
        f"{project} YAML",
        "projects",
        project=project,
        compose_yaml=compose_yaml,
    )


@app.route("/backups", methods=["GET"])
def backups_page():
    backup_choices = (
        demo_backup_choices()
        if test_demo_is_active()
        else scan_backup_choices(BACKUP_ROOT, include_dirs=True)
    )
    _docker_snapshot, projects = professional_ui_snapshot()
    inventory = backup_inventory(
        backup_choices["database"],
        backup_choices["files"],
        demo=test_demo_is_active(),
    )
    return render_professional_page(
        BACKUPS,
        "Backups",
        "backups",
        projects=projects,
        backup_root=(
            "/demo-only/backups"
            if test_demo_is_active()
            else str(BACKUP_ROOT)
        ),
        db_backups=backup_choices["database"],
        file_backups=backup_choices["files"],
        inventory=inventory,
        complete_pairs=len({
            item["pair_key"] for item in inventory if item["complete_pair"]
        }),
        verified_at=(
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if request.args.get("verify") == "1" else ""
        ),
        stale_days=BACKUP_STALE_DAYS,
        dispatcher_command=f"/bin/bash {BACKUP_DISPATCHER_PATH}",
        dispatcher_healthy=bool(
            test_demo_is_active() or dispatcher_is_healthy()
        ),
        legacy_backup_config=(
            not test_demo_is_active()
            and BACKUP_ENV_PATH.is_file()
        ),
    )


def recent_activity(projects, selected_project=None, limit=80):
    activity = []
    for project in projects:
        if selected_project and project["name"] != selected_project:
            continue
        for filename in list_logs(project["name"], limit=20):
            match = re.match(
                r"^(?P<date>[0-9]{8})-(?P<time>[0-9]{6})-(?P<action>.+)\.log$",
                filename,
            )
            label = filename
            shown_time = ""
            if match:
                action = match.group("action").replace("-", " ").replace("_", " ")
                label = action.capitalize()
                try:
                    stamp = datetime.strptime(
                        match.group("date") + match.group("time"), "%Y%m%d%H%M%S"
                    )
                    shown_time = stamp.strftime("%Y-%m-%d %H:%M")
                except ValueError:
                    pass
            activity.append({
                "project": project["name"],
                "filename": filename,
                "label": label,
                "time": shown_time,
            })
    return sorted(
        activity, key=lambda item: item["filename"], reverse=True
    )[:limit]


@app.route("/activity", methods=["GET"])
def activity_page():
    selected_project = request.args.get("project", "").strip() or None
    if selected_project:
        selected_project = validate_project(selected_project)
    _docker_snapshot, projects = professional_ui_snapshot()
    if test_demo_is_active():
        activities = [
            {
                "project": "demo-production",
                "filename": "20260727-201500-demo-action.log",
                "label": "Scheduled backup completed",
                "time": "2026-07-27 20:15",
                "simulated": True,
            },
            {
                "project": "demo-staging",
                "filename": "20260727-200100-health-check.log",
                "label": "Health check passed",
                "time": "2026-07-27 20:01",
                "simulated": True,
            },
            {
                "project": "demo-recovery",
                "filename": "20260727-194500-diagnostics.log",
                "label": "Diagnostics found a stopped GLPI container",
                "time": "2026-07-27 19:45",
                "simulated": True,
            },
        ]
        if selected_project:
            activities = [
                item for item in activities
                if item["project"] == selected_project
            ]
    else:
        activities = recent_activity(projects, selected_project)
    return render_professional_page(
        ACTIVITY,
        "Activity",
        "activity",
        activities=activities,
        selected_project=selected_project,
    )


@app.route("/settings", methods=["GET"])
def settings_page():
    auth = {
        "username": AUTH_CONFIG.username,
        "idle_minutes": AUTH_CONFIG.session_timeout_seconds // 60,
        "absolute_hours": AUTH_CONFIG.session_absolute_timeout_seconds // 3600,
    }
    return render_professional_page(
        SETTINGS,
        "Settings",
        "settings",
        auth=auth,
        base_path=str(BASE_PATH),
        backup_root=str(BACKUP_ROOT),
        backup_data_root=str(BACKUP_DATA_ROOT),
        tz_default=TZ_DEFAULT,
        cookie_secure=app.config["SESSION_COOKIE_SECURE"],
        glpi_prefixes=ALLOWED_GLPI_IMAGES,
        db_prefixes=ALLOWED_DB_IMAGES,
        request_line_limit=APACHE_REQUEST_LINE_LIMIT,
        request_line_kib=APACHE_REQUEST_LINE_LIMIT // 1024,
        glpi_internal_port=GLPI_INTERNAL_PORT,
        default_cookie_samesite=DEFAULT_SESSION_COOKIE_SAMESITE,
        default_cookie_secure=DEFAULT_SESSION_COOKIE_SECURE,
        max_scan_entries=MAX_SCAN_ENTRIES,
        operation_modes=OPERATION_MODES,
    )


@app.after_request
def v11_security_headers(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"
    return response


if __name__ == "__main__":
    BASE_PATH.mkdir(parents=True, exist_ok=True)
    app.run(host="0.0.0.0", port=APP_PORT)
