#!/usr/bin/env python3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as module
from tests.auth_test_support import authenticate


PROJECT = {
    "name": "glpi-test",
    "env_port": "8775",
    "active_port": "8775",
    "glpi_status": "running",
    "db_status": "running",
    "glpi_image": "glpi/glpi:11.0.8",
    "mariadb_image": "mariadb:11.4",
    "tz": "Europe/Brussels",
    "cookie_samesite": "Lax",
    "cookie_secure": "Off",
    "path": "/volume1/docker/glpi-test",
    "mappings": "0.0.0.0:8775 -> 8080/tcp",
    "latest_log": None,
    "logs": ["20260727-120000-diagnostics.log"],
    "backup_source": True,
    "backup_status": {
        "selected": True,
        "ready": True,
        "issues": [],
        "latest": {
            "name": "glpi-test.sql.gz",
            "created_at": "2026-07-27 12:00",
            "size_label": "10 MB",
            "checksum_manifest": True,
        },
    },
}


class ProfessionalUiTest(unittest.TestCase):
    def setUp(self):
        module.app.config.update(TESTING=True, SECRET_KEY="professional-ui-test")
        self.client = module.app.test_client()
        authenticate(self.client, module)
        self.snapshot = {
            "containers": [],
            "image_tags": ("glpi/glpi:11.0.8", "mariadb:11.4", "docker.n8n.io/n8nio/n8n:1.99.1", "teampasswordmanager/teampasswordmanager:12.158.302", "postgres:16-alpine", "mysql:5.7"),
        }

    def get_with_data(self, path):
        with patch.object(module, "dashboard_docker_snapshot", return_value=self.snapshot), \
             patch.object(module, "discover_projects", return_value=[PROJECT]), \
             patch.object(module, "discover_unmanaged_glpi_projects", return_value=[]), \
             patch.object(module, "scan_backup_choices", return_value={
                 "database": [("/backups/db.sql.gz", "db.sql.gz")],
                 "files": [("/backups/files.tar.gz", "files.tar.gz")],
             }), \
             patch.object(module, "suggest_free_host_port", return_value=8776):
            return self.client.get(path)

    def test_primary_pages_render_shared_navigation(self):
        for path, heading in (
            ("/", "Infrastructure overview"),
            ("/projects", "Applications"),
            ("/projects/new", "Add application"),
            ("/backups", "Backups"),
            ("/activity", "Activity"),
            ("/settings", "Settings"),
        ):
            with self.subTest(path=path):
                response = self.get_with_data(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn(heading.encode(), response.data)
                self.assertIn(b"aria-label=\"Primary\"", response.data)
                self.assertIn(b"Mobile navigation", response.data)

    def test_glpi_stays_in_shared_application_wizard(self):
        response = self.get_with_data("/applications/new?app=glpi")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Add application", response.data)
        self.assertIn(b"Application profile", response.data)
        self.assertIn(b"GLPI", response.data)
        self.assertIn(b"n8n", response.data)
        self.assertIn(b"Team Password Manager", response.data)
        self.assertIn(b"NAS preflight", response.data)
        self.assertIn(b"Deployment guardrails", response.data)
        self.assertIn(b"Deployment mode", response.data)
        self.assertIn(b"Application settings", response.data)
        self.assertNotIn(b"New project or restore", response.data)

    def test_profiles_always_show_isolated_restore_and_fail_closed_when_unverified(self):
        glpi = self.get_with_data("/applications/new?app=glpi")
        self.assertEqual(glpi.status_code, 200)
        self.assertIn(b"Isolated test restore", glpi.data)
        self.assertIn(b'value="isolated" disabled', glpi.data)

        n8n = self.get_with_data("/applications/new?app=n8n")
        self.assertEqual(n8n.status_code, 200)
        self.assertIn(b'name="app_type" value="n8n"', n8n.data)
        self.assertIn(b'aria-current="true" href="/applications/new?app=n8n"', n8n.data)
        self.assertIn(b"Isolated test restore", n8n.data)
        self.assertNotIn(b'value="quarantine" disabled', n8n.data)
        self.assertIn(b"Verified n8n database backup", n8n.data)
        self.assertIn(b"docker.n8n.io/n8nio/n8n:1.99.1", n8n.data)
        self.assertNotIn(b"docker.n8n.io/n8nio/n8n:latest", n8n.data)
        self.assertIn(b"postgres:16-alpine", n8n.data)
        self.assertIn(b"installed", n8n.data)

        tpm = self.get_with_data("/applications/new?app=teampasswordmanager")
        self.assertEqual(tpm.status_code, 200)
        self.assertIn(b'name="app_type" value="teampasswordmanager"', tpm.data)
        self.assertIn(b"Isolated test restore", tpm.data)
        self.assertNotIn(b'value="quarantine" disabled', tpm.data)

    def test_project_detail_preserves_existing_safe_actions(self):
        response = self.get_with_data("/projects/glpi-test")
        self.assertEqual(response.status_code, 200)
        for route in (
            b"/change-port",
            b"/change-cookie",
            b"/rebuild-glpi",
            b"/diagnose",
            b"/testdb",
            b"/resetdb",
            b"/run-backup",
        ):
            self.assertIn(route, response.data)
        self.assertNotIn(b"builder-auth.json", response.data)
        self.assertIn(b'role="dialog"' if False else b"<dialog", response.data)
        self.assertIn(b'name="confirm_rebuild"', response.data)
        self.assertIn(b'name="confirm_project"', response.data)
        self.assertIn(b"Advanced recovery", response.data)

    def test_status_api_and_refresh_asset_are_read_only(self):
        with patch.object(module, "dashboard_docker_snapshot", return_value=self.snapshot), \
             patch.object(module, "discover_projects", return_value=[PROJECT]):
            response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["projects"][0]["name"], "glpi-test")
        asset = self.client.get("/assets/app.js")
        self.assertEqual(asset.status_code, 200)
        self.assertIn(b"setInterval(refresh, 20000)", asset.data)
        self.assertIn(b"showModal", asset.data)

    def test_backup_inventory_exposes_pairing_and_retention(self):
        with patch.object(module, "scan_backup_choices", return_value={
            "database": [("/backups/nightly.sql.gz", "nightly.sql.gz")],
            "files": [("/backups/nightly.tar.gz", "nightly.tar.gz")],
        }):
            response = self.get_with_data("/backups?verify=1")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Verification completed", response.data)
        self.assertIn(b"Complete pair", response.data)
        self.assertIn(b"Configured per project", response.data)
        self.assertIn(b"Synology backup dispatcher", response.data)

    def test_operational_metadata_reports_local_versions_and_drift(self):
        project = module.enrich_project_operational_metadata(
            {**PROJECT, "tz": "UTC"},
            ("glpi/glpi:11.0.8", "glpi/glpi:11.0.9", "mariadb:11.4"),
        )
        self.assertEqual(project["newer_glpi_image"], "glpi/glpi:11.0.9")
        self.assertEqual(project["contract_status"], "Review")
        self.assertTrue(project["configuration_drift"])

    def test_unknown_project_is_clean_404(self):
        response = self.get_with_data("/projects/glpi-missing")
        self.assertEqual(response.status_code, 404)

    def test_wizard_has_review_first_and_conditional_restore_fields(self):
        response = self.get_with_data("/projects/new")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Review execution plan", response.data)
        self.assertIn(b'value="restore"', response.data)
        self.assertIn(b'value="fresh"', response.data)
        self.assertIn(b"Local allowlist only", response.data)
        self.assertIn(b'name="csrf_token"', response.data)
        self.assertIn(
            b'pattern="[a-z0-9][a-z0-9_\\-]{2,50}"',
            response.data,
        )

    def test_add_application_is_single_entry_point_and_offers_glpi(self):
        projects = self.get_with_data("/projects")
        self.assertEqual(projects.status_code, 200)
        self.assertEqual(projects.data.count(b">Add application</a>"), 1)
        self.assertNotIn(b">Add GLPI</a>", projects.data)

        chooser = self.get_with_data("/applications/new")
        self.assertEqual(chooser.status_code, 200)
        self.assertIn(b">GLPI</strong>", chooser.data)
        self.assertIn(b'href="/applications/new?app=glpi"', chooser.data)
        self.assertIn(b">n8n</strong>", chooser.data)
        self.assertIn(b">Team Password Manager</strong>", chooser.data)

    def test_overview_and_backups_use_application_wide_language(self):
        overview = self.get_with_data("/")
        self.assertIn(b"Review application backups", overview.data)
        self.assertNotIn(b"Review GLPI backups", overview.data)
        backups = self.get_with_data("/backups")
        self.assertIn(b"<th>Application</th>", backups.data)
        self.assertIn("The shared Synology dispatcher", module.APPLICATION_DETAIL)

    def test_navigation_get_routes_do_not_acquire_mutation_lock(self):
        for path in ("/", "/projects", "/projects/new", "/backups", "/activity", "/settings"):
            with self.subTest(path=path):
                self.assertEqual(self.get_with_data(path).status_code, 200)
                self.assertFalse(module.MUTATION_LOCK.locked())

    def test_setting_labels_keep_space_before_their_values(self):
        response = self.get_with_data("/settings")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"column-gap:24px", response.data)
        self.assertIn(b"Two-factor authentication</dt><dd>", response.data)
        self.assertIn(b"policy-list", response.data)
        self.assertNotIn(b"Arbitrary Compose editing", response.data)
        self.assertNotIn(b"Automatic updates", response.data)

    def test_settings_show_effective_deployment_defaults(self):
        response = self.get_with_data("/settings")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Deployment defaults", response.data)
        self.assertIn(b"32768 bytes", response.data)
        self.assertIn(b"32 KiB", response.data)
        self.assertIn(b"GLPI internal port</dt><dd>8080", response.data)

    def test_generated_yaml_page_and_download_redact_literal_secrets(self):
        source = """services:
  glpi:
    environment:
      GLPI_DB_PASSWORD: ${GLPI_DB_PASSWORD}
      API_TOKEN: literal-token-value
      PRIVATE_KEY: |-
        first-private-line
        second-private-line
      - DATABASE_PASSWORD=list-style-secret
      ENDPOINT: https://admin:private@example.invalid/api
"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            compose_path = Path(temporary_directory) / "docker-compose.yml"
            compose_path.write_text(source, encoding="utf-8")
            with patch.object(
                module,
                "professional_ui_snapshot",
                return_value=(self.snapshot, [PROJECT]),
            ), patch.object(module, "compose_file", return_value=compose_path):
                page = self.client.get("/projects/glpi-test/compose")
                download = self.client.get(
                    "/projects/glpi-test/compose?download=1"
                )
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Generated YAML", page.data)
        self.assertIn(b"${GLPI_DB_PASSWORD}", page.data)
        self.assertNotIn(b"literal-token-value", page.data)
        self.assertNotIn(b"first-private-line", page.data)
        self.assertNotIn(b"second-private-line", page.data)
        self.assertNotIn(b"list-style-secret", page.data)
        self.assertNotIn(b"admin:private", page.data)
        self.assertEqual(download.status_code, 200)
        self.assertIn(b"${GLPI_DB_PASSWORD}", download.data)
        self.assertIn(b"[REDACTED]", download.data)
        self.assertIn(
            "attachment;",
            download.headers["Content-Disposition"],
        )


if __name__ == "__main__":
    unittest.main()
