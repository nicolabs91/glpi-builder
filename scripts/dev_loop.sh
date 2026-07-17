#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
IMAGE=${IMAGE:-glpi-builder:dev-loop}
CONTAINER="glpi-builder-dev-loop-$$"
TEMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/glpi-builder-loop.XXXXXX")
TEST_PASSWORD_HASH=$(python3 -c 'from auth_security import hash_password; print(hash_password("Development password only", salt=b"0123456789abcdef"))')
export FLASK_SECRET_KEY="$(python3 -c 'print("d" * 64)')"
export BUILDER_ADMIN_USERNAME="dev-admin"
export BUILDER_ADMIN_PASSWORD_HASH="$TEST_PASSWORD_HASH"
export BUILDER_ADMIN_TOTP_SECRET="JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
export BUILDER_SESSION_COOKIE_SECURE="false"

cleanup() {
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    rm -rf "$TEMP_ROOT"
}
trap cleanup EXIT INT TERM

cd "$ROOT"
mkdir -p "$TEMP_ROOT/_BACKUPS"

echo "[1/6] Checking Python syntax"
python3 -m py_compile app.py auth_security.py scripts/provision_admin.py tests/*.py

echo "[2/6] Checking locked YAML sources"
python3 tests/test_yaml_contract.py

echo "[3/6] Validating Builder Compose"
docker compose -f docker-compose.app.yml config --quiet

echo "[4/6] Building a clean Docker image"
docker build --pull -t "$IMAGE" .

echo "[5/6] Running regression, security, and exact YAML tests"
docker run --rm --entrypoint sh \
    -e FLASK_SECRET_KEY -e BUILDER_ADMIN_USERNAME -e BUILDER_ADMIN_PASSWORD_HASH \
    -e BUILDER_ADMIN_TOTP_SECRET -e BUILDER_SESSION_COOKIE_SECURE \
    "$IMAGE" -c \
    'docker --version && gunicorn --version && pip check && python -m unittest discover -s tests -p "test_*.py" -v'

echo "[6/6] Starting a real container and waiting for its health check"
docker run -d \
    --name "$CONTAINER" \
    -e BASE_PATH=/volume1/docker \
    -e BACKUP_ROOT=/volume1/docker/_BACKUPS \
    -e FLASK_SECRET_KEY -e BUILDER_ADMIN_USERNAME -e BUILDER_ADMIN_PASSWORD_HASH \
    -e BUILDER_ADMIN_TOTP_SECRET -e BUILDER_SESSION_COOKIE_SECURE \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$TEMP_ROOT:/volume1/docker" \
    "$IMAGE" >/dev/null

attempt=0
while [ "$attempt" -lt 30 ]; do
    health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "$CONTAINER")
    if [ "$health" = "healthy" ]; then
        echo "OK: full development loop passed; YAML contract intact and container healthy."
        exit 0
    fi
    if [ "$health" = "unhealthy" ]; then
        docker logs "$CONTAINER"
        echo "ERROR: container became unhealthy." >&2
        exit 1
    fi
    attempt=$((attempt + 1))
    sleep 1
done

docker logs "$CONTAINER"
echo "ERROR: health check did not become healthy in time." >&2
exit 1
