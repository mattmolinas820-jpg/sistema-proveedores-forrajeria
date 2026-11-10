import re
import tempfile
import unittest
from pathlib import Path

import app as web_app
import database
import services
from werkzeug.security import generate_password_hash


class WebSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        self.original_cloud_mode = web_app.cloud_mode
        self.original_admin_username = web_app.admin_username
        self.original_admin_password_hash = web_app.admin_password_hash
        database.DB_PATH = Path(self.temp_dir.name) / "web-test.db"
        database.init_db()
        web_app.app.config["TESTING"] = True
        web_app.app.config["SECRET_KEY"] = "test-secret"
        self.client = web_app.app.test_client()

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        web_app.cloud_mode = self.original_cloud_mode
        web_app.admin_username = self.original_admin_username
        web_app.admin_password_hash = self.original_admin_password_hash
        self.temp_dir.cleanup()

    def test_post_without_csrf_token_is_rejected(self):
        response = self.client.post(
            "/proveedores",
            data={"razon_social": "Demo", "cuit_cuil": "30-71185817-9"},
        )
        self.assertEqual(response.status_code, 400)

    def test_provider_form_submits_with_csrf_token(self):
        page = self.client.get("/proveedores")
        match = re.search(rb'name="csrf_token" value="([^"]+)"', page.data)
        self.assertIsNotNone(match)

        response = self.client.post(
            "/proveedores",
            data={
                "csrf_token": match.group(1).decode(),
                "razon_social": "Proveedor web",
                "cuit_cuil": "30-71185817-9",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Proveedor web", response.data)

    def test_dashboard_renders_animated_balance_chart(self):
        proveedor_id = services.crear_proveedor(
            "Proveedor del gráfico", "30-71185817-9"
        )
        services.registrar_movimiento(
            proveedor_id,
            "FACTURA_A",
            "F-CHART",
            25000,
            "2026-03-10",
            "2026-03-25",
        )

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Mayores saldos pendientes", response.data)
        self.assertIn(b"Proveedor del gr", response.data)
        self.assertIn(b"chart-bar", response.data)
        self.assertIn(b"width: 100%", response.data)

    def test_cloud_pages_require_login(self):
        web_app.cloud_mode = True
        web_app.admin_username = "admin"
        web_app.admin_password_hash = generate_password_hash("test-password")

        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.location)
        sync_response = self.client.get("/sync-version")
        self.assertEqual(sync_response.status_code, 302)

        login_page = self.client.get("/login")
        token = re.search(rb'name="csrf_token" value="([^"]+)"', login_page.data).group(1).decode()
        response = self.client.post(
            "/login",
            data={"csrf_token": token, "username": "admin", "password": "test-password"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/sync-version").json, {"revision": 0})


if __name__ == "__main__":
    unittest.main()
