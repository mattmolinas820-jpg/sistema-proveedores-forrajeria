"""Copy the local SQLite database into an empty PostgreSQL database."""

import argparse
import sqlite3
from decimal import Decimal
from pathlib import Path

import database


def _columns(connection, table):
    return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}


def _to_cents(value):
    return int((Decimal(str(value or 0)) * 100).quantize(Decimal("1")))


def migrate(source_path):
    if not database.DATABASE_URL:
        raise RuntimeError("Configurá DATABASE_URL antes de ejecutar la migración.")
    if not source_path.is_file():
        raise FileNotFoundError(f"No se encontró la base SQLite: {source_path}")

    source = sqlite3.connect(f"file:{source_path.resolve().as_posix()}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    try:
        tables = {
            row["name"]
            for row in source.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if not {"proveedores", "movimientos_ctacte"}.issubset(tables):
            raise RuntimeError("El archivo SQLite no contiene las tablas esperadas del sistema.")

        providers = source.execute("SELECT * FROM proveedores ORDER BY id").fetchall()
        movement_columns = _columns(source, "movimientos_ctacte")
        movements = source.execute("SELECT * FROM movimientos_ctacte ORDER BY id").fetchall()
        allocations = (
            source.execute("SELECT * FROM imputaciones ORDER BY id").fetchall()
            if "imputaciones" in tables
            else []
        )
        allocation_columns = _columns(source, "imputaciones") if "imputaciones" in tables else set()
    finally:
        source.close()

    database.init_db()
    with database.get_db() as target:
        target_count = sum(
            target.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"]
            for table in ("proveedores", "movimientos_ctacte", "imputaciones")
        )
        if target_count:
            raise RuntimeError(
                "La base PostgreSQL no está vacía. La migración no sobrescribe ni mezcla datos."
            )

        for provider in providers:
            target.execute(
                """
                INSERT INTO proveedores (id, razon_social, cuit_cuil, email, telefono, estado, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    provider["id"],
                    provider["razon_social"],
                    provider["cuit_cuil"],
                    provider["email"] or "",
                    provider["telefono"] or "",
                    provider["estado"] if provider["estado"] is not None else 1,
                    provider["created_at"] if "created_at" in provider.keys() else "",
                ),
            )

        for movement in movements:
            if "debe_centavos" in movement_columns:
                debe = movement["debe_centavos"]
                haber = movement["haber_centavos"]
            else:
                debe = _to_cents(movement["debe"])
                haber = _to_cents(movement["haber"])
            if "saldo_resultante_centavos" in movement_columns:
                balance = movement["saldo_resultante_centavos"]
            else:
                balance = _to_cents(movement["saldo_resultante"])

            target.execute(
                """
                INSERT INTO movimientos_ctacte (
                    id, proveedor_id, fecha, tipo_comprobante, numero_comprobante,
                    debe_centavos, haber_centavos, saldo_resultante_centavos,
                    vencimiento, observaciones, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    movement["id"],
                    movement["proveedor_id"],
                    str(movement["fecha"]),
                    movement["tipo_comprobante"],
                    movement["numero_comprobante"],
                    debe,
                    haber,
                    balance,
                    movement["vencimiento"] if "vencimiento" in movement_columns else None,
                    movement["observaciones"] or "",
                    movement["created_at"] if "created_at" in movement_columns else "",
                ),
            )

        for allocation in allocations:
            if "importe_centavos" in allocation_columns:
                amount = allocation["importe_centavos"]
            else:
                amount = _to_cents(allocation["importe"])
            target.execute(
                """
                INSERT INTO imputaciones (id, pago_id, factura_id, importe_centavos, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    allocation["id"],
                    allocation["pago_id"],
                    allocation["factura_id"],
                    amount,
                    allocation["created_at"] if "created_at" in allocation_columns else "",
                ),
            )

        for table in ("proveedores", "movimientos_ctacte", "imputaciones"):
            target.execute(
                """
                SELECT setval(
                    pg_get_serial_sequence(?, 'id'),
                    GREATEST(COALESCE((SELECT MAX(id) FROM {}), 1), 1),
                    EXISTS (SELECT 1 FROM {} )
                )
                """.format(table, table),
                (table,),
            )

    print(
        "Migración completada: "
        f"{len(providers)} proveedor(es), {len(movements)} movimiento(s), "
        f"{len(allocations)} imputación(es)."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sqlite",
        type=Path,
        default=database.DB_PATH,
        help="Ruta a proveedores.db (por defecto, la base local del proyecto).",
    )
    arguments = parser.parse_args()
    migrate(arguments.sqlite)
