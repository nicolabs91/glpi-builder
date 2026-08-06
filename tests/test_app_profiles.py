import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app_profiles import (
    MANIFEST_NAME,
    build_environment,
    get_profile,
    manifest,
    profile_catalog,
    read_manifest,
    render_compose,
    validate_image,
)


class ApplicationProfileTests(unittest.TestCase):
    def test_catalog_contains_initial_supported_apps(self):
        self.assertEqual({item.key for item in profile_catalog()}, {"n8n", "teampasswordmanager"})

    @patch("app_profiles._secret", side_effect=["db-secret", "encryption-secret"])
    def test_n8n_compose_keeps_secrets_in_env_placeholders(self, _secret):
        profile = get_profile("n8n")
        env = build_environment(profile, "n8n-prod", 5678, profile.default_image, "Europe/Brussels")
        compose = render_compose(profile, env)
        self.assertIn("${N8N_ENCRYPTION_KEY}", compose)
        self.assertIn("${POSTGRES_PASSWORD}", compose)
        self.assertNotIn("encryption-secret", compose)
        self.assertIn("condition: service_healthy", compose)
        self.assertIn("/volume1/docker/n8n-prod/data:/home/node/.n8n:rw", compose)

    @patch("app_profiles._secret", side_effect=["user-secret", "root-secret"])
    def test_team_password_manager_compose_isolated_database(self, _secret):
        profile = get_profile("teampasswordmanager")
        env = build_environment(profile, "passwords", 8780, profile.default_image, "Europe/Brussels")
        compose = render_compose(profile, env)
        self.assertIn("passwords-db", compose)
        self.assertIn("${MYSQL_ROOT_PASSWORD}", compose)
        self.assertNotIn("root-secret", compose)
        self.assertIn("TPM_CONFIG_HOSTNAME: passwords-db", compose)
        self.assertIn("image: mysql:5.7", compose)
        self.assertIn("/volume1/docker/passwords/application:/var/www/html:rw", compose)

    @patch("app_profiles._secret", side_effect=["user-secret", "root-secret"])
    def test_tpm_quarantine_compose_has_no_external_route(self, _secret):
        profile = get_profile("teampasswordmanager")
        env = build_environment(
            profile, "passwords-test", 8781,
            "teampasswordmanager/teampasswordmanager:12.158.302",
            "Europe/Brussels", quarantine=True, bind_address="192.0.2.10",
            expires_at="2026-08-19T12:00:00",
        )
        compose = render_compose(profile, env)
        self.assertIn("internal: true", compose)
        self.assertIn("${APP_BIND_ADDRESS}:${APP_HTTP_PORT}:80", compose)
        self.assertIn("no-new-privileges:true", compose)
        self.assertIn("pids_limit: 256", compose)
        self.assertIn("mem_limit: 1g", compose)
        self.assertIn("BUILDER_TEST_ENVIRONMENT: ${BUILDER_QUARANTINE}", compose)
        self.assertTrue(manifest(profile, env)["quarantine"])
        self.assertEqual(manifest(profile, env)["bind_address"], "192.0.2.10")
        self.assertEqual(manifest(profile, env)["expires_at"], "2026-08-19T12:00:00")

    def test_rejects_images_outside_profile_allowlist(self):
        with self.assertRaises(ValueError):
            validate_image(get_profile("n8n"), "evil.invalid/n8n:latest")

    def test_manifest_is_fail_closed(self):
        profile = get_profile("n8n")
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / "n8n-prod"
            folder.mkdir()
            data = {
                "PROJECT_NAME": "n8n-prod",
                "APP_HTTP_PORT": "5678",
                "APP_IMAGE": profile.default_image,
            }
            (folder / MANIFEST_NAME).write_text(json.dumps(manifest(profile, data)), encoding="utf-8")
            self.assertEqual(read_manifest(folder)["type"], "n8n")
            broken = json.loads((folder / MANIFEST_NAME).read_text(encoding="utf-8"))
            broken["project"] = "another-project"
            (folder / MANIFEST_NAME).write_text(json.dumps(broken), encoding="utf-8")
            self.assertIsNone(read_manifest(folder))

    def test_generated_compose_profiles_validate_with_docker_compose(self):
        if subprocess.run(
            ["docker", "compose", "version"], capture_output=True
        ).returncode:
            self.skipTest("Docker Compose plugin is unavailable")
        for key in ("n8n", "teampasswordmanager"):
            with self.subTest(profile=key), tempfile.TemporaryDirectory() as root:
                profile = get_profile(key)
                env = build_environment(profile, f"{key}-test", 18775, profile.default_image, "Europe/Brussels")
                folder = Path(root)
                (folder / "docker-compose.yml").write_text(render_compose(profile, env), encoding="utf-8")
                (folder / ".env").write_text(
                    "".join(f"{name}={value}\n" for name, value in env.items()), encoding="utf-8"
                )
                os.chmod(folder / ".env", 0o600)
                result = subprocess.run(
                    ["docker", "compose", "config", "--quiet"],
                    cwd=folder, capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
