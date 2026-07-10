#!/bin/sh
set -eu
APP_DIR="${1:-/volume1/docker/glpi-project-builder-v11}"
CONTAINER="glpi-project-builder-full-restore"
STATE_FILE="$APP_DIR/.last-install-state"
[ -f "$STATE_FILE" ] || { echo "Geen installatiestatus gevonden." >&2; exit 1; }
. "$STATE_FILE"
[ -n "${OLD_CONTAINER:-}" ] || { echo "Geen vorige container geregistreerd." >&2; exit 1; }
sudo docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
sudo docker rename "$OLD_CONTAINER" "$CONTAINER"
sudo docker start "$CONTAINER"
echo "Vorige Builder-container hersteld: $CONTAINER"
