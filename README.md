# Sistema de proveedores y cuenta corriente

Aplicación web para gestionar proveedores, comprobantes, pagos y vencimientos. En uso local guarda datos en SQLite; para compartirla entre dispositivos utiliza Render y PostgreSQL administrado en Supabase.

## Requisitos

- Python 3.10 o superior
- Windows PowerShell

## Instalación y ejecución

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

Abrí `http://127.0.0.1:5000`. La base `proveedores.db` se crea automáticamente junto a la aplicación. Para inicializarla sin iniciar el servidor:

```powershell
python setup.py
```

Los importes se guardan como centavos enteros para evitar errores de redondeo. La cuenta corriente ordena los movimientos por fecha y, ante igualdad, por orden de registro. Los movimientos y las imputaciones no se editan ni eliminan; corregí un asiento con un nuevo comprobante.

## Publicarla para usarla desde distintos dispositivos

El archivo SQLite local no se comparte automáticamente. La versión cloud usa PostgreSQL y requiere un proyecto en Supabase y un servicio web en Render. No publiques la base ni pegues credenciales en el código.

1. Creá un proyecto en Supabase y guardá su contraseña de base de datos. En el panel del proyecto, abrí **Connect** y copiá la cadena de **Session pooler**. Usá el puerto `5432` y conservá o agregá `sslmode=require`. Reemplazá el marcador de contraseña; si contiene caracteres especiales, codificalos para URL. El pooler de sesión es compatible con redes IPv4 y con este servidor persistente de Flask; no uses el pooler de transacciones.
2. Creá un repositorio **privado** en GitHub y subí el código del proyecto. No agregues `proveedores.db`, archivos `.env` ni credenciales.
3. En Render, elegí **New > Blueprint** y conectá el repositorio. Render leerá `render.yaml`, configurará Gunicorn para escuchar en el puerto asignado y solicitará las variables secretas. El plan configurado es gratuito; el servicio puede tardar en responder cuando vuelve de un período de inactividad.
4. En las variables privadas de Render, configurá `DATABASE_URL` con la cadena de conexión de Supabase, `ADMIN_USERNAME` con tu usuario y `ADMIN_PASSWORD_HASH` con el hash que genera:

   ```powershell
   .\.venv\Scripts\python.exe create_password_hash.py
   ```

   Render genera `SECRET_KEY` automáticamente desde `render.yaml`. No compartas la contraseña ni el hash.
5. Si querés conservar los datos locales, antes de desplegar migrá la base SQLite una sola vez a la base Supabase nueva y vacía. Detené la app local, configurá `DATABASE_URL` temporalmente en PowerShell con la misma cadena del pooler de sesión y ejecutá:

   ```powershell
   $env:DATABASE_URL = "TU_URL_DE_SUPABASE"
   .\.venv\Scripts\python.exe migrate_sqlite_to_postgres.py
   Remove-Item Env:DATABASE_URL
   ```

   El destino debe estar vacío. El script copia proveedores, movimientos e imputaciones, preserva sus identificadores y no sobrescribe datos. No ejecutes la migración más de una vez sobre la misma base.
6. Confirmá la creación del servicio y esperá a que el despliegue termine. La dirección del servicio será la que usarás desde cualquier dispositivo; el acceso está protegido por usuario y contraseña.

Si solo querés probar el despliegue sin datos previos, omití el paso de migración. La aplicación inicializa el esquema PostgreSQL de Supabase al arrancar.

Todos los dispositivos deben abrir la misma dirección de Render para trabajar sobre los mismos datos. La aplicación comprueba cambios cada 15 segundos y actualiza la pantalla automáticamente; si estás completando un formulario, espera a que lo guardes o elijas actualizar. La aplicación local abierta en `localhost` usa SQLite y no comparte cambios con Supabase.

### Ejecutar localmente contra PostgreSQL

Definí `DATABASE_URL`, `SECRET_KEY`, `ADMIN_USERNAME` y `ADMIN_PASSWORD_HASH` antes de iniciar `python app.py`. Si `DATABASE_URL` no está definida, el sistema sigue usando SQLite local.

## Funcionalidades

- Alta, edición, búsqueda y activación/inactivación de proveedores.
- Validación del dígito verificador CUIT/CUIL y control de duplicados.
- Registro de facturas, notas de débito, órdenes de pago, recibos y notas de crédito.
- Saldo acumulado y filtros de estado de cuenta por fecha.
- Imputación parcial o total de pagos a una o varias facturas; el remanente queda a cuenta.
- Panel de facturas vencidas y con vencimiento dentro de los próximos 30 días.
- Exportación del estado filtrado a CSV compatible con Excel y vista imprimible para guardar como PDF desde el navegador.

## Pruebas

```powershell
python -m unittest discover -s tests
```

En modo cloud todas las páginas requieren inicio de sesión; las cookies son seguras y los formularios validan protección CSRF. Configurá respaldos en Supabase y limitá el acceso al repositorio de despliegue.
