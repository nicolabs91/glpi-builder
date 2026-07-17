import time

from auth_security import AuthConfig, generate_totp_secret, hash_password


TEST_PASSWORD = "Correct horse battery staple 2026"
TEST_HASH = hash_password(TEST_PASSWORD, salt=b"0123456789abcdef")
TEST_TOTP_SECRET = generate_totp_secret()


def configure_auth(module):
    module.AUTH_CONFIG = AuthConfig(
        username="admin-test",
        password_hash=TEST_HASH,
        totp_secret=TEST_TOTP_SECRET,
        session_timeout_seconds=900,
        session_absolute_timeout_seconds=28_800,
        cookie_secure=False,
    )
    module.AUTH_CONFIG_ERROR = ""
    module.app.config["SESSION_COOKIE_SECURE"] = False


def authenticate(client, module):
    configure_auth(module)
    now = int(time.time())
    with client.session_transaction() as auth_session:
        auth_session["admin_authenticated"] = True
        auth_session["admin_issued_at"] = now
        auth_session["admin_last_activity"] = now
        auth_session["csrf_token"] = "test-csrf-token"
