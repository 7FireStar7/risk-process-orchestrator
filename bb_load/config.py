"""Настройки подключения к БД из файла .env (без сторонних библиотек)."""
import os
from pathlib import Path

NEED = ["DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD"]


def load_env(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def db_conninfo() -> str:
    from psycopg.conninfo import make_conninfo
    load_env()
    missing = [k for k in NEED if not os.environ.get(k)]
    if missing:
        raise SystemExit("В файле .env не хватает: " + ", ".join(missing))
    return make_conninfo(
        host=os.environ["DB_HOST"], port=os.environ["DB_PORT"],
        dbname=os.environ["DB_NAME"], user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
    )
