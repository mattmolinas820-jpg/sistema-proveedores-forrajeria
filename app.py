import csv
import hmac
import io
import os
import secrets
from datetime import date
from decimal import Decimal

from flask import Flask, Response, abort, flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

import services
from database import DATABASE_URL, INTEGRITY_ERRORS, get_db, init_db


app = Flask(__name__)
cloud_mode = bool(DATABASE_URL)
secret_key = os.environ.get("SECRET_KEY")
admin_username = os.environ.get("ADMIN_USERNAME", "")
admin_password_hash = os.environ.get("ADMIN_PASSWORD_HASH", "")
if cloud_mode and not secret_key:
    raise RuntimeError("SECRET_KEY es obligatorio cuando DATABASE_URL está configurada.")
if cloud_mode and (not admin_username or not admin_password_hash):
    raise RuntimeError("ADMIN_USERNAME y ADMIN_PASSWORD_HASH son obligatorios para el acceso cloud.")
app.secret_key = secret_key or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=cloud_mode,
)
init_db()


@app.before_request
def protect_application():
    if cloud_mode and request.endpoint not in {"login", "static", "health"}:
        if not session.get("authenticated"):
            return redirect(url_for("login", next=request.full_path))

    if request.method == "POST":
        expected = session.get("_csrf_token", "")
        received = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
        if not expected or not hmac.compare_digest(expected, received):
            abort(400, description="Token de seguridad ausente o inválido. Actualizá la página e intentá otra vez.")


@app.context_processor
def inject_security_helpers():
    def csrf_token():
        if "_csrf_token" not in session:
            session["_csrf_token"] = secrets.token_urlsafe(32)
        return session["_csrf_token"]

    return {"csrf_token": csrf_token, "cloud_mode": cloud_mode}


@app.route("/login", methods=["GET", "POST"])
def login():
    if not cloud_mode:
        return redirect(url_for("index"))
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if hmac.compare_digest(username, admin_username) and check_password_hash(
            admin_password_hash, password
        ):
            session.clear()
            session["authenticated"] = True
            destination = request.args.get("next", "")
            if destination.startswith("/") and not destination.startswith("//"):
                return redirect(destination)
            return redirect(url_for("index"))
        flash("Usuario o contraseña incorrectos.", "danger")
    return render_template("login.html")


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login") if cloud_mode else url_for("index"))


@app.get("/health")
def health():
    with get_db() as connection:
        connection.execute("SELECT 1")
    return jsonify(status="ok")


def _read_date(value, label, required=False):
    value = (value or "").strip()
    if not value:
        if required:
            raise services.ValidationError(f"El campo {label} es obligatorio.")
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise services.ValidationError(f"La fecha de {label} no es válida.") from None


def _statement_filters():
    desde = _read_date(request.args.get("desde"), "inicio")
    hasta = _read_date(request.args.get("hasta"), "fin")
    if desde and hasta and desde > hasta:
        raise services.ValidationError("La fecha inicial no puede ser posterior a la fecha final.")
    return desde, hasta


def _spreadsheet_safe(value):
    value = str(value or "")
    if value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def _render_statement(proveedor_id, print_mode=False):
    try:
        desde, hasta = _statement_filters()
    except services.ValidationError as error:
        flash(str(error), "danger")
        return redirect(url_for("ver_ctacte", proveedor_id=proveedor_id))

    proveedor, movimientos, saldo_total = services.obtener_cuenta_proveedor(
        proveedor_id, desde, hasta
    )
    if not proveedor:
        flash("Proveedor no encontrado.", "danger")
        return redirect(url_for("listar_proveedores"))
    facturas_pendientes = services.obtener_facturas_pendientes(proveedor_id)
    return render_template(
        "ctacte.html",
        proveedor=proveedor,
        movimientos=movimientos,
        saldo_total=saldo_total,
        facturas_pendientes=facturas_pendientes,
        desde=desde or "",
        hasta=hasta or "",
        print_mode=print_mode,
        today=date.today().isoformat(),
    )


@app.template_filter("money")
def money_filter(cents):
    value = f"{Decimal(cents) / 100:,.2f}"
    return "$" + value.replace(",", "X").replace(".", ",").replace("X", ".")


@app.route("/")
def index():
    resumen = services.obtener_resumen()
    return render_template("dashboard.html", resumen=resumen, today=date.today().isoformat())


@app.route("/proveedores", methods=["GET", "POST"])
def listar_proveedores():
    if request.method == "POST":
        try:
            services.crear_proveedor(
                request.form.get("razon_social", ""),
                request.form.get("cuit_cuil", ""),
                request.form.get("email", ""),
                request.form.get("telefono", ""),
            )
            flash("Proveedor registrado con éxito.", "success")
        except services.ValidationError as error:
            flash(str(error), "danger")
        except INTEGRITY_ERRORS:
            flash("Ya existe un proveedor con ese CUIT/CUIL.", "danger")
        return redirect(url_for("listar_proveedores"))

    busqueda = request.args.get("q", "")
    proveedores = services.obtener_proveedores(busqueda)
    return render_template("proveedores.html", proveedores=proveedores, busqueda=busqueda)


@app.route("/proveedores/<int:proveedor_id>/editar", methods=["GET", "POST"])
def editar_proveedor(proveedor_id):
    proveedor = services.obtener_proveedor(proveedor_id)
    if not proveedor:
        flash("Proveedor no encontrado.", "danger")
        return redirect(url_for("listar_proveedores"))

    if request.method == "POST":
        try:
            services.actualizar_proveedor(
                proveedor_id,
                request.form.get("razon_social", ""),
                request.form.get("cuit_cuil", ""),
                request.form.get("email", ""),
                request.form.get("telefono", ""),
            )
            flash("Datos del proveedor actualizados.", "success")
            return redirect(url_for("listar_proveedores"))
        except services.ValidationError as error:
            flash(str(error), "danger")
        except INTEGRITY_ERRORS:
            flash("Ya existe otro proveedor con ese CUIT/CUIL.", "danger")

    return render_template("editar_proveedor.html", proveedor=proveedor)


@app.post("/proveedores/<int:proveedor_id>/estado")
def cambiar_estado(proveedor_id):
    try:
        services.cambiar_estado_proveedor(proveedor_id)
        flash("Estado del proveedor actualizado.", "success")
    except services.ValidationError as error:
        flash(str(error), "danger")
    return redirect(url_for("listar_proveedores"))


@app.route("/proveedores/<int:proveedor_id>/ctacte", methods=["GET", "POST"])
def ver_ctacte(proveedor_id):
    if request.method == "POST":
        try:
            tipo = request.form.get("tipo_comprobante", "")
            fecha_movimiento = _read_date(request.form.get("fecha"), "operación", required=True)
            vencimiento = _read_date(request.form.get("vencimiento"), "vencimiento")
            if vencimiento and vencimiento < fecha_movimiento:
                raise services.ValidationError("El vencimiento no puede ser anterior a la fecha del comprobante.")

            imputaciones = []
            for field_name, raw_amount in request.form.items():
                if not field_name.startswith("imputacion_") or not raw_amount.strip():
                    continue
                try:
                    factura_id = int(field_name.removeprefix("imputacion_"))
                except ValueError:
                    raise services.ValidationError("La factura seleccionada no es válida.") from None
                imputaciones.append((factura_id, services.amount_to_cents(raw_amount)))

            services.registrar_movimiento(
                proveedor_id=proveedor_id,
                tipo_comprobante=tipo,
                numero_comprobante=request.form.get("numero_comprobante", ""),
                monto_centavos=services.amount_to_cents(request.form.get("monto", "")),
                fecha_movimiento=fecha_movimiento,
                vencimiento=vencimiento if tipo in services.INVOICE_TYPES else None,
                observaciones=request.form.get("observaciones", ""),
                imputaciones=imputaciones,
            )
            flash("Comprobante registrado en la cuenta corriente.", "success")
            return redirect(url_for("ver_ctacte", proveedor_id=proveedor_id))
        except services.ValidationError as error:
            flash(str(error), "danger")
        except INTEGRITY_ERRORS as error:
            if getattr(error, "sqlstate", None) == "23505" or "UNIQUE constraint failed" in str(error):
                flash("Ya existe ese número de comprobante para el proveedor.", "danger")
            else:
                raise

    return _render_statement(proveedor_id)


@app.get("/proveedores/<int:proveedor_id>/ctacte/exportar.csv")
def exportar_ctacte(proveedor_id):
    try:
        desde, hasta = _statement_filters()
    except services.ValidationError as error:
        flash(str(error), "danger")
        return redirect(url_for("ver_ctacte", proveedor_id=proveedor_id))

    proveedor, movimientos, _ = services.obtener_cuenta_proveedor(proveedor_id, desde, hasta)
    if not proveedor:
        flash("Proveedor no encontrado.", "danger")
        return redirect(url_for("listar_proveedores"))

    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output)
    writer.writerow(
        ["Fecha", "Tipo", "Número", "Vencimiento", "Debe", "Haber", "Saldo", "Observaciones"]
    )
    for movement in movimientos:
        writer.writerow(
            [
                movement["fecha"],
                _spreadsheet_safe(movement["tipo_comprobante"]),
                _spreadsheet_safe(movement["numero_comprobante"]),
                movement["vencimiento"] or "",
                services.cents_to_decimal(movement["debe_centavos"]),
                services.cents_to_decimal(movement["haber_centavos"]),
                services.cents_to_decimal(movement["saldo_resultante_centavos"]),
                _spreadsheet_safe(movement["observaciones"]),
            ]
        )

    response = Response(output.getvalue(), content_type="text/csv; charset=utf-8")
    response.headers["Content-Disposition"] = (
        f'attachment; filename="cuenta_proveedor_{proveedor_id}.csv"'
    )
    return response


@app.get("/proveedores/<int:proveedor_id>/ctacte/imprimir")
def imprimir_ctacte(proveedor_id):
    return _render_statement(proveedor_id, print_mode=True)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
