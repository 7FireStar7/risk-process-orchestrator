"""Реестр правил: чтение YAML и синхронизация с таблицей validation_rule."""
import yaml
from psycopg.types.json import Jsonb


def load_registry(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def sync_rules(conn, registry: dict, table: str) -> int:
    t = registry["tables"][table]
    rows = []
    for c in t["columns"]:
        rw = c.get("required_when")
        rows.append((table, c["name"], c["severity"], "VALUE", c.get("type"),
                     c.get("date_format"), bool(c.get("unique")), c.get("note"),
                     Jsonb(rw) if rw else None))
    for name in t.get("presence_only", []):
        rows.append((table, name, "WARNING", "PRESENCE_ONLY", None, None, False, None, None))
    for name in t.get("unclassified", []):
        rows.append((table, name, "WARNING", "PRESENCE_ONLY", None, None, False,
                     "не классифицирован: проверяется только наличие", None))
    with conn.cursor() as cur:
        cur.execute("DELETE FROM validation_rule WHERE table_name = %s", (table,))
        cur.executemany(
            "INSERT INTO validation_rule (table_name, column_name, severity, check_mode, "
            "data_type, date_format, is_unique, note, required_when) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)", rows)
    return len(rows)


def load_rules(conn, table: str) -> list:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, severity, check_mode, data_type, date_format, "
            "is_unique, required_when FROM validation_rule WHERE table_name = %s "
            "ORDER BY column_name", (table,))
        keys = ["column", "severity", "mode", "type", "date_format", "unique", "required_when"]
        return [dict(zip(keys, r)) for r in cur.fetchall()]
