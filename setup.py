"""Initialize the SQLite database without overwriting application files."""

from database import DB_PATH, init_db


if __name__ == "__main__":
    init_db()
    print(f"Base de datos inicializada en {DB_PATH}")
