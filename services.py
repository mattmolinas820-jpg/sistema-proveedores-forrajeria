import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from database import DATABASE_URL, get_db, touch_sync_revision


INVOICE_TYPES = {"FACTURA_A", "FACTURA_B", "FACTURA_C", "NOTA_DEBITO"}
PAYMENT_TYPES = {"ORDEN_PAGO", "RECIBO"}
CREDIT_TYPES = {"ORDEN_PAGO", "RECIBO", "NOTA_CREDITO"}
DOCUMENT_TYPES = INVOICE_TYPES | CREDIT_TYPES


class ValidationError(ValueError):
    pass


def normalize_cuit(value):
    value = value.strip()
    if not re.fullmatch(r"\d{2}-?\d{8}-?\d", value):
        raise ValidationError("Ingresá un CUIT/CUIL de 11 dígitos.")

    digits = re.sub(r"\D", "", value)
    weights = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)
    check_digit = 11 - sum(int(digit) * weight for digit, weight in zip(digits[:10], weights)) % 11
    if check_digit == 11:
        check_digit = 0
    elif check_digit == 10:
        check_digit = 9
    if int(digits[-1]) != check_digit:
        raise ValidationError("El dígito verificador del CUIT/CUIL no es válido.")

    return f"{digits[:2]}-{digits[2:10]}-{digits[10]}"


def amount_to_cents(value):
    try:
        amount = Decimal(str(value).strip().replace(",", "."))
    except InvalidOperation:
        raise ValidationError("Ingresá un importe válido.") from None
    if not amount.is_finite():
        raise ValidationError("Ingresá un importe válido.")
    cents = amount * 100
    if cents != cents.to_integral_value() or cents <= 0:
        raise ValidationError("El importe debe ser mayor que cero y tener hasta dos decimales.")
    if cents >= 10**12:
        raise ValidationError("El importe supera el máximo permitido.")
    return int(cents)


def cents_to_decimal(cents):
    return f"{Decimal(cents) / 100:.2f}"


def _rebuild_balances(connection, proveedor_id):
    movements = connection.execute(
        """
        SELECT id, debe_centavos, haber_centavos
        FROM movimientos_ctacte
        WHERE proveedor_id = ?
        ORDER BY fecha, id
        """,
        (proveedor_id,),
    ).fetchall()
    balance = 0
    for movement in movements:
        balance += movement["debe_centavos"] - movement["haber_centavos"]
        connection.execute(
            "UPDATE movimientos_ctacte SET saldo_resultante_centavos = ? WHERE id = ?",
            (balance, movement["id"]),
        )


def _invoice_outstanding(connection, factura_id):
    row = connection.execute(
        """
        SELECT m.debe_centavos - COALESCE(SUM(i.importe_centavos), 0) AS pendiente
        FROM movimientos_ctacte AS m
        LEFT JOIN imputaciones AS i ON i.factura_id = m.id
        WHERE m.id = ?
        GROUP BY m.id
        """,
        (factura_id,),
    ).fetchone()
    return row["pendiente"] if row else 0


def registrar_movimiento(
    proveedor_id,
    tipo_comprobante,
    numero_comprobante,
    monto_centavos,
    fecha_movimiento,
    vencimiento=None,
    observaciones="",
    imputaciones=None,
):
    if tipo_comprobante not in DOCUMENT_TYPES:
        raise ValidationError("Seleccioná un tipo de comprobante válido.")
    if not numero_comprobante.strip():
        raise ValidationError("El número de comprobante es obligatorio.")
    if len(numero_comprobante.strip()) > 50:
        raise ValidationError("El número de comprobante no puede superar los 50 caracteres.")
    if monto_centavos <= 0:
        raise ValidationError("El importe debe ser mayor que cero.")
    if tipo_comprobante in INVOICE_TYPES and vencimiento is None:
        raise ValidationError("Indicá la fecha de vencimiento de la factura.")
    if tipo_comprobante not in INVOICE_TYPES and vencimiento is not None:
        raise ValidationError("Solo las facturas y notas de débito llevan vencimiento.")

    allocations = imputaciones or []
    if allocations and tipo_comprobante not in PAYMENT_TYPES:
        raise ValidationError("Solo las órdenes de pago y recibos se pueden imputar a facturas.")
    if len({factura_id for factura_id, _ in allocations}) != len(allocations):
        raise ValidationError("No repitas la misma factura en las imputaciones.")
    if sum(amount for _, amount in allocations) > monto_centavos:
        raise ValidationError("La suma imputada no puede superar el importe del pago.")

    debe = monto_centavos if tipo_comprobante in INVOICE_TYPES else 0
    haber = 0 if tipo_comprobante in INVOICE_TYPES else monto_centavos

    with get_db() as connection:
        if DATABASE_URL:
            connection.begin_write(proveedor_id)
        else:
            connection.execute("BEGIN IMMEDIATE")
        proveedor = connection.execute(
            "SELECT estado FROM proveedores WHERE id = ?", (proveedor_id,)
        ).fetchone()
        if not proveedor:
            raise ValidationError("El proveedor no existe.")
        if not proveedor["estado"]:
            raise ValidationError("No se pueden registrar movimientos para un proveedor inactivo.")
        duplicate = connection.execute(
            """
            SELECT 1
            FROM movimientos_ctacte
            WHERE proveedor_id = ? AND tipo_comprobante = ? AND numero_comprobante = ?
            """,
            (proveedor_id, tipo_comprobante, numero_comprobante.strip()),
        ).fetchone()
        if duplicate:
            raise ValidationError("Ya existe ese número de comprobante para el proveedor.")

        cursor = connection.execute(
            """
            INSERT INTO movimientos_ctacte (
                proveedor_id, fecha, tipo_comprobante, numero_comprobante,
                debe_centavos, haber_centavos, vencimiento, observaciones
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (
                proveedor_id,
                fecha_movimiento,
                tipo_comprobante,
                numero_comprobante.strip(),
                debe,
                haber,
                vencimiento,
                observaciones.strip(),
            ),
        )
        pago_id = cursor.fetchone()["id"]

        for factura_id, importe_centavos in allocations:
            invoice = connection.execute(
                """
                SELECT proveedor_id, tipo_comprobante, fecha
                FROM movimientos_ctacte
                WHERE id = ?
                """,
                (factura_id,),
            ).fetchone()
            if (
                not invoice
                or invoice["proveedor_id"] != proveedor_id
                or invoice["tipo_comprobante"] not in INVOICE_TYPES
            ):
                raise ValidationError("La factura seleccionada no pertenece a este proveedor.")
            if invoice["fecha"] > fecha_movimiento:
                raise ValidationError("No se puede imputar un pago a una factura posterior.")
            if importe_centavos <= 0 or importe_centavos > _invoice_outstanding(connection, factura_id):
                raise ValidationError("El importe imputado supera el saldo pendiente de una factura.")
            connection.execute(
                """
                INSERT INTO imputaciones (pago_id, factura_id, importe_centavos)
                VALUES (?, ?, ?)
                """,
                (pago_id, factura_id, importe_centavos),
            )

        _rebuild_balances(connection, proveedor_id)
        touch_sync_revision(connection)
        balance = connection.execute(
            """
            SELECT saldo_resultante_centavos
            FROM movimientos_ctacte
            WHERE proveedor_id = ?
            ORDER BY fecha DESC, id DESC
            LIMIT 1
            """,
            (proveedor_id,),
        ).fetchone()
        return balance["saldo_resultante_centavos"] if balance else 0


def crear_proveedor(razon_social, cuit_cuil, email="", telefono=""):
    razon_social = razon_social.strip()
    if not razon_social:
        raise ValidationError("La razón social es obligatoria.")
    if len(razon_social) > 150:
        raise ValidationError("La razón social no puede superar los 150 caracteres.")
    cuit_cuil = normalize_cuit(cuit_cuil)
    with get_db() as connection:
        cursor = connection.execute(
            """
            INSERT INTO proveedores (razon_social, cuit_cuil, email, telefono)
            VALUES (?, ?, ?, ?)
            RETURNING id
            """,
            (razon_social, cuit_cuil, email.strip(), telefono.strip()),
        )
        proveedor_id = cursor.fetchone()["id"]
        touch_sync_revision(connection)
        return proveedor_id


def actualizar_proveedor(proveedor_id, razon_social, cuit_cuil, email="", telefono=""):
    razon_social = razon_social.strip()
    if not razon_social:
        raise ValidationError("La razón social es obligatoria.")
    if len(razon_social) > 150:
        raise ValidationError("La razón social no puede superar los 150 caracteres.")
    cuit_cuil = normalize_cuit(cuit_cuil)
    with get_db() as connection:
        cursor = connection.execute(
            """
            UPDATE proveedores
            SET razon_social = ?, cuit_cuil = ?, email = ?, telefono = ?
            WHERE id = ?
            """,
            (razon_social, cuit_cuil, email.strip(), telefono.strip(), proveedor_id),
        )
        if cursor.rowcount == 0:
            raise ValidationError("El proveedor no existe.")
        touch_sync_revision(connection)


def cambiar_estado_proveedor(proveedor_id):
    with get_db() as connection:
        cursor = connection.execute(
            "UPDATE proveedores SET estado = CASE estado WHEN 1 THEN 0 ELSE 1 END WHERE id = ?",
            (proveedor_id,),
        )
        if cursor.rowcount == 0:
            raise ValidationError("El proveedor no existe.")
        touch_sync_revision(connection)


def obtener_proveedor(proveedor_id):
    with get_db() as connection:
        return connection.execute(
            "SELECT * FROM proveedores WHERE id = ?", (proveedor_id,)
        ).fetchone()


def obtener_proveedores(busqueda=""):
    query = f"%{busqueda.strip()}%"
    with get_db() as connection:
        return connection.execute(
            """
            SELECT p.*,
                   COALESCE(SUM(m.debe_centavos - m.haber_centavos), 0) AS saldo_centavos
            FROM proveedores AS p
            LEFT JOIN movimientos_ctacte AS m ON m.proveedor_id = p.id
            WHERE p.razon_social LIKE ? OR p.cuit_cuil LIKE ?
            GROUP BY p.id
            ORDER BY p.estado DESC, LOWER(p.razon_social)
            """,
            (query, query),
        ).fetchall()


def obtener_cuenta_proveedor(proveedor_id, desde=None, hasta=None):
    with get_db() as connection:
        proveedor = connection.execute(
            "SELECT * FROM proveedores WHERE id = ?", (proveedor_id,)
        ).fetchone()
        if not proveedor:
            return None, [], 0

        all_movements = connection.execute(
            """
            SELECT m.*,
                   COALESCE((
                       SELECT SUM(i.importe_centavos)
                       FROM imputaciones AS i
                       WHERE i.factura_id = m.id
                   ), 0) AS imputado_centavos
            FROM movimientos_ctacte AS m
            WHERE m.proveedor_id = ?
            ORDER BY m.fecha, m.id
            """,
            (proveedor_id,),
        ).fetchall()

    movements = []
    balance = 0
    for row in all_movements:
        balance += row["debe_centavos"] - row["haber_centavos"]
        movement = dict(row)
        movement["saldo_resultante_centavos"] = balance
        movement_day = movement["fecha"][:10]
        if (desde is None or movement_day >= desde) and (
            hasta is None or movement_day <= hasta
        ):
            movements.append(movement)
    return proveedor, movements, balance


def obtener_facturas_pendientes(proveedor_id):
    with get_db() as connection:
        return connection.execute(
            """
            SELECT m.id, m.fecha, m.vencimiento, m.tipo_comprobante,
                   m.numero_comprobante, m.debe_centavos,
                   m.debe_centavos - COALESCE(SUM(i.importe_centavos), 0) AS pendiente_centavos
            FROM movimientos_ctacte AS m
            LEFT JOIN imputaciones AS i ON i.factura_id = m.id
            WHERE m.proveedor_id = ?
              AND m.tipo_comprobante IN ('FACTURA_A', 'FACTURA_B', 'FACTURA_C', 'NOTA_DEBITO')
            GROUP BY m.id
            HAVING pendiente_centavos > 0
            ORDER BY m.vencimiento, m.fecha, m.id
            """,
            (proveedor_id,),
        ).fetchall()


def obtener_resumen():
    hoy = date.today()
    horizonte = (hoy + timedelta(days=30)).isoformat()
    with get_db() as connection:
        proveedores = connection.execute(
            """
            SELECT p.*,
                   COALESCE(SUM(m.debe_centavos - m.haber_centavos), 0) AS saldo_centavos
            FROM proveedores AS p
            LEFT JOIN movimientos_ctacte AS m ON m.proveedor_id = p.id
            GROUP BY p.id
            ORDER BY LOWER(p.razon_social)
            """
        ).fetchall()
        vencimientos = connection.execute(
            """
            SELECT p.id AS proveedor_id, p.razon_social, m.id, m.fecha, m.vencimiento,
                   m.numero_comprobante, m.tipo_comprobante,
                   m.debe_centavos - COALESCE(SUM(i.importe_centavos), 0) AS pendiente_centavos
            FROM movimientos_ctacte AS m
            JOIN proveedores AS p ON p.id = m.proveedor_id
            LEFT JOIN imputaciones AS i ON i.factura_id = m.id
            WHERE m.tipo_comprobante IN ('FACTURA_A', 'FACTURA_B', 'FACTURA_C', 'NOTA_DEBITO')
              AND m.vencimiento <= ?
              AND m.vencimiento IS NOT NULL
            GROUP BY m.id
            HAVING pendiente_centavos > 0
            ORDER BY m.vencimiento, p.razon_social
            """,
            (horizonte,),
        ).fetchall()

    saldos = [proveedor["saldo_centavos"] for proveedor in proveedores]
    top_saldos = sorted(
        (
            proveedor
            for proveedor in proveedores
            if proveedor["estado"] and proveedor["saldo_centavos"] > 0
        ),
        key=lambda proveedor: proveedor["saldo_centavos"],
        reverse=True,
    )[:5]
    max_saldo = top_saldos[0]["saldo_centavos"] if top_saldos else 0
    return {
        "cantidad_proveedores": sum(proveedor["estado"] for proveedor in proveedores),
        "saldo_total_centavos": sum(saldos),
        "vencimientos": vencimientos,
        "proveedores_con_mayor_saldo": [
            {
                "razon_social": proveedor["razon_social"],
                "saldo_centavos": proveedor["saldo_centavos"],
                "porcentaje": round(proveedor["saldo_centavos"] / max_saldo * 100),
            }
            for proveedor in top_saldos
        ],
    }
