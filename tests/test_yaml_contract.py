#!/usr/bin/env python3
"""Fail when the proven project docker-compose generator changes."""
import ast
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_GENERATOR_SHA256 = "9d8ee969f724aa905bf46bc54a413bdda74cf6b5abf5c1fb4c1d4c2f57b408f2"
EXPECTED_BUILDER_COMPOSE_SHA256 = "ab701e95ab16e4e77e07e0395920ec6140444cf71dc98c6ec84984b41cff7e7d"

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
        "FOUT: write_compose/YAML-contract is gewijzigd. "
        f"Verwacht {EXPECTED_GENERATOR_SHA256}, ontvangen {actual}."
    )

builder_compose = ROOT / "docker-compose.app.yml"
builder_actual = hashlib.sha256(builder_compose.read_bytes()).hexdigest()
if builder_actual != EXPECTED_BUILDER_COMPOSE_SHA256:
    raise SystemExit(
        "FOUT: docker-compose.app.yml is gewijzigd. "
        f"Verwacht {EXPECTED_BUILDER_COMPOSE_SHA256}, ontvangen {builder_actual}."
    )

print(f"OK: YAML-generator ongewijzigd ({actual})")
print(f"OK: Builder-compose ongewijzigd ({builder_actual})")
