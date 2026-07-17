#!/usr/bin/env python3
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import app as module
from auth_security import matching_totp_counter, totp_code, verify_password
from scripts.provision_admin import (
    check_configuration,
    provision,
    select_bind_ip,
    validate_bind_ip,
    validate_builder_port,
)
from tests.auth_test_support import TEST_PASSWORD, TEST_TOTP_SECRET, configure_auth


class AuthenticationTest(unittest.TestCase):
    def setUp(self):
        configure_auth(module)
        module.app.config.update(TESTING=True, SECRET_KEY="authentication-test-secret")
        module.LOGIN_RATE_BUCKETS.clear()
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.state_path = Path(self.temporary_directory.name) / "auth-state"
        self.state_patcher = patch.object(module, "AUTH_STATE_PATH", self.state_path)
        self.state_patcher.start()
        self.client = module.app.test_client()

    def tearDown(self):
        self.state_patcher.stop()
        self.temporary_directory.cleanup()

    def login_csrf(self):
        response = self.client.get("/login")
        self.assertEqual(response.status_code, 200)
        with self.client.session_transaction() as login_session:
            return login_session["csrf_token"]

    def test_rfc6238_sha1_vector_and_window(self):
        secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
        self.assertEqual(totp_code(secret, timestamp=59), "287082")
        self.assertEqual(matching_totp_counter(secret, "287082", timestamp=59, window=1), 1)

    def test_password_hash_is_strong_and_plaintext_is_absent(self):
        encoded = module.AUTH_CONFIG.password_hash
        self.assertTrue(encoded.startswith("pbkdf2_sha256$600000$"))
        self.assertNotIn(TEST_PASSWORD, encoded)
        self.assertTrue(verify_password(TEST_PASSWORD, encoded))
        self.assertFalse(verify_password("wrong password", encoded))

    def test_unauthenticated_management_route_redirects_to_login(self):
        response = self.client.get("/ui-preview")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_unsafe_next_targets_are_normalized_to_dashboard(self):
        unsafe = (
            "//example.invalid/path",
            "/\\example.invalid/path",
            "/%5cexample.invalid/path",
            "/%255cexample.invalid/path",
            "/safe%0d%0aLocation:%20https://example.invalid",
            "https://example.invalid/path",
        )
        with module.app.test_request_context("/"):
            for value in unsafe:
                with self.subTest(value=value):
                    self.assertEqual(module.safe_internal_next(value), "/")
            self.assertEqual(module.safe_internal_next("/progress/token?view=full"), "/progress/token?view=full")

    def test_missing_configuration_fails_closed_but_minimal_health_is_public(self):
        with patch.object(module, "AUTH_CONFIG_ERROR", "missing"), patch.object(module, "AUTH_CONFIG", None):
            protected = self.client.get("/")
            login = self.client.get("/login")
            health = self.client.get("/healthz")
        self.assertEqual(protected.status_code, 503)
        self.assertEqual(login.status_code, 503)
        self.assertEqual(health.status_code, 503)
        self.assertEqual(health.get_json(), {"status": "unhealthy"})

    def test_password_totp_login_rotates_session_and_rejects_replay(self):
        csrf = self.login_csrf()
        with self.client.session_transaction() as login_session:
            old_cookie_payload = dict(login_session)
        code = totp_code(TEST_TOTP_SECRET)
        response = self.client.post("/login", data={
            "csrf_token": csrf,
            "username": "admin-test",
            "password": TEST_PASSWORD,
            "totp": code,
            "next": "/",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/")
        with self.client.session_transaction() as authenticated:
            self.assertTrue(authenticated["admin_authenticated"])
            self.assertNotEqual(old_cookie_payload.get("csrf_token"), authenticated["csrf_token"])
            self.assertIn("login_nonce", authenticated)
        self.assertTrue(self.state_path.is_file())
        self.assertEqual(self.state_path.stat().st_mode & 0o777, 0o600)

        second_client = module.app.test_client()
        second_csrf_response = second_client.get("/login")
        self.assertEqual(second_csrf_response.status_code, 200)
        with second_client.session_transaction() as second_session:
            second_csrf = second_session["csrf_token"]
        replay = second_client.post("/login", data={
            "csrf_token": second_csrf,
            "username": "admin-test",
            "password": TEST_PASSWORD,
            "totp": code,
        })
        self.assertEqual(replay.status_code, 401)
        self.assertIn(b"Unable to sign in", replay.data)

    def test_login_errors_are_generic_and_rate_limit_is_bounded(self):
        for _ in range(module.LOGIN_RATE_MAX_FAILURES):
            csrf = self.login_csrf()
            response = self.client.post("/login", data={
                "csrf_token": csrf,
                "username": "unknown",
                "password": "not the password",
                "totp": "000000",
            })
            self.assertIn(response.status_code, (401, 429))
            self.assertIn(b"Unable to sign in", response.data)
            self.assertNotIn(b"unknown", response.data.lower())
            self.assertNotIn(b"not the password", response.data.lower())
        blocked = self.client.post("/login", data={"csrf_token": "unused"})
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(blocked.headers["Retry-After"], str(module.LOGIN_RATE_BLOCK_SECONDS))
        for index in range(module.LOGIN_RATE_MAX_BUCKETS + 100):
            module.login_rate_record_failure(f"192.0.2.{index}", now=float(index + 1))
        self.assertLessEqual(len(module.LOGIN_RATE_BUCKETS), module.LOGIN_RATE_MAX_BUCKETS)

    def test_idle_and_absolute_session_expiration(self):
        now = int(time.time())
        with self.client.session_transaction() as authenticated:
            authenticated["admin_authenticated"] = True
            authenticated["admin_issued_at"] = now - module.AUTH_CONFIG.session_absolute_timeout_seconds - 1
            authenticated["admin_last_activity"] = now
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

        idle_client = module.app.test_client()
        with idle_client.session_transaction() as authenticated:
            authenticated["admin_authenticated"] = True
            authenticated["admin_issued_at"] = now - 60
            authenticated["admin_last_activity"] = now - module.AUTH_CONFIG.session_timeout_seconds - 1
        idle_response = idle_client.get("/")
        self.assertEqual(idle_response.status_code, 302)
        self.assertIn("/login", idle_response.headers["Location"])

    def test_forwarded_for_is_ignored_for_rate_limit_identity(self):
        with module.app.test_request_context(
            "/login",
            environ_base={"REMOTE_ADDR": "192.0.2.44"},
            headers={"X-Forwarded-For": "198.51.100.7"},
        ):
            self.assertEqual(module.login_rate_key(), "192.0.2.44")

    def test_concurrent_totp_replay_allows_only_one_login(self):
        clients = [module.app.test_client(), module.app.test_client()]
        csrf_tokens = []
        for client in clients:
            self.assertEqual(client.get("/login").status_code, 200)
            with client.session_transaction() as login_session:
                csrf_tokens.append(login_session["csrf_token"])
        barrier = threading.Barrier(2)
        results = []
        result_lock = threading.Lock()
        code = totp_code(TEST_TOTP_SECRET)

        def attempt(client, csrf):
            barrier.wait()
            response = client.post("/login", data={
                "csrf_token": csrf,
                "username": "admin-test",
                "password": TEST_PASSWORD,
                "totp": code,
            })
            with result_lock:
                results.append(response.status_code)

        threads = [threading.Thread(target=attempt, args=item) for item in zip(clients, csrf_tokens)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(sorted(results), [302, 401])

    def test_unauthenticated_post_never_reaches_mutation_helper(self):
        with patch.object(module, "change_project_port") as mutation:
            response = self.client.post("/change-port", data={"project": "glpi-test", "host_port": "9000"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])
        mutation.assert_not_called()
        self.assertFalse(module.MUTATION_LOCK.locked())

    def test_logout_is_post_only_and_csrf_protected(self):
        now = int(time.time())
        with self.client.session_transaction() as authenticated:
            authenticated.update(admin_authenticated=True, admin_issued_at=now, admin_last_activity=now, csrf_token="logout-csrf")
        self.assertEqual(self.client.get("/logout").status_code, 405)
        self.assertEqual(self.client.post("/logout", data={"csrf_token": "wrong"}).status_code, 400)
        response = self.client.post("/logout", data={"csrf_token": "logout-csrf"})
        self.assertEqual(response.status_code, 302)

    def test_route_inventory_has_no_unexpected_public_management_route(self):
        public = {"login", "healthz", "favicon"}
        management = {rule.endpoint for rule in module.app.url_map.iter_rules()} - {"static"} - public
        expected = {
            "index", "create", "execute_create", "restore_progress", "ui_preview", "change_port_route",
            "change_cookie_route", "set_backup_source_route", "run_backup_now_route", "rebuild_glpi_route",
            "diagnose", "testdb_route", "resetdb_route", "view_log", "logout",
        }
        self.assertEqual(management, expected)

    def test_provisioning_writes_only_hash_and_secret_with_mode_600(self):
        env_path = Path(self.temporary_directory.name) / ".env"
        env_path.write_text(
            "FLASK_SECRET_KEY=" + "a" * 64 + "\nBUILDER_PORT=5055\n",
            encoding="utf-8",
        )
        provision(env_path, "admin-user", TEST_PASSWORD, TEST_PASSWORD, TEST_TOTP_SECRET)
        text = env_path.read_text(encoding="utf-8")
        self.assertNotIn(TEST_PASSWORD, text)
        self.assertIn("BUILDER_BIND_IP=127.0.0.1", text)
        self.assertIn("BUILDER_ADMIN_PASSWORD_HASH='pbkdf2_sha256$600000$", text)
        self.assertIn(f"BUILDER_ADMIN_TOTP_SECRET={TEST_TOTP_SECRET}", text)
        self.assertEqual(env_path.stat().st_mode & 0o777, 0o600)
        check_configuration(env_path)

    def test_bind_ip_validation_and_wizard_selection(self):
        env_path = Path(self.temporary_directory.name) / "bind.env"
        env_path.write_text("BUILDER_BIND_IP=192.168.10.20\n", encoding="utf-8")
        self.assertEqual(validate_bind_ip("127.0.0.1"), "127.0.0.1")
        self.assertEqual(select_bind_ip(env_path, input_func=lambda _prompt: ""), "192.168.10.20")
        self.assertEqual(select_bind_ip(env_path, requested="192.168.10.21"), "192.168.10.21")
        with self.assertRaisesRegex(ValueError, "valid IPv4"):
            validate_bind_ip("nas.internal")
        for value in ("192.168.10.0/24", "192.168.10.20:5055", "224.0.0.1", "255.255.255.255"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_bind_ip(value)
        with self.assertRaisesRegex(ValueError, "IPv4"):
            validate_bind_ip("::1")
        with self.assertRaisesRegex(ValueError, "allow-all-interfaces"):
            select_bind_ip(env_path, requested="0.0.0.0")
        self.assertEqual(
            select_bind_ip(env_path, requested="0.0.0.0", allow_all_interfaces=True),
            "0.0.0.0",
        )
        self.assertEqual(validate_builder_port("5055"), 5055)
        for value in ("", "0", "65536", "five"):
            with self.subTest(port=value), self.assertRaises(ValueError):
                validate_builder_port(value)

    def test_all_interface_bind_requires_interactive_confirmation(self):
        env_path = Path(self.temporary_directory.name) / "bind.env"
        env_path.write_text("BUILDER_BIND_IP=127.0.0.1\n", encoding="utf-8")
        answers = iter(("0.0.0.0", "cancel"))
        with self.assertRaisesRegex(ValueError, "not confirmed"):
            select_bind_ip(env_path, input_func=lambda _prompt: next(answers))
        answers = iter(("0.0.0.0", "EXPOSE"))
        self.assertEqual(select_bind_ip(env_path, input_func=lambda _prompt: next(answers)), "0.0.0.0")

    def test_duplicate_env_keys_are_rejected_before_publication(self):
        env_path = Path(self.temporary_directory.name) / "duplicate.env"
        env_path.write_text(
            "BUILDER_BIND_IP=0.0.0.0\nBUILDER_BIND_IP=127.0.0.1\nBUILDER_PORT=5055\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "Duplicate configuration key"):
            select_bind_ip(env_path)

        env_path.write_text(
            "BUILDER_BIND_IP=127.0.0.1\nBUILDER_PORT=5055\nBUILDER_PORT=5056\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "Duplicate configuration key"):
            check_configuration(env_path)

    def test_reprovision_preserves_existing_bind_and_unrelated_values(self):
        env_path = Path(self.temporary_directory.name) / "existing.env"
        env_path.write_text(
            "FLASK_SECRET_KEY=" + "b" * 64
            + "\nBUILDER_BIND_IP=192.168.10.20\nBUILDER_PORT=5055\nEXTRA_SETTING=keep-me\n",
            encoding="utf-8",
        )
        provision(env_path, "admin-user", TEST_PASSWORD, TEST_PASSWORD, TEST_TOTP_SECRET)
        text = env_path.read_text(encoding="utf-8")
        self.assertIn("BUILDER_BIND_IP=192.168.10.20", text)
        self.assertIn("BUILDER_PORT=5055", text)
        self.assertIn("EXTRA_SETTING=keep-me", text)
        self.assertEqual(text.count("BUILDER_BIND_IP="), 1)
        check_configuration(env_path)


if __name__ == "__main__":
    unittest.main()
