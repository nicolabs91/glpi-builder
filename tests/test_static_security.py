#!/usr/bin/env python3
from pathlib import Path

source = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
required = [
    "path_under_backup_root(backup_path)",
    "path_under_backup_root(source)",
    "validate_local_image(source.get(\"glpi_image\")",
    "MUTATION_LOCK.acquire(blocking=False)",
    'session["pending_create_preview"]',
    '@app.route("/create/execute", methods=["POST"])',
    '@app.route("/healthz")',
]
missing = [item for item in required if item not in source]
if missing:
    raise SystemExit("Ontbrekende beveiligingscontroles: " + ", ".join(missing))
print("OK: statische beveiligingscontroles aanwezig")
