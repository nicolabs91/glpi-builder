#!/usr/bin/env python3
"""Compare the generated Synology Compose file byte for byte."""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as module


PROJECT = "glpi-contract-test"
ENV = {
    "MARIADB_IMAGE": "mariadb:11.4",
    "GLPI_IMAGE": "glpi/glpi:10.0.18",
    "GLPI_HTTP_PORT": "8099",
    "GLPI_CONTAINER_PORT": "8080",
    "GLPI_SESSION_COOKIE_SAMESITE": "Lax",
    "GLPI_SESSION_COOKIE_SECURE": "Off",
    "MARIADB_ROOT_PASSWORD": "contract-root",
    "GLPI_DB_NAME": "glpi",
    "GLPI_DB_USER": "glpiuser",
    "GLPI_DB_PASSWORD": "contract-db",
    "TZ": "Europe/Brussels",
}


with tempfile.TemporaryDirectory() as temporary_directory:
    original_base_path = module.BASE_PATH
    try:
        module.BASE_PATH = Path(temporary_directory)
        (module.BASE_PATH / PROJECT).mkdir()
        module.write_compose(PROJECT, ENV)
        actual = (module.BASE_PATH / PROJECT / "docker-compose.yml").read_bytes()
    finally:
        module.BASE_PATH = original_base_path

expected_path = ROOT / "tests" / "fixtures" / "locked-project-compose.yml"
expected = expected_path.read_bytes()
if actual != expected:
    raise SystemExit(
        "ERROR: generated GLPI Compose differs from the proven Synology YAML. "
        "Restore the generator or perform an explicitly approved NAS requalification."
    )

print("OK: generated Synology Compose is unchanged byte for byte")
