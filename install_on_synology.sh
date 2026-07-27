#!/bin/sh
set -eu

APP_DIR="${1:-/volume1/docker/glpi-builder}"
IMAGE="glpi-builder:latest"
CONTAINER="glpi-builder"
COMPOSE_PROJECT="glpi-builder"
COMPOSE_FILE="$APP_DIR/docker-compose.app.yml"
LEGACY_CONTAINER="glpi-project-builder-full-restore"
STATE_FILE="$APP_DIR/.last-install-state"
BACKUP_TASK_DIR="/volume1/docker/_BACKUPS/Restore_Scripts/GLPI"
BACKUP_SCRIPT="$BACKUP_TASK_DIR/GLPI_backup.sh"
BACKUP_DISPATCHER="$BACKUP_TASK_DIR/GLPI_backup_dispatcher.sh"

cd "$APP_DIR"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Configuration created: $APP_DIR/.env"
fi

CURRENT_SECRET=$(sed -n 's/^FLASK_SECRET_KEY=//p' .env | head -n 1)
if [ -z "$CURRENT_SECRET" ] || [ "$CURRENT_SECRET" = "CHANGE_ME_RANDOM_64_HEX" ]; then
  SECRET=$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')
  if grep -q '^FLASK_SECRET_KEY=' .env; then
    sed -i "s|^FLASK_SECRET_KEY=.*|FLASK_SECRET_KEY=$SECRET|" .env
  else
    printf '\nFLASK_SECRET_KEY=%s\n' "$SECRET" >> .env
  fi
  echo "Internal session key generated automatically."
fi
chmod 600 .env

# Older installations may store the PBKDF2 hash without quotes. Docker Compose
# then treated its "$" separators as variable references and passed a broken
# hash to the container, making /healthz return 503. Migrate it in place while
# preserving the exact hash.
HASH_LINE=$(sed -n 's/^BUILDER_ADMIN_PASSWORD_HASH=//p' .env | head -n 1)
case "$HASH_LINE" in
  ""|\'*\') ;;
  *)
    ESCAPED_HASH=$(printf '%s' "$HASH_LINE" | sed "s/'/'\\\\''/g")
    sed -i "s|^BUILDER_ADMIN_PASSWORD_HASH=.*|BUILDER_ADMIN_PASSWORD_HASH='$ESCAPED_HASH'|" .env
    echo "Existing administrator password hash made Compose-safe."
    ;;
esac

# Migrate the pre-rename replay-state path without changing credentials.
if grep -q '^BUILDER_AUTH_STATE_PATH=/volume1/docker/.glpi-project-builder-auth-state$' .env; then
  sed -i 's|^BUILDER_AUTH_STATE_PATH=/volume1/docker/.glpi-project-builder-auth-state$|BUILDER_AUTH_STATE_PATH=/volume1/docker/.glpi-builder-auth-state|' .env
fi

if ! python3 "$APP_DIR/scripts/provision_admin.py" --env "$APP_DIR/.env" --check; then
  echo "Starting the administrator, password, bind-IP, and OTP setup wizard."
  if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
    sudo chown "$SUDO_USER" .env
    sudo -u "$SUDO_USER" python3 "$APP_DIR/scripts/provision_admin.py" --env "$APP_DIR/.env"
  else
    python3 "$APP_DIR/scripts/provision_admin.py" --env "$APP_DIR/.env"
  fi
  python3 "$APP_DIR/scripts/provision_admin.py" --env "$APP_DIR/.env" --check
fi

sudo mkdir -p "$BACKUP_TASK_DIR"
if [ -f "$BACKUP_SCRIPT" ] && ! sudo grep -Eq "Managed by GLPI (Project )?Builder" "$BACKUP_SCRIPT"; then
  if [ ! -f "$BACKUP_TASK_DIR/GLPI_backup.pre-builder.sh" ]; then
    sudo cp -p "$BACKUP_SCRIPT" "$BACKUP_TASK_DIR/GLPI_backup.pre-builder.sh"
    sudo chmod 700 "$BACKUP_TASK_DIR/GLPI_backup.pre-builder.sh"
  fi
fi
sudo cp "$APP_DIR/backup/GLPI_backup.sh" "$BACKUP_SCRIPT"
sudo cp "$APP_DIR/backup/GLPI_backup_dispatcher.sh" "$BACKUP_DISPATCHER"
sudo chmod 750 "$BACKUP_SCRIPT" "$BACKUP_DISPATCHER"
echo "Backup script installed: $BACKUP_SCRIPT"
echo "Backup dispatcher installed: $BACKUP_DISPATCHER"
echo "Fixed Task Scheduler command: /bin/bash $BACKUP_DISPATCHER"

sudo docker build -t "$IMAGE" .
sudo docker run --rm "$IMAGE" docker --version
sudo docker run --rm "$IMAGE" python tests/test_yaml_contract.py
sudo docker run --rm "$IMAGE" python tests/test_static_security.py

OLD_NAME=""
if sudo docker container inspect "$CONTAINER" >/dev/null 2>&1; then
  OLD_NAME="${CONTAINER}-pre-upgrade-$(date +%Y%m%d-%H%M%S)"
  sudo docker stop "$CONTAINER" >/dev/null 2>&1 || true
  sudo docker rename "$CONTAINER" "$OLD_NAME"
elif sudo docker container inspect "$LEGACY_CONTAINER" >/dev/null 2>&1; then
  OLD_NAME="${CONTAINER}-pre-rename-$(date +%Y%m%d-%H%M%S)"
  sudo docker stop "$LEGACY_CONTAINER" >/dev/null 2>&1 || true
  sudo docker rename "$LEGACY_CONTAINER" "$OLD_NAME"
fi
printf 'OLD_CONTAINER=%s\nIMAGE=%s\n' "$OLD_NAME" "$IMAGE" > "$STATE_FILE"

# Start through Compose so Synology Container Manager sees the project as
# running and can manage the active container with its Start/Stop controls.
sudo docker compose --project-name "$COMPOSE_PROJECT" --env-file "$APP_DIR/.env" \
  -f "$COMPOSE_FILE" up -d --no-build --force-recreate

for _ in 1 2 3 4 5 6 7 8 9 10; do
  if sudo docker exec "$CONTAINER" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)" >/dev/null 2>&1; then
    echo "Done. The Builder is healthy. Stop it after use."
    exit 0
  fi
  sleep 2
done

echo "ERROR: health check failed. The previous container remains stopped to avoid restoring an unauthenticated management interface." >&2
sudo docker logs --tail 100 "$CONTAINER" >&2 || true
sudo docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
if [ -n "$OLD_NAME" ]; then echo "Stopped rollback container preserved as: $OLD_NAME" >&2; fi
exit 1
