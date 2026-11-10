import sqlite3
import tempfile
import unittest
from pathlib import Path

import database
import services


class ServicesTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "test.db"
        database.init_db()
        self.provider_id = services.crear_proveedor(
            "Proveedor de prueba", "30-71185817-9"
        )

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def add_movement(
        self, document_type, number, amount, movement_date, due_date=None, allocations=None
    ):
        return services.registrar_movimiento(
            proveedor_id=self.provider_id,
            tipo_comprobante=document_type,
            numero_comprobante=number,
            monto_centavos=amount,
            fecha_movimiento=movement_date,
            vencimiento=due_date,
            imputaciones=allocations,
        )

    def test_cuit_checksum_and_currency_precision(self):
        self.assertEqual(services.normalize_cuit("30711858179"), "30-71185817-9")
        with self.assertRaises(services.ValidationError):
            services.normalize_cuit("30-71185817-8")
        self.assertEqual(services.amount_to_cents("1234.56"), 123456)
        with self.assertRaises(services.ValidationError):
            services.amount_to_cents("12.345")

    def test_backdated_entries_rebuild_running_balances_and_payment_allocations(self):
        invoice_id = None
        self.add_movement("FACTURA_A", "F-2", 100000, "2026-03-10", "2026-03-25")
        with database.get_db() as connection:
            invoice_id = connection.execute(
                "SELECT id FROM movimientos_ctacte WHERE numero_comprobante = 'F-2'"
            ).fetchone()["id"]

        self.add_movement(
            "ORDEN_PAGO",
            "P-1",
            40000,
            "2026-03-20",
            allocations=[(invoice_id, 30000)],
        )
        self.add_movement("FACTURA_B", "F-1", 20000, "2026-03-05", "2026-03-20")

        proveedor, movements, balance = services.obtener_cuenta_proveedor(self.provider_id)
        self.assertIsNotNone(proveedor)
        self.assertEqual([row["numero_comprobante"] for row in movements], ["F-1", "F-2", "P-1"])
        self.assertEqual(
            [row["saldo_resultante_centavos"] for row in movements],
            [20000, 120000, 80000],
        )
        self.assertEqual(balance, 80000)
        pending = services.obtener_facturas_pendientes(self.provider_id)
        self.assertEqual(
            {row["numero_comprobante"]: row["pendiente_centavos"] for row in pending},
            {"F-1": 20000, "F-2": 70000},
        )

    def test_invalid_allocation_rolls_back_payment(self):
        self.add_movement("FACTURA_A", "F-1", 10000, "2026-03-10", "2026-03-25")
        with database.get_db() as connection:
            invoice_id = connection.execute(
                "SELECT id FROM movimientos_ctacte WHERE numero_comprobante = 'F-1'"
            ).fetchone()["id"]

        with self.assertRaises(services.ValidationError):
            self.add_movement(
                "ORDEN_PAGO",
                "P-1",
                10000,
                "2026-03-20",
                allocations=[(invoice_id, 10001)],
            )

        with database.get_db() as connection:
            count = connection.execute(
                "SELECT COUNT(*) AS count FROM movimientos_ctacte WHERE numero_comprobante = 'P-1'"
            ).fetchone()["count"]
        self.assertEqual(count, 0)

    def test_movement_history_cannot_be_edited_or_deleted(self):
        self.add_movement("FACTURA_A", "F-1", 10000, "2026-03-10", "2026-03-25")
        with self.assertRaises(sqlite3.DatabaseError):
            with database.get_db() as connection:
                connection.execute(
                    "DELETE FROM movimientos_ctacte WHERE numero_comprobante = 'F-1'"
                )

    def test_existing_scaffold_database_is_migrated(self):
        current_path = database.DB_PATH
        legacy_path = Path(self.temp_dir.name) / "legacy.db"
        try:
            database.DB_PATH = legacy_path
            connection = sqlite3.connect(legacy_path)
            try:
                connection.executescript(
                    """
                    CREATE TABLE proveedores (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        razon_social TEXT NOT NULL,
                        cuit_cuil TEXT UNIQUE NOT NULL,
                        email TEXT,
                        telefono TEXT,
                        estado INTEGER DEFAULT 1
                    );
                    CREATE TABLE movimientos_ctacte (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        proveedor_id INTEGER NOT NULL,
                        fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        tipo_comprobante TEXT NOT NULL,
                        numero_comprobante TEXT NOT NULL,
                        debe REAL DEFAULT 0.0,
                        haber REAL DEFAULT 0.0,
                        saldo_resultante REAL NOT NULL,
                        observaciones TEXT,
                        FOREIGN KEY (proveedor_id) REFERENCES proveedores (id)
                    );
                    INSERT INTO proveedores (razon_social, cuit_cuil)
                    VALUES ('Legado', '30-71185817-9');
                    INSERT INTO movimientos_ctacte (
                        proveedor_id, fecha, tipo_comprobante, numero_comprobante,
                        debe, haber, saldo_resultante
                    ) VALUES (1, '2026-03-10 12:34:56', 'FACTURA_A', 'F-OLD', 12.34, 0, 12.34);
                    """
                )
            finally:
                connection.close()

            database.init_db()
            with database.get_db() as connection:
                migrated = connection.execute(
                    "SELECT debe_centavos, saldo_resultante_centavos FROM movimientos_ctacte"
                ).fetchone()
            self.assertEqual(migrated["debe_centavos"], 1234)
            self.assertEqual(migrated["saldo_resultante_centavos"], 1234)
            _, filtered, _ = services.obtener_cuenta_proveedor(1, "2026-03-10", "2026-03-10")
            self.assertEqual(len(filtered), 1)
        finally:
            database.DB_PATH = current_path


if __name__ == "__main__":
    unittest.main()
