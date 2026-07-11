#!/bin/bash
# Managed by GLPI Project Builder. Project-specific values live in GLPI_backup.env.
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

for name in PROJECT_NAME PROJECT_DIR DB_CONTAINER DB_NAME BACKUP_ROOT MYSQL_CNF CONTAINER_CNF RETENTION_DAYS; do
  [ -n "${!name:-}" ] || fail "Required setting is missing from $ENV_FILE: $name"
done

case "$PROJECT_NAME" in *[!a-z0-9_-]*|'') fail "Invalid PROJECT_NAME in $ENV_FILE" ;; esac
case "$RETENTION_DAYS" in *[!0-9]*|'') fail "RETENTION_DAYS must be a non-negative number" ;; esac
case "$PROJECT_DIR" in /volume1/docker/*) ;; *) fail "PROJECT_DIR must be below /volume1/docker" ;; esac
case "$BACKUP_ROOT" in /volume1/docker/*) ;; *) fail "BACKUP_ROOT must be below /volume1/docker" ;; esac

GLPI_DATA_DIR="$PROJECT_DIR/glpi"
GLPI_PLUGINS_DIR="$PROJECT_DIR/plugins"
[ -f "$MYSQL_CNF" ] || fail "Credential file not found: $MYSQL_CNF"
[ -d "$GLPI_DATA_DIR" ] || fail "GLPI data folder not found: $GLPI_DATA_DIR"
[ -d "$GLPI_PLUGINS_DIR" ] || fail "GLPI plugins folder not found: $GLPI_PLUGINS_DIR"

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

mkdir -p "$BACKUP_ROOT"
LOCK_DIR="$SCRIPT_DIR/.GLPI_backup.lock"
mkdir "$LOCK_DIR" 2>/dev/null || fail "Another GLPI backup is already running"

STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_DIR="$BACKUP_ROOT/GLPI_Backup_$STAMP"
TEMP_BACKUP_DIR="$BACKUP_ROOT/.GLPI_Backup_$STAMP.partial.$$"
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
echo "Starting GLPI backup for $PROJECT_NAME: $STAMP"

echo "Copying the credential file temporarily to $DB_CONTAINER..."
"${DOCKER[@]}" cp "$MYSQL_CNF" "$DB_CONTAINER:$CONTAINER_CNF"
CNF_COPIED=1

echo "Creating database dump..."
"${DOCKER[@]}" exec "$DB_CONTAINER" \
  mariadb-dump \
  --defaults-extra-file="$CONTAINER_CNF" \
  --single-transaction \
  --quick \
  "$DB_NAME" \
  > "$TEMP_BACKUP_DIR/glpi-database.sql"
[ -s "$TEMP_BACKUP_DIR/glpi-database.sql" ] || fail "Database dump is empty"

echo "Creating portable GLPI files archive..."
tar -C "$PROJECT_DIR" -czf "$TEMP_BACKUP_DIR/glpi-files.tar.gz" \
  --exclude="glpi/files/_sessions" \
  --exclude="glpi/files/_cache" \
  --exclude="glpi/files/_tmp" \
  glpi plugins
[ -s "$TEMP_BACKUP_DIR/glpi-files.tar.gz" ] || fail "GLPI files archive is empty"

if command -v sha256sum >/dev/null 2>&1; then
  (cd "$TEMP_BACKUP_DIR" && sha256sum glpi-database.sql glpi-files.tar.gz > SHA256SUMS)
elif command -v shasum >/dev/null 2>&1; then
  (cd "$TEMP_BACKUP_DIR" && shasum -a 256 glpi-database.sql glpi-files.tar.gz > SHA256SUMS)
fi

mv "$TEMP_BACKUP_DIR" "$BACKUP_DIR"
TEMP_BACKUP_DIR=""

echo "Removing GLPI backups older than $RETENTION_DAYS days..."
find "$BACKUP_ROOT" \
  -mindepth 1 \
  -maxdepth 1 \
  -type d \
  -name "GLPI_Backup_*" \
  -mtime "+$RETENTION_DAYS" \
  -exec rm -rf -- {} +

echo "GLPI backup completed: $BACKUP_DIR"
