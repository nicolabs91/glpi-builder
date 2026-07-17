#!/usr/bin/env python3
"""Provision the single Builder administrator without storing a plaintext password."""

import argparse
import getpass
import ipaddress
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from auth_security import USERNAME_RE, generate_totp_secret, hash_password, load_auth_config, matching_totp_counter


MANAGED_KEYS = (
    "BUILDER_BIND_IP",
    "BUILDER_ADMIN_USERNAME",
    "BUILDER_ADMIN_PASSWORD_HASH",
    "BUILDER_ADMIN_TOTP_SECRET",
)


def validate_bind_ip(value):
    value = str(value or "").strip()
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValueError("Builder bind address must be a valid IPv4 address.") from exc
    if address.version != 4:
        raise ValueError("Builder bind address must be IPv4; IPv6 publishing is not supported by this installer.")
    if address.is_multicast or address.is_reserved or address == ipaddress.ip_address("255.255.255.255"):
        raise ValueError("Builder bind address cannot be multicast, reserved, or broadcast.")
    return str(address)


def validate_builder_port(value):
    value = str(value or "").strip()
    if not value.isdigit() or not 1 <= int(value) <= 65535:
        raise ValueError("BUILDER_PORT must be a number between 1 and 65535.")
    return int(value)


def select_bind_ip(path, requested=None, allow_all_interfaces=False, input_func=input):
    current = read_env(path).get("BUILDER_BIND_IP", "127.0.0.1") or "127.0.0.1"
    if requested is None:
        entered = input_func(f"Builder bind IPv4 address [{current}]: ").strip()
        selected = validate_bind_ip(entered or current)
        if selected == "0.0.0.0":
            print("WARNING: 0.0.0.0 exposes GLPI Builder on every IPv4 interface. Use HTTPS and firewall rules.")
            if input_func("Type EXPOSE to confirm: ").strip() != "EXPOSE":
                raise ValueError("Binding to every interface was not confirmed.")
        elif not ipaddress.ip_address(selected).is_loopback:
            print("Notice: the Builder will be reachable on the selected network interface. Use HTTPS and firewall rules.")
        return selected

    selected = validate_bind_ip(requested)
    if selected == "0.0.0.0" and not allow_all_interfaces:
        raise ValueError("Use --allow-all-interfaces to confirm a non-interactive 0.0.0.0 bind.")
    return selected


def provision(path, username, password, confirmation, secret, bind_ip=None):
    if not USERNAME_RE.fullmatch(username):
        raise ValueError("Username must be 3-64 letters, numbers, dots, underscores, or hyphens.")
    if password != confirmation:
        raise ValueError("Passwords do not match.")
    current_values = read_env(path)
    bind_ip = validate_bind_ip(bind_ip or current_values.get("BUILDER_BIND_IP", "127.0.0.1"))
    password_hash = hash_password(password)
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = [line for line in current.splitlines() if line.partition("=")[0] not in MANAGED_KEYS]
    if lines and lines[-1]:
        lines.append("")
    lines.extend((
        f"BUILDER_BIND_IP={bind_ip}",
        f"BUILDER_ADMIN_USERNAME={username}",
        # Compose expands dollar signs in unquoted env-file values. Keep the
        # PBKDF2 separators literal by using dotenv-compatible single quotes.
        f"BUILDER_ADMIN_PASSWORD_HASH='{password_hash}'",
        f"BUILDER_ADMIN_TOTP_SECRET={secret}",
    ))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    return secret


def read_env(path):
    values = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                if key in values:
                    raise ValueError(f"Duplicate configuration key is not allowed: {key}.")
                if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                    value = value[1:-1]
                values[key] = value
    return values


def check_configuration(path):
    values = read_env(path)
    validate_bind_ip(values.get("BUILDER_BIND_IP"))
    validate_builder_port(values.get("BUILDER_PORT"))
    flask_secret = values.get("FLASK_SECRET_KEY", "")
    if len(flask_secret) < 32 or flask_secret in {"CHANGE_ME_RANDOM_64_HEX", "CHANGE_ME"}:
        raise ValueError("FLASK_SECRET_KEY is missing, placeholder, or too short.")
    load_auth_config(values)
    if path.stat().st_mode & 0o077:
        raise ValueError(f"{path} must have mode 600.")


def main():
    parser = argparse.ArgumentParser(description="Provision GLPI Builder login and TOTP MFA.")
    parser.add_argument("--env", default=".env", help="Builder environment file (default: .env)")
    parser.add_argument("--username", help="Single administrator username")
    parser.add_argument("--bind-ip", help="IPv4 address on which Docker publishes the Builder port")
    parser.add_argument(
        "--allow-all-interfaces",
        action="store_true",
        help="Explicitly allow --bind-ip 0.0.0.0 (exposes every IPv4 interface)",
    )
    parser.add_argument("--check", action="store_true", help="Validate existing authentication configuration")
    args = parser.parse_args()
    if args.check:
        try:
            check_configuration(Path(args.env))
        except (OSError, ValueError) as exc:
            raise SystemExit(f"Authentication configuration invalid: {exc}") from exc
        print("Authentication configuration is valid.")
        return
    env_path = Path(args.env)
    try:
        bind_ip = select_bind_ip(env_path, args.bind_ip, args.allow_all_interfaces)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Provisioning failed: {exc}") from exc
    username = args.username or input("Administrator username: ").strip()
    password = getpass.getpass("Administrator password (minimum 14 characters): ")
    confirmation = getpass.getpass("Repeat administrator password: ")
    secret = generate_totp_secret()
    issuer = "GLPI Builder"
    uri = f"otpauth://totp/{quote(issuer)}:{quote(username)}?secret={secret}&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period=30"
    print("Add this Base32 secret to the administrator authenticator app:")
    print(secret)
    print("Authenticator URI:")
    print(uri)
    current_code = input("Enter the current six-digit authenticator code to confirm setup: ").strip()
    if matching_totp_counter(secret, current_code, window=1) is None:
        raise SystemExit("Provisioning failed: the authenticator code is invalid. No configuration was changed.")
    try:
        provision(env_path, username, password, confirmation, secret, bind_ip=bind_ip)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Provisioning failed: {exc}") from exc
    print(f"Configuration written safely to {args.env} (mode 600).")
    print(f"Builder bind address saved: {bind_ip}")
    print("The plaintext password was not stored. Keep the .env file and TOTP secret protected.")


if __name__ == "__main__":
    main()
