#!/bin/sh
set -eu

APP_DIR="${1:-/volume1/docker/glpi-project-builder-v11}"
IMAGE="glpi-project-builder-v11:latest"
CONTAINER="glpi-project-builder-full-restore"
STATE_FILE="$APP_DIR/.last-install-state"
BACKUP_TASK_DIR="/volume1/docker/_BACKUPS/Restore_Scripts/GLPI"
BACKUP_SCRIPT="$BACKUP_TASK_DIR/GLPI_backup.sh"

cd "$APP_DIR"

if [ ! -f .env ]; then
  SECRET=$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')
  cp .env.example .env
  sed -i "s|CHANGE_ME_RANDOM_64_HEX|$SECRET|" .env
  echo "Configuratie aangemaakt: $APP_DIR/.env"
fi

sudo mkdir -p "$BACKUP_TASK_DIR"
if [ -f "$BACKUP_SCRIPT" ] && ! sudo grep -q "Managed by GLPI Project Builder" "$BACKUP_SCRIPT"; then
  if [ ! -f "$BACKUP_TASK_DIR/GLPI_backup.pre-builder.sh" ]; then
    sudo cp -p "$BACKUP_SCRIPT" "$BACKUP_TASK_DIR/GLPI_backup.pre-builder.sh"
    sudo chmod 700 "$BACKUP_TASK_DIR/GLPI_backup.pre-builder.sh"
  fi
fi
sudo cp "$APP_DIR/backup/GLPI_backup.sh" "$BACKUP_SCRIPT"
sudo chmod 750 "$BACKUP_SCRIPT"
echo "Backupscript geïnstalleerd: $BACKUP_SCRIPT"
echo "Vaste Taakplanner-opdracht: /bin/bash $BACKUP_SCRIPT"

sudo docker build -t "$IMAGE" .
sudo docker run --rm "$IMAGE" python tests/test_yaml_contract.py
sudo docker run --rm "$IMAGE" python tests/test_static_security.py

OLD_NAME=""
if sudo docker container inspect "$CONTAINER" >/dev/null 2>&1; then
  OLD_NAME="${CONTAINER}-pre-v11-$(date +%Y%m%d-%H%M%S)"
  sudo docker stop "$CONTAINER" >/dev/null 2>&1 || true
  sudo docker rename "$CONTAINER" "$OLD_NAME"
fi
printf 'OLD_CONTAINER=%s\nIMAGE=%s\n' "$OLD_NAME" "$IMAGE" > "$STATE_FILE"

sudo docker run -d \
  --name "$CONTAINER" \
  --restart no \
  --env-file "$APP_DIR/.env" \
  -p "$(sed -n 's/^BUILDER_BIND_IP=//p' .env):$(sed -n 's/^BUILDER_PORT=//p' .env):8080" \
  -e BASE_PATH=/volume1/docker \
  -e BACKUP_ROOT=/volume1/docker/_BACKUPS \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /volume1/docker:/volume1/docker \
  "$IMAGE"

for _ in 1 2 3 4 5 6 7 8 9 10; do
  if sudo docker exec "$CONTAINER" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)" >/dev/null 2>&1; then
    echo "Klaar. Builder is gezond. Stop hem na gebruik."
    exit 0
  fi
  sleep 2
done

echo "FOUT: healthcheck mislukt; vorige container wordt teruggezet." >&2
sudo docker logs --tail 100 "$CONTAINER" >&2 || true
sudo docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
if [ -n "$OLD_NAME" ]; then sudo docker rename "$OLD_NAME" "$CONTAINER" && sudo docker start "$CONTAINER"; fi
exit 1
