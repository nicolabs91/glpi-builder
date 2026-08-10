#!/usr/bin/env python3
"""Fail when the proven project docker-compose generator changes."""
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_BUILDER_COMPOSE_SHA256 = "8d904e44fecc15e3b87cfa77393d0159ef350d3a5fb490deea5d9ffa51496807"

source = (ROOT / "app.py").read_text(encoding="utf-8")
template = (ROOT / "templates" / "glpi-compose.yml").read_bytes()
if b"__PROJECT__" not in template or b"__ENTRYPOINT__" not in template:
    raise SystemExit("ERROR: canonical GLPI YAML template is missing required placeholders.")

builder_compose = ROOT / "docker-compose.app.yml"
builder_actual = hashlib.sha256(builder_compose.read_bytes()).hexdigest()
if builder_actual != EXPECTED_BUILDER_COMPOSE_SHA256:
    raise SystemExit(
        "ERROR: docker-compose.app.yml changed. "
        f"Expected {EXPECTED_BUILDER_COMPOSE_SHA256}, received {builder_actual}."
    )

print(f"OK: canonical GLPI YAML template present ({hashlib.sha256(template).hexdigest()})")
print(f"OK: Builder Compose unchanged ({builder_actual})")
