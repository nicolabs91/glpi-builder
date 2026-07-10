#!/usr/bin/env python3
"""Fail when the proven project docker-compose generator changes."""
import ast
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_AST_SHA256 = "1f267e7ec91b30134553458c44e40982fe2674bb6165eb0c143abef2d0f27831"

tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
function = next(
    node for node in tree.body
    if isinstance(node, ast.FunctionDef) and node.name == "write_compose"
)
actual = hashlib.sha256(ast.dump(function, include_attributes=False).encode()).hexdigest()
if actual != EXPECTED_AST_SHA256:
    raise SystemExit(
        "FOUT: write_compose/YAML-contract is gewijzigd. "
        f"Verwacht {EXPECTED_AST_SHA256}, ontvangen {actual}."
    )
print(f"OK: YAML-contract ongewijzigd ({actual})")
