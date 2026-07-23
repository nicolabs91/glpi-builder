import tempfile
import unittest
from pathlib import Path

import app as module


class RequestUrlLimitTests(unittest.TestCase):
    def test_glpi_entrypoint_persists_32_kib_request_line_limit(self):
        self.assertIn(
            "LimitRequestLine 32768",
            module.GLPI_ENTRY_COMMAND,
        )

    def test_generated_compose_contains_request_line_limit_once(self):
        project = "request-limit-test"
        env = {
            "MARIADB_IMAGE": "mariadb:11.4",
            "GLPI_IMAGE": "glpi/glpi:11.0.8",
            "GLPI_HTTP_PORT": "8775",
            "GLPI_CONTAINER_PORT": "8080",
            "GLPI_SESSION_COOKIE_SAMESITE": "Lax",
            "GLPI_SESSION_COOKIE_SECURE": "Off",
            "MARIADB_ROOT_PASSWORD": "root-password",
            "GLPI_DB_NAME": "glpi",
            "GLPI_DB_USER": "glpi",
            "GLPI_DB_PASSWORD": "db-password",
            "TZ": "Europe/Brussels",
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            original_base_path = module.BASE_PATH
            try:
                module.BASE_PATH = Path(temporary_directory)
                (module.BASE_PATH / project).mkdir()
                module.write_compose(project, env)
                compose = (module.BASE_PATH / project / "docker-compose.yml").read_text()
            finally:
                module.BASE_PATH = original_base_path

        self.assertEqual(compose.count("LimitRequestLine 32768"), 1)


if __name__ == "__main__":
    unittest.main()
