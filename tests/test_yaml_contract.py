#!/usr/bin/env python3
"""Fail when the proven project docker-compose generator changes."""
import ast
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_GENERATOR_SHA256 = "166049b97670f522755a3877cefa3dff470d4f33eca637bb4bcb27729ef36006"
EXPECTED_BUILDER_COMPOSE_SHA256 = "8d904e44fecc15e3b87cfa77393d0159ef350d3a5fb490deea5d9ffa51496807"

source = (ROOT / "app.py").read_text(encoding="utf-8")
tree = ast.parse(source)
function = next(
    node for node in tree.body
    if isinstance(node, ast.FunctionDef) and node.name == "write_compose"
)
function_source = "".join(
    source.splitlines(keepends=True)[function.lineno - 1:function.end_lineno]
)
actual = hashlib.sha256(function_source.encode()).hexdigest()
if actual != EXPECTED_GENERATOR_SHA256:
    raise SystemExit(
        "ERROR: write_compose/YAML contract changed. "
        f"Expected {EXPECTED_GENERATOR_SHA256}, received {actual}."
    )

builder_compose = ROOT / "docker-compose.app.yml"
builder_actual = hashlib.sha256(builder_compose.read_bytes()).hexdigest()
if builder_actual != EXPECTED_BUILDER_COMPOSE_SHA256:
    raise SystemExit(
        "ERROR: docker-compose.app.yml changed. "
        f"Expected {EXPECTED_BUILDER_COMPOSE_SHA256}, received {builder_actual}."
    )

print(f"OK: YAML generator unchanged ({actual})")
print(f"OK: Builder Compose unchanged ({builder_actual})")
