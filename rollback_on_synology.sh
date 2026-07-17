#!/bin/sh
set -eu

APP_DIR="${1:-/volume1/docker/glpi-builder}"
CONTAINER="glpi-builder"
COMPOSE_PROJECT="glpi-builder"
COMPOSE_FILE="$APP_DIR/docker-compose.app.yml"
STATE_FILE="$APP_DIR/.last-install-state"
CANDIDATE=""

cleanup() {
  if [ -n "$CANDIDATE" ]; then sudo docker rm -f "$CANDIDATE" >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT INT TERM

[ -f "$STATE_FILE" ] || { echo "No installation state found." >&2; exit 1; }
. "$STATE_FILE"
[ -n "${OLD_CONTAINER:-}" ] || { echo "No previous container was recorded." >&2; exit 1; }
python3 "$APP_DIR/scripts/provision_admin.py" --env "$APP_DIR/.env" --check

OLD_IMAGE_ID=$(sudo docker inspect --format '{{.Image}}' "$OLD_CONTAINER")
CANDIDATE="glpi-builder-rollback-proof-$(date +%Y%m%d-%H%M%S)"

# Prove the old image in isolation. It gets no published port, but otherwise
# receives the current secure environment and the same required mounts.
sudo docker run -d \
  --name "$CANDIDATE" \
  --restart no \
  --env-file "$APP_DIR/.env" \
  -e BASE_PATH=/volume1/docker \
  -e BACKUP_ROOT=/volume1/docker/_BACKUPS \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /volume1/docker:/volume1/docker \
  "$OLD_IMAGE_ID" >/dev/null

[ -z "$(sudo docker port "$CANDIDATE")" ] || { echo "Refusing rollback: proof candidate unexpectedly published a port." >&2; exit 1; }

PROOF_HEALTH=""
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if sudo docker exec "$CANDIDATE" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)" >/dev/null 2>&1; then
    PROOF_HEALTH="ok"
    break
  fi
  sleep 2
done
[ "$PROOF_HEALTH" = "ok" ] || { echo "Refusing rollback: proof candidate health check failed." >&2; exit 1; }

ROOT_RESULT=$(sudo docker exec "$CANDIDATE" curl -sS -o /dev/null -w '%{http_code}|%{redirect_url}' http://127.0.0.1:8080/)
ROOT_STATUS=${ROOT_RESULT%%|*}
ROOT_LOCATION=${ROOT_RESULT#*|}
case "$ROOT_STATUS|$ROOT_LOCATION" in
  302*\|*/login*|303*\|*/login*|401\|*) ;;
  *) echo "Refusing rollback: unauthenticated dashboard access was not denied." >&2; exit 1 ;;
esac
LOGIN_STATUS=$(sudo docker exec "$CANDIDATE" curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/login)
[ "$LOGIN_STATUS" = "200" ] || { echo "Refusing rollback: login page proof failed." >&2; exit 1; }

sudo docker rm -f "$CANDIDATE" >/dev/null
CANDIDATE=""

# Keep the currently authenticated container as recovery until the rollback
# image has passed its published health check.
CURRENT_SAFE="${CONTAINER}-pre-rollback-$(date +%Y%m%d-%H%M%S)"
sudo docker stop "$CONTAINER" >/dev/null 2>&1 || true
sudo docker rename "$CONTAINER" "$CURRENT_SAFE"

if ! sudo env GLPI_BUILDER_IMAGE="$OLD_IMAGE_ID" docker compose \
  --project-name "$COMPOSE_PROJECT" --env-file "$APP_DIR/.env" \
  -f "$COMPOSE_FILE" up -d --no-build --force-recreate >/dev/null; then
  # Compose can fail after creating a container in Created state. Remove that
  # name before restoring the previously current authenticated container.
  sudo docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  sudo docker rename "$CURRENT_SAFE" "$CONTAINER"
  sudo docker start "$CONTAINER" >/dev/null
  echo "Rollback start failed; the authenticated current container was restored." >&2
  exit 1
fi

for _ in 1 2 3 4 5 6 7 8 9 10; do
  if sudo docker exec "$CONTAINER" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)" >/dev/null 2>&1; then
    echo "Authenticated rollback completed. Previous current container preserved as: $CURRENT_SAFE"
    exit 0
  fi
  sleep 2
done

sudo docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
sudo docker rename "$CURRENT_SAFE" "$CONTAINER"
sudo docker start "$CONTAINER" >/dev/null
echo "Rollback health check failed; the authenticated current container was restored." >&2
exit 1
