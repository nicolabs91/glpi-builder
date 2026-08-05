#!/bin/sh
set -eu

OLD_DIR="${GLPI_BUILDER_OLD_DIR:-/volume1/docker/glpi-builder}"
NEW_DIR="${DOCKER_APP_MANAGER_APP_DIR:-/volume1/docker/docker-app-manager}"
OLD_AUTH_STATE="${GLPI_BUILDER_AUTH_STATE_PATH:-/volume1/docker/.glpi-builder-auth-state}"
NEW_AUTH_STATE="${DOCKER_APP_MANAGER_AUTH_STATE_PATH:-/volume1/docker/.docker-app-manager-auth-state}"

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

as_root() {
  if [ "${DOCKER_APP_MANAGER_MIGRATION_TESTING:-0}" = "1" ]; then
    "$@"
  else
    sudo "$@"
  fi
}

if [ "${DOCKER_APP_MANAGER_MIGRATION_TESTING:-0}" != "1" ]; then
  case "$OLD_DIR" in /volume*/docker/glpi-builder) ;; *) fail "Unexpected legacy application directory: $OLD_DIR" ;; esac
  case "$NEW_DIR" in /volume*/docker/docker-app-manager) ;; *) fail "Unexpected new application directory: $NEW_DIR" ;; esac
fi
[ ! -L "$OLD_DIR" ] || fail "Legacy application directory must not be a symlink."
[ ! -L "$NEW_DIR" ] || fail "New application directory must not be a symlink."
[ -d "$OLD_DIR" ] || fail "Legacy installation not found: $OLD_DIR"
[ -d "$NEW_DIR" ] || fail "Extract version 0.4.1 or newer first: $NEW_DIR"
[ -f "$NEW_DIR/install_on_synology.sh" ] || fail "The new release is incomplete."
[ ! -L "$OLD_DIR/.env" ] || fail "Legacy .env must not be a symlink."
[ ! -L "$OLD_DIR/config" ] || fail "Legacy config directory must not be a symlink."

if [ -e "$NEW_DIR/.env" ]; then
  fail "The new directory already contains .env; refusing to overwrite configuration."
fi
if [ -e "$NEW_DIR/config" ]; then
  fail "The new directory already contains config; refusing to merge authentication data."
fi

if [ -f "$OLD_DIR/.env" ] && [ -f "$OLD_DIR/docker-compose.app.yml" ]; then
  as_root docker compose --project-name glpi-builder --env-file "$OLD_DIR/.env" \
    -f "$OLD_DIR/docker-compose.app.yml" stop >/dev/null 2>&1 || true
elif [ -f "$OLD_DIR/docker-compose.container-manager.yml" ]; then
  as_root docker compose --project-name glpi-builder \
    -f "$OLD_DIR/docker-compose.container-manager.yml" stop >/dev/null 2>&1 || true
fi
as_root docker stop glpi-builder >/dev/null 2>&1 || true

if [ -f "$OLD_DIR/.env" ]; then
  as_root cp -p "$OLD_DIR/.env" "$NEW_DIR/.env"
  as_root chmod 600 "$NEW_DIR/.env"
fi
if [ -d "$OLD_DIR/config" ]; then
  as_root cp -pR "$OLD_DIR/config" "$NEW_DIR/config"
fi
if [ -f "$OLD_AUTH_STATE" ] && [ ! -L "$OLD_AUTH_STATE" ] && [ ! -e "$NEW_AUTH_STATE" ]; then
  as_root cp -p "$OLD_AUTH_STATE" "$NEW_AUTH_STATE"
  as_root chmod 600 "$NEW_AUTH_STATE"
fi

if [ -f "$NEW_DIR/.env" ]; then
  as_root python3 - "$NEW_DIR/.env" <<'PY'
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
old = "BUILDER_AUTH_STATE_PATH=/volume1/docker/.glpi-builder-auth-state"
new = "BUILDER_AUTH_STATE_PATH=/volume1/docker/.docker-app-manager-auth-state"
lines = path.read_text(encoding="utf-8").splitlines()
path.write_text("\n".join(new if line == old else line for line in lines) + "\n", encoding="utf-8")
os.chmod(path, 0o600)
PY
fi

echo "Configuration copied. Starting the renamed Docker App Manager installation."
if [ -f "$NEW_DIR/.env" ]; then
  as_root sh "$NEW_DIR/install_on_synology.sh" "$NEW_DIR"
else
  OLD_CONTAINER=""
  if as_root docker container inspect glpi-builder >/dev/null 2>&1; then
    OLD_CONTAINER="docker-app-manager-pre-rename-$(date +%Y%m%d-%H%M%S)"
    as_root docker rename glpi-builder "$OLD_CONTAINER"
  fi
  as_root docker compose --project-name docker-app-manager \
    -f "$NEW_DIR/docker-compose.container-manager.yml" up -d --build --force-recreate
  HEALTHY=""
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    if as_root docker exec docker-app-manager python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)" >/dev/null 2>&1; then
      HEALTHY="yes"
      break
    fi
    sleep "${MIGRATION_HEALTH_DELAY_SECONDS:-2}"
  done
  if [ "$HEALTHY" != "yes" ]; then
    as_root docker logs --tail 100 docker-app-manager >&2 || true
    as_root docker rm -f docker-app-manager >/dev/null 2>&1 || true
    if [ -n "$OLD_CONTAINER" ]; then
      as_root docker rename "$OLD_CONTAINER" glpi-builder
      as_root docker start glpi-builder >/dev/null
      fail "The renamed container did not become healthy. The legacy glpi-builder container was restored."
    fi
    fail "The renamed container did not become healthy. No legacy container was available to restore."
  fi
fi
echo "Migration completed. Keep $OLD_DIR until you have verified login, applications, and backups."
