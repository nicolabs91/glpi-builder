#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
IMAGE=${IMAGE:-glpi-project-builder-v11:dev-loop}
CONTAINER="glpi-builder-dev-loop-$$"
TEMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/glpi-builder-loop.XXXXXX")

cleanup() {
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    rm -rf "$TEMP_ROOT"
}
trap cleanup EXIT INT TERM

cd "$ROOT"
mkdir -p "$TEMP_ROOT/_BACKUPS"

echo "[1/6] Python-syntax controleren"
python3 -m py_compile app.py tests/*.py

echo "[2/6] Vergrendelde YAML-bronnen controleren"
python3 tests/test_yaml_contract.py

echo "[3/6] Builder-compose valideren"
FLASK_SECRET_KEY=dev-loop-secret docker compose -f docker-compose.app.yml config --quiet

echo "[4/6] Schone Docker-image bouwen"
docker build --pull -t "$IMAGE" .

echo "[5/6] Regressie-, security- en exacte YAML-tests draaien"
docker run --rm --entrypoint sh "$IMAGE" -c \
    'python tests/test_yaml_contract.py && python tests/test_locked_yaml_output.py && python tests/test_static_security.py && python tests/test_preview_flow.py && python tests/test_restore_modes.py && python tests/test_ui_language_progress.py && python tests/test_backup_configuration.py'

echo "[6/6] Echte container starten en healthcheck afwachten"
docker run -d \
    --name "$CONTAINER" \
    -e BASE_PATH=/volume1/docker \
    -e BACKUP_ROOT=/volume1/docker/_BACKUPS \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$TEMP_ROOT:/volume1/docker" \
    "$IMAGE" >/dev/null

attempt=0
while [ "$attempt" -lt 30 ]; do
    health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "$CONTAINER")
    if [ "$health" = "healthy" ]; then
        echo "OK: volledige ontwikkelloop geslaagd; YAML-contract intact en container healthy."
        exit 0
    fi
    if [ "$health" = "unhealthy" ]; then
        docker logs "$CONTAINER"
        echo "FOUT: container werd unhealthy." >&2
        exit 1
    fi
    attempt=$((attempt + 1))
    sleep 1
done

docker logs "$CONTAINER"
echo "FOUT: healthcheck werd niet tijdig healthy." >&2
exit 1
