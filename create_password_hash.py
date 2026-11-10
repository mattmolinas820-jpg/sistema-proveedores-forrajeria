from getpass import getpass

from werkzeug.security import generate_password_hash


password = getpass("Contraseña nueva (no se mostrará): ")
confirmation = getpass("Repetí la contraseña: ")
if not password or password != confirmation:
    raise SystemExit("Las contraseñas no coinciden o están vacías.")

print("\nCopiá este valor en ADMIN_PASSWORD_HASH en Render:\n")
print(generate_password_hash(password))
