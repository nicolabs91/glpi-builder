#!/bin/sh
set -eu

APP_DIR="/volume1/docker/glpi-project-builder-full-restore"

cd "$APP_DIR"

sudo docker build -t glpi-project-builder-full-restore-v10-4-sso-yaml-locked:latest .
sudo docker rm -f glpi-project-builder-safe 2>/dev/null || true
sudo docker rm -f glpi-project-builder-safe-restore 2>/dev/null || true
sudo docker rm -f glpi-project-builder-full-restore 2>/dev/null || true

sudo docker run -d \
  --name glpi-project-builder-full-restore \
  --restart no \
  -p 5055:8080 \
  -e BASE_PATH=/volume1/docker \
  -e BACKUP_ROOT=/volume1/docker/_BACKUPS \
  -e TZ=Europe/Amsterdam \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /volume1/docker:/volume1/docker \
  glpi-project-builder-full-restore-v10-4-sso-yaml-locked:latest

echo "Klaar. Open: http://<NAS-IP>:5055"
