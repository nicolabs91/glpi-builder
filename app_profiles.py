"""Extensible application profiles for Builder-managed Docker projects.

Legacy GLPI projects deliberately remain owned by app.py.  New application
types use an explicit manifest so discovery never mistakes arbitrary Compose
projects for something the Builder may safely mutate.
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass
from pathlib import Path


MANIFEST_NAME = ".builder-app.json"
PROFILE_SCHEMA = 1
PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,50}$")
IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]*(?::[A-Za-z0-9][A-Za-z0-9._-]*)?(?:@sha256:[a-f0-9]{64})?$")


@dataclass(frozen=True)
class AppProfile:
    key: str
    name: str
    description: str
    image_prefixes: tuple[str, ...]
    default_image: str
    internal_port: int
    database: str
    database_image_prefixes: tuple[str, ...]
    default_database_image: str
    volumes: tuple[str, ...]
    backup_note: str
    quarantine_restore: bool = False
    quarantine_note: str = "No verified isolated restore adapter is available for this application."


PROFILES = {
    "n8n": AppProfile(
        key="n8n",
        name="n8n",
        description="Workflow automation with PostgreSQL and a persistent encryption key.",
        image_prefixes=("docker.n8n.io/n8nio/n8n:", "n8nio/n8n:"),
        default_image="docker.n8n.io/n8nio/n8n:latest",
        internal_port=5678,
        database="postgresql",
        database_image_prefixes=("postgres:",),
        default_database_image="postgres:16-alpine",
        volumes=("data", "database"),
        backup_note="Back up PostgreSQL and the n8n data directory together; the encryption key is retained in the private environment file.",
        quarantine_restore=True,
        quarantine_note="Restores one manifest-verified PostgreSQL and n8n data backup set into a new private environment. Triggers and external routes remain blocked by the internal Docker network.",
    ),
    "teampasswordmanager": AppProfile(
        key="teampasswordmanager",
        name="Team Password Manager",
        description="Team credential management with a dedicated MySQL database.",
        image_prefixes=("teampasswordmanager/teampasswordmanager:",),
        default_image="teampasswordmanager/teampasswordmanager:latest",
        internal_port=80,
        database="mysql:5.7",
        database_image_prefixes=("mysql:",),
        default_database_image="mysql:5.7",
        volumes=("application", "database"),
        backup_note="Back up the MariaDB database and application data directory as one consistent set.",
        quarantine_restore=True,
        quarantine_note="Restores a TPM SQL backup into a new private MySQL database. External settings remain in the copied data but cannot connect because the application network has no external route.",
    ),
}


def profile_catalog():
    return tuple(PROFILES.values())


def get_profile(key: str) -> AppProfile:
    try:
        return PROFILES[str(key or "").strip().lower()]
    except KeyError as exc:
        raise ValueError("Select a supported application type.") from exc


def validate_project_name(value: str) -> str:
    value = str(value or "").strip().lower()
    if not PROJECT_RE.fullmatch(value):
        raise ValueError("Project name must use 3-51 lowercase letters, numbers, _ or -.")
    return value


def validate_image(profile: AppProfile, image: str) -> str:
    image = str(image or "").strip()
    if not IMAGE_RE.fullmatch(image) or not image.startswith(profile.image_prefixes):
        raise ValueError(f"Image must use an allowed {profile.name} repository: {', '.join(profile.image_prefixes)}")
    return image


def validate_database_image(profile: AppProfile, image: str) -> str:
    image = str(image or "").strip()
    if not IMAGE_RE.fullmatch(image) or not image.startswith(profile.database_image_prefixes):
        raise ValueError(
            f"Database image must use an allowed repository for {profile.name}: "
            f"{', '.join(profile.database_image_prefixes)}"
        )
    return image


def postgres_data_mount_target(image: str) -> str:
    """Return the persistent-data mount expected by the selected Postgres image."""
    match = re.match(r"^postgres:(\d+)(?:$|[.@-])", str(image or "").strip())
    if match and int(match.group(1)) >= 18:
        return "/var/lib/postgresql"
    return "/var/lib/postgresql/data"


def validate_port(value) -> int:
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("Web port must be a number.") from exc
    if not 1 <= port <= 65535:
        raise ValueError("Web port must be between 1 and 65535.")
    return port


def _secret() -> str:
    return secrets.token_urlsafe(36)


def build_environment(
    profile: AppProfile, project: str, host_port: int, image: str, timezone: str,
    *, database_image="", quarantine=False, bind_address="0.0.0.0", expires_at="",
):
    project = validate_project_name(project)
    host_port = validate_port(host_port)
    image = validate_image(profile, image)
    database_image = validate_database_image(
        profile, database_image or profile.default_database_image
    )
    timezone = str(timezone or "Europe/Brussels").strip()
    if not re.fullmatch(r"[A-Za-z0-9_+./-]{1,64}", timezone):
        raise ValueError("Time zone contains unsupported characters.")
    common = {
        "BUILDER_APP_SCHEMA": str(PROFILE_SCHEMA),
        "BUILDER_APP_TYPE": profile.key,
        "PROJECT_NAME": project,
        "APP_IMAGE": image,
        "DATABASE_IMAGE": database_image,
        "APP_HTTP_PORT": str(host_port),
        "TZ": timezone,
        "BUILDER_QUARANTINE": "1" if quarantine else "0",
        "APP_BIND_ADDRESS": str(bind_address),
        "BUILDER_QUARANTINE_EXPIRES_AT": str(expires_at or ""),
    }
    if profile.key == "n8n":
        common.update({
            "POSTGRES_DB": "n8n",
            "POSTGRES_USER": "n8n",
            "POSTGRES_PASSWORD": _secret(),
            "N8N_ENCRYPTION_KEY": _secret(),
        })
    elif profile.key == "teampasswordmanager":
        common.update({
            "MYSQL_DATABASE": "teampasswordmanager",
            "MYSQL_USER": "teampasswordmanager",
            "MYSQL_PASSWORD": _secret(),
            "MYSQL_ROOT_PASSWORD": _secret(),
        })
    return common


def render_compose(profile: AppProfile, env: dict, base_path: str = "/volume1/docker") -> str:
    project = validate_project_name(env["PROJECT_NAME"])
    root = f"{base_path.rstrip('/')}/{project}"
    quarantine = str(env.get("BUILDER_QUARANTINE", "0")) == "1"
    network_options = "\n    internal: true" if quarantine else ""
    security_options = '''
    security_opt:
      - no-new-privileges:true
    pids_limit: 256
    mem_limit: 1g''' if quarantine else ""
    if profile.key == "n8n":
        postgres_mount = postgres_data_mount_target(env.get("DATABASE_IMAGE", ""))
        return f'''services:
  {project}-db:
    image: ${{DATABASE_IMAGE}}
    container_name: {project}-db
    restart: unless-stopped
    env_file: [.env]
    environment:
      POSTGRES_DB: ${{POSTGRES_DB}}
      POSTGRES_USER: ${{POSTGRES_USER}}
      POSTGRES_PASSWORD: ${{POSTGRES_PASSWORD}}
    volumes:
      - {root}/database:{postgres_mount}:rw
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U $${{POSTGRES_USER}} -d $${{POSTGRES_DB}}"]
      interval: 10s
      timeout: 5s
      retries: 10
    networks: [{project}-network]
  {project}:
    image: ${{APP_IMAGE}}
    container_name: {project}
    restart: unless-stopped
    env_file: [.env]
    ports: ["${{APP_BIND_ADDRESS}}:${{APP_HTTP_PORT}}:5678"]{security_options}
    environment:
      DB_TYPE: postgresdb
      DB_POSTGRESDB_HOST: {project}-db
      DB_POSTGRESDB_DATABASE: ${{POSTGRES_DB}}
      DB_POSTGRESDB_USER: ${{POSTGRES_USER}}
      DB_POSTGRESDB_PASSWORD: ${{POSTGRES_PASSWORD}}
      N8N_ENCRYPTION_KEY: ${{N8N_ENCRYPTION_KEY}}
      GENERIC_TIMEZONE: ${{TZ}}
      TZ: ${{TZ}}
    volumes:
      - {root}/data:/home/node/.n8n:rw
    depends_on:
      {project}-db:
        condition: service_healthy
    networks: [{project}-network]
networks:
  {project}-network:
    name: {project}-network
    driver: bridge{network_options}
'''
    return f'''services:
  {project}-db:
    image: ${{DATABASE_IMAGE}}
    container_name: {project}-db
    restart: unless-stopped
    env_file: [.env]
    environment:
      MYSQL_DATABASE: ${{MYSQL_DATABASE}}
      MYSQL_USER: ${{MYSQL_USER}}
      MYSQL_PASSWORD: ${{MYSQL_PASSWORD}}
      MYSQL_ROOT_PASSWORD: ${{MYSQL_ROOT_PASSWORD}}
    volumes:
      - {root}/database:/var/lib/mysql:rw
    healthcheck:
      test: ["CMD-SHELL", "mysqladmin ping -h 127.0.0.1 -u root -p$${{MYSQL_ROOT_PASSWORD}} --silent"]
      interval: 10s
      timeout: 5s
      retries: 10
    networks: [{project}-network]
  {project}:
    image: ${{APP_IMAGE}}
    container_name: {project}
    restart: unless-stopped
    env_file: [.env]
    ports: ["${{APP_BIND_ADDRESS}}:${{APP_HTTP_PORT}}:80"]{security_options}
    environment:
      TPM_SERVER_TIMEZONE: ${{TZ}}
      TPM_PHP_TIMEZONE: ${{TZ}}
      TPM_ENCRYPT_DB_CONFIG: "0"
      TPM_CONFIG_HOSTNAME: {project}-db
      TPM_CONFIG_PORT: "3306"
      TPM_CONFIG_DATABASE: ${{MYSQL_DATABASE}}
      TPM_CONFIG_USERNAME: ${{MYSQL_USER}}
      TPM_CONFIG_PASSWORD: ${{MYSQL_PASSWORD}}
      TPM_UPGRADE: "0"
      BUILDER_TEST_ENVIRONMENT: ${{BUILDER_QUARANTINE}}
    volumes:
      - {root}/application:/var/www/html:rw
    depends_on:
      {project}-db:
        condition: service_healthy
    networks: [{project}-network]
networks:
  {project}-network:
    name: {project}-network
    driver: bridge{network_options}
'''


def manifest(profile: AppProfile, env: dict) -> dict:
    return {
        "schema": PROFILE_SCHEMA,
        "type": profile.key,
        "name": profile.name,
        "project": validate_project_name(env["PROJECT_NAME"]),
        "port": validate_port(env["APP_HTTP_PORT"]),
        "image": validate_image(profile, env["APP_IMAGE"]),
        "database_image": validate_database_image(
            profile, env.get("DATABASE_IMAGE") or profile.default_database_image
        ),
        "internal_port": profile.internal_port,
        "volumes": list(profile.volumes),
        "quarantine": str(env.get("BUILDER_QUARANTINE", "0")) == "1",
        "bind_address": str(env.get("APP_BIND_ADDRESS") or "0.0.0.0"),
        "expires_at": str(env.get("BUILDER_QUARANTINE_EXPIRES_AT") or ""),
    }


def read_manifest(folder: Path):
    path = Path(folder) / MANIFEST_NAME
    if path.is_symlink() or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        profile = get_profile(data.get("type"))
        if data.get("schema") != PROFILE_SCHEMA or data.get("project") != Path(folder).name:
            return None
        validate_image(profile, data.get("image"))
        validate_port(data.get("port"))
        return data
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
