#!/bin/bash
# Managed by Docker App Manager. Supports verified GLPI, n8n and TPM backup adapters.
set -Eeuo pipefail
umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ENV_FILE=${GLPI_BACKUP_ENV:-"$SCRIPT_DIR/GLPI_backup.env"}

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

[ -f "$ENV_FILE" ] || fail "Backup configuration not found: $ENV_FILE"
# The updater generates this file with mode 600 and validated values.
# shellcheck disable=SC1090
. "$ENV_FILE"
APP_TYPE=${APP_TYPE:-glpi}
DATA_PATHS=${DATA_PATHS:-glpi,plugins}

for name in PROJECT_NAME APP_TYPE PROJECT_DIR DB_CONTAINER DB_NAME BACKUP_ROOT DATA_PATHS RETENTION_DAYS; do
  [ -n "${!name:-}" ] || fail "Required setting is missing from $ENV_FILE: $name"
done

case "$PROJECT_NAME" in *[!a-z0-9_-]*|'') fail "Invalid PROJECT_NAME in $ENV_FILE" ;; esac
case "$RETENTION_DAYS" in *[!0-9]*|'') fail "RETENTION_DAYS must be a non-negative number" ;; esac
case "$PROJECT_DIR" in /volume1/docker/*) ;; *) fail "PROJECT_DIR must be below /volume1/docker" ;; esac
case "$BACKUP_ROOT" in /volume1/docker/*) ;; *) fail "BACKUP_ROOT must be below /volume1/docker" ;; esac

GLPI_DATA_DIR="$PROJECT_DIR/glpi"
GLPI_PLUGINS_DIR="$PROJECT_DIR/plugins"
case "$APP_TYPE" in
  glpi)
    [ -f "${MYSQL_CNF:-}" ] || fail "Credential file not found: ${MYSQL_CNF:-missing}"
    [ -d "$GLPI_DATA_DIR" ] || fail "GLPI data folder not found: $GLPI_DATA_DIR"
    [ -d "$GLPI_PLUGINS_DIR" ] || fail "GLPI plugins folder not found: $GLPI_PLUGINS_DIR"
    ;;
  n8n) [ -d "$PROJECT_DIR/data" ] || fail "n8n data folder not found: $PROJECT_DIR/data" ;;
  teampasswordmanager) [ -d "$PROJECT_DIR/application" ] || fail "TPM application folder not found: $PROJECT_DIR/application" ;;
  *) fail "Unsupported application backup adapter: $APP_TYPE" ;;
esac

if docker info >/dev/null 2>&1; then
  DOCKER=(docker)
elif command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
  DOCKER=(sudo docker)
else
  fail "Docker is not available for this scheduled task"
fi

"${DOCKER[@]}" inspect "$DB_CONTAINER" >/dev/null 2>&1 || fail "Database container not found: $DB_CONTAINER"
RUNNING=$("${DOCKER[@]}" inspect --format '{{.State.Running}}' "$DB_CONTAINER")
[ "$RUNNING" = "true" ] || fail "Database container is not running: $DB_CONTAINER"

PROJECT_BACKUP_ROOT="$BACKUP_ROOT/$PROJECT_NAME"
[ ! -L "$PROJECT_BACKUP_ROOT" ] || fail "Project backup folder must not be a symlink: $PROJECT_BACKUP_ROOT"
mkdir -p "$PROJECT_BACKUP_ROOT"
chmod 0750 "$PROJECT_BACKUP_ROOT"
[ -w "$PROJECT_BACKUP_ROOT" ] && [ -x "$PROJECT_BACKUP_ROOT" ] || fail "Project backup folder is not writable: $PROJECT_BACKUP_ROOT"
LOCKS_DIR="$SCRIPT_DIR/locks"
[ ! -L "$LOCKS_DIR" ] || fail "Backup locks folder must not be a symlink: $LOCKS_DIR"
mkdir -p "$LOCKS_DIR"
chmod 0700 "$LOCKS_DIR"
LOCK_DIR="$LOCKS_DIR/backup.lock"
mkdir "$LOCK_DIR" 2>/dev/null || fail "Another GLPI backup is already running (shared application backup lock)"

STAMP=$(date +%Y-%m-%d_%H%M%S)
BACKUP_DIR="$PROJECT_BACKUP_ROOT/$STAMP"
TEMP_BACKUP_DIR="$PROJECT_BACKUP_ROOT/.$STAMP.partial.$$"
CNF_COPIED=0

cleanup() {
  if [ "$CNF_COPIED" -eq 1 ]; then
    "${DOCKER[@]}" exec "$DB_CONTAINER" rm -f "$CONTAINER_CNF" >/dev/null 2>&1 || true
  fi
  if [ -n "${TEMP_BACKUP_DIR:-}" ] && [ -d "$TEMP_BACKUP_DIR" ]; then
    rm -rf "$TEMP_BACKUP_DIR"
  fi
  rmdir "$LOCK_DIR" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

mkdir "$TEMP_BACKUP_DIR"
echo "Starting $APP_TYPE backup for $PROJECT_NAME: $STAMP"
CREATED_AT=$(date '+%Y-%m-%dT%H:%M:%S%z')

echo "Creating database dump..."
case "$APP_TYPE" in
  glpi)
    echo "Copying the credential file temporarily to $DB_CONTAINER..."
    "${DOCKER[@]}" cp "$MYSQL_CNF" "$DB_CONTAINER:$CONTAINER_CNF"
    CNF_COPIED=1
    "${DOCKER[@]}" exec "$DB_CONTAINER" mariadb-dump --defaults-extra-file="$CONTAINER_CNF" --single-transaction --quick "$DB_NAME" | gzip -c > "$TEMP_BACKUP_DIR/database.sql.gz"
    ;;
  teampasswordmanager)
    "${DOCKER[@]}" exec "$DB_CONTAINER" sh -c 'exec mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" --single-transaction --quick "$MYSQL_DATABASE"' | gzip -c > "$TEMP_BACKUP_DIR/database.sql.gz"
    ;;
  n8n)
    "${DOCKER[@]}" exec "$DB_CONTAINER" sh -c 'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists' | gzip -c > "$TEMP_BACKUP_DIR/database.sql.gz"
    ;;
esac
[ -s "$TEMP_BACKUP_DIR/database.sql.gz" ] || fail "Database dump is empty"

echo "Creating portable $APP_TYPE files archive..."
case "$APP_TYPE" in
  glpi) tar -C "$PROJECT_DIR" -czf "$TEMP_BACKUP_DIR/files.tar.gz" --exclude="glpi/files/_sessions" --exclude="glpi/files/_cache" --exclude="glpi/files/_tmp" glpi plugins ;;
  n8n) tar -C "$PROJECT_DIR" -czf "$TEMP_BACKUP_DIR/files.tar.gz" data ;;
  teampasswordmanager) tar -C "$PROJECT_DIR" -czf "$TEMP_BACKUP_DIR/files.tar.gz" application ;;
esac
[ -s "$TEMP_BACKUP_DIR/files.tar.gz" ] || fail "GLPI files archive is empty"

EXTRA_CHECKSUM_FILE=""
MANIFEST_SCHEMA=1
APP_VERSION=${APP_IMAGE##*:}
DB_VERSION=${DB_IMAGE:-unknown}
DB_VERSION=${DB_VERSION##*:}
if [ "$APP_TYPE" = "n8n" ]; then
  [ -n "${N8N_ENCRYPTION_KEY:-}" ] || fail "n8n encryption key is missing from the private backup configuration"
  printf 'N8N_ENCRYPTION_KEY=%s\n' "$N8N_ENCRYPTION_KEY" > "$TEMP_BACKUP_DIR/secrets.env"
  chmod 0600 "$TEMP_BACKUP_DIR/secrets.env"
  EXTRA_CHECKSUM_FILE=" secrets.env"
  MANIFEST_SCHEMA=2
fi

if command -v sha256sum >/dev/null 2>&1; then
  (cd "$TEMP_BACKUP_DIR" && sha256sum database.sql.gz files.tar.gz $EXTRA_CHECKSUM_FILE > SHA256SUMS)
elif command -v shasum >/dev/null 2>&1; then
  (cd "$TEMP_BACKUP_DIR" && shasum -a 256 database.sql.gz files.tar.gz $EXTRA_CHECKSUM_FILE > SHA256SUMS)
fi
if [ "$APP_TYPE" = "n8n" ]; then
  printf '{\n  "schema": %s,\n  "application": "%s",\n  "application_version": "%s",\n  "database_version": "PostgreSQL 16",\n  "project": "%s",\n  "created_at": "%s",\n  "database": "database.sql.gz",\n  "files": "files.tar.gz",\n  "secrets": "secrets.env",\n  "checksums": "SHA256SUMS"\n}\n' \
    "$MANIFEST_SCHEMA" "$APP_TYPE" "$APP_VERSION" "$PROJECT_NAME" "$CREATED_AT" > "$TEMP_BACKUP_DIR/manifest.json"
else
  printf '{\n  "schema": 1,\n  "application": "%s",\n  "application_version": "%s",\n  "database_version": "%s",\n  "project": "%s",\n  "created_at": "%s",\n  "database": "database.sql.gz",\n  "files": "files.tar.gz",\n  "checksums": "SHA256SUMS"\n}\n' \
    "$APP_TYPE" "$APP_VERSION" "$DB_VERSION" "$PROJECT_NAME" "$CREATED_AT" > "$TEMP_BACKUP_DIR/manifest.json"
fi

mv "$TEMP_BACKUP_DIR" "$BACKUP_DIR"
TEMP_BACKUP_DIR=""

echo "Removing $APP_TYPE backups older than $RETENTION_DAYS days..."
find "$PROJECT_BACKUP_ROOT" \
  -mindepth 1 \
  -maxdepth 1 \
  -type d \
  -name "20??-??-??_??????" \
  -mtime "+$RETENTION_DAYS" \
  -exec rm -rf -- {} +

echo "$APP_TYPE backup completed: $BACKUP_DIR"
