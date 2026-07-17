"""Small, dependency-free authentication primitives for the Builder."""

import base64
import hashlib
import hmac
import os
import re
import struct
import time
from dataclasses import dataclass


PASSWORD_SCHEME = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 600_000
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,64}$")


@dataclass(frozen=True)
class AuthConfig:
    username: str
    password_hash: str
    totp_secret: str
    session_timeout_seconds: int
    session_absolute_timeout_seconds: int
    cookie_secure: bool


def hash_password(password, *, salt=None, iterations=PASSWORD_ITERATIONS):
    if len(password) < 14:
        raise ValueError("The administrator password must contain at least 14 characters.")
    if iterations < PASSWORD_ITERATIONS:
        raise ValueError("The password hashing work factor is too low.")
    salt_bytes = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt_bytes, iterations)
    return "$".join((
        PASSWORD_SCHEME,
        str(iterations),
        base64.urlsafe_b64encode(salt_bytes).decode("ascii").rstrip("="),
        base64.urlsafe_b64encode(digest).decode("ascii").rstrip("="),
    ))


def _decode_urlsafe(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def verify_password(password, encoded):
    try:
        scheme, iterations_text, salt_text, digest_text = encoded.split("$", 3)
        iterations = int(iterations_text)
        if scheme != PASSWORD_SCHEME or iterations < PASSWORD_ITERATIONS:
            return False
        salt = _decode_urlsafe(salt_text)
        expected = _decode_urlsafe(digest_text)
        if len(salt) < 16 or len(expected) != 32:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(actual, expected)
    except (TypeError, ValueError, base64.binascii.Error):
        return False


def generate_totp_secret():
    return base64.b32encode(os.urandom(20)).decode("ascii").rstrip("=")


def normalize_totp_secret(secret):
    normalized = re.sub(r"\s+", "", str(secret or "")).upper().rstrip("=")
    if not re.fullmatch(r"[A-Z2-7]{32,128}", normalized):
        raise ValueError("BUILDER_ADMIN_TOTP_SECRET is not a valid Base32 secret.")
    try:
        decoded = base64.b32decode(normalized + "=" * (-len(normalized) % 8), casefold=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise ValueError("BUILDER_ADMIN_TOTP_SECRET is not a valid Base32 secret.") from exc
    if len(decoded) < 20:
        raise ValueError("BUILDER_ADMIN_TOTP_SECRET must contain at least 160 bits.")
    return normalized


def totp_code(secret, *, timestamp=None, counter=None, digits=6, period=30):
    secret = normalize_totp_secret(secret)
    if counter is None:
        counter = int((time.time() if timestamp is None else timestamp) // period)
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", int(counter)), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(number % (10 ** digits)).zfill(digits)


def matching_totp_counter(secret, supplied, *, timestamp=None, window=1, period=30):
    supplied = str(supplied or "").strip()
    if not re.fullmatch(r"[0-9]{6}", supplied):
        return None
    current = int((time.time() if timestamp is None else timestamp) // period)
    for candidate in range(current - window, current + window + 1):
        if hmac.compare_digest(totp_code(secret, counter=candidate), supplied):
            return candidate
    return None


def _parse_bool(value, label):
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{label} must be true or false.")


def _parse_timeout(env, name, default, minimum, maximum):
    try:
        value = int(str(env.get(name, default)).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number.") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum} seconds.")
    return value


def load_auth_config(env=None):
    env = os.environ if env is None else env
    username = str(env.get("BUILDER_ADMIN_USERNAME", "")).strip()
    password_hash = str(env.get("BUILDER_ADMIN_PASSWORD_HASH", "")).strip()
    secret = str(env.get("BUILDER_ADMIN_TOTP_SECRET", "")).strip()
    if not USERNAME_RE.fullmatch(username):
        raise ValueError("BUILDER_ADMIN_USERNAME is missing or invalid.")
    if not _valid_hash_shape(password_hash):
        raise ValueError("BUILDER_ADMIN_PASSWORD_HASH is missing or invalid.")
    secret = normalize_totp_secret(secret)
    idle = _parse_timeout(env, "BUILDER_SESSION_TIMEOUT_SECONDS", 900, 300, 86_400)
    absolute = _parse_timeout(env, "BUILDER_SESSION_ABSOLUTE_TIMEOUT_SECONDS", 28_800, idle, 604_800)
    secure = _parse_bool(env.get("BUILDER_SESSION_COOKIE_SECURE", "true"), "BUILDER_SESSION_COOKIE_SECURE")
    return AuthConfig(username, password_hash, secret, idle, absolute, secure)


def _valid_hash_shape(encoded):
    try:
        scheme, iterations_text, salt_text, digest_text = encoded.split("$", 3)
        return (
            scheme == PASSWORD_SCHEME
            and int(iterations_text) >= PASSWORD_ITERATIONS
            and len(_decode_urlsafe(salt_text)) >= 16
            and len(_decode_urlsafe(digest_text)) == 32
        )
    except (TypeError, ValueError, base64.binascii.Error):
        return False
