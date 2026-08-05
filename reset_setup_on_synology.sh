#!/bin/sh
set -eu

APP_DIR="${DOCKER_APP_MANAGER_APP_DIR:-${GLPI_BUILDER_APP_DIR:-/volume1/docker/docker-app-manager}}"
CONFIG_DIR="$APP_DIR/config"
AUTH_FILE="$CONFIG_DIR/builder-auth.json"
AUTH_STATE="${BUILDER_AUTH_STATE_PATH:-/volume1/docker/.docker-app-manager-auth-state}"
RECOVERY_ROOT="$CONFIG_DIR/recovery-backups"

if [ "${1:-}" != "--confirm-reset" ]; then
  echo "Refusing to reset authentication without --confirm-reset." >&2
  echo "Stop the docker-app-manager project, then run: $0 --confirm-reset" >&2
  exit 2
fi

case "$APP_DIR" in
  /volume*/docker/docker-app-manager) ;;
  *)
    if [ "${GLPI_BUILDER_TESTING:-}" = "1" ]; then
      :
    else
      echo "Refusing unexpected Docker App Manager directory: $APP_DIR" >&2
      exit 2
    fi
    ;;
esac

if [ -L "$APP_DIR" ] || [ -L "$CONFIG_DIR" ] || [ -L "$AUTH_FILE" ] || [ -L "$AUTH_STATE" ] || [ -L "$RECOVERY_ROOT" ]; then
  echo "Refusing authentication reset because a managed path is a symbolic link." >&2
  exit 2
fi

if [ ! -f "$AUTH_FILE" ]; then
  echo "No authentication file exists at $AUTH_FILE; nothing was reset." >&2
  exit 1
fi

STAMP=$(date +%Y%m%d-%H%M%S)
RECOVERY_DIR="$RECOVERY_ROOT/$STAMP"
if [ -e "$RECOVERY_DIR" ]; then
  echo "Refusing to overwrite existing recovery backup: $RECOVERY_DIR" >&2
  exit 2
fi

umask 077
mkdir -p "$RECOVERY_DIR"
chmod 700 "$CONFIG_DIR" "$CONFIG_DIR/recovery-backups" "$RECOVERY_DIR"
mv "$AUTH_FILE" "$RECOVERY_DIR/builder-auth.json"
chmod 600 "$RECOVERY_DIR/builder-auth.json"

if [ -f "$AUTH_STATE" ]; then
  mv "$AUTH_STATE" "$RECOVERY_DIR/totp-replay-state"
  chmod 600 "$RECOVERY_DIR/totp-replay-state"
fi

echo "Authentication was reset safely."
echo "Backup: $RECOVERY_DIR"
echo "Start docker-app-manager and copy the new setup token from the container log."
