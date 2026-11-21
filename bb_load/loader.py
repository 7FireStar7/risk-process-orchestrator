"""Загрузка прочитанных строк в сырую таблицу raw_<таблица> (все столбцы - text)."""
from psycopg import sql


def raw_name(table: str) -> str:
    return "raw_" + table


def ensure_raw_table(conn, table: str, header: list) -> None:
    raw = raw_name(table)
    with conn.cursor() as cur:
        cur.execute(sql.SQL("CREATE TABLE IF NOT EXISTS {} (process_id uuid NOT NULL, "
                            "row_num int NOT NULL)").format(sql.Identifier(raw)))
        for col in header:
            cur.execute(sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS {} text")
                        .format(sql.Identifier(raw), sql.Identifier(col)))
        cur.execute(sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} (process_id)")
                    .format(sql.Identifier("ix_" + raw + "_pid"), sql.Identifier(raw)))


def copy_rows(conn, table: str, process_id: str, header: list, rows: list) -> int:
    """Пакетная загрузка через COPY. Значения не меняются."""
    cols = ["process_id", "row_num"] + header
    q = sql.SQL("COPY {} ({}) FROM STDIN").format(
        sql.Identifier(raw_name(table)),
        sql.SQL(", ").join(sql.Identifier(c) for c in cols))
    with conn.cursor() as cur, cur.copy(q) as cp:
        for i, r in enumerate(rows, 1):
            cp.write_row([str(process_id), i] + r)
    return len(rows)
