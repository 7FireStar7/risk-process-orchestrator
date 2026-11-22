"""Раскладка: из сырой таблицы raw_<t> в чистую stg_<t>.

В stg попадают только описанные в реестре столбцы, с настоящими типами.
Правила отбора строк:
  - значения '' и 'None' становятся NULL;
  - строка с проблемой уровня WARNING попадает в stg (плохое значение -> NULL);
  - строка, где столбец уровня ERROR пуст или не подходит по типу, в stg не попадает;
  - при дубликатах уникального ключа остаётся первая строка, остальные отклоняются.
"""
from psycopg import sql
from .loader import raw_name

PG_TYPE = {"integer": "bigint", "number": "numeric", "text": "text"}
TYPE_RX = {"integer": r"^-?\d+$", "number": r"^-?\d+(\.\d+)?$"}
# формат даты -> (тип в PostgreSQL, регулярка, способ преобразования)
DATE_SPEC = {
    "YYYY-MM-DD":          ("date",      r"^\d{4}-\d{2}-\d{2}$",                    "cast_date"),
    "YYYY-MM-DD HH:MI:SS": ("timestamp", r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$",  "cast_ts"),
    "DD.MM.YYYY":          ("date",      r"^\d{2}\.\d{2}\.\d{4}$",                  "to_date:DD.MM.YYYY"),
    "DD.MM.YYYY H:MI":     ("timestamp", r"^\d{2}\.\d{2}\.\d{4} \d{1,2}:\d{2}$",    "to_ts:DD.MM.YYYY HH24:MI"),
    "YYYYMMDD":            ("date",      r"^\d{8}$",                                "to_date:YYYYMMDD"),
}


def stg_name(table: str) -> str:
    return "stg_" + table


def spec(rule: dict):
    """(тип PostgreSQL, регулярка проверки или None, способ преобразования)."""
    t = rule["type"]
    if t == "date" and rule["date_format"] in DATE_SPEC:
        return DATE_SPEC[rule["date_format"]]
    if t in ("integer", "number"):
        return PG_TYPE[t], TYPE_RX[t], "cast_" + PG_TYPE[t]
    return "text", None, "text"


def _v(alias: str, col: str):
    """Значение столбца без пробелов; '' и 'None' -> NULL."""
    return sql.SQL("nullif(nullif(btrim({c}), ''), 'None')").format(c=sql.Identifier(alias, col))


def _convert(v, rx, how: str):
    if how == "text":
        return v
    lit = sql.Literal(rx)
    if how.startswith("cast_"):
        target = {"cast_bigint": "bigint", "cast_numeric": "numeric",
                  "cast_date": "date", "cast_ts": "timestamp"}[how]
        return sql.SQL("CASE WHEN {v} ~ {rx} THEN {v}::" + target + " END").format(v=v, rx=lit)
    kind, fmt = how.split(":", 1)
    fn = "to_date({v}, {f})" if kind == "to_date" else "to_timestamp({v}, {f})::timestamp"
    return sql.SQL("CASE WHEN {v} ~ {rx} THEN " + fn + " END").format(v=v, rx=lit, f=sql.Literal(fmt))


def ensure_stg_table(conn, table: str, rules: list) -> None:
    stg = sql.Identifier(stg_name(table))
    with conn.cursor() as cur:
        cur.execute(sql.SQL("CREATE TABLE IF NOT EXISTS {} (process_id uuid NOT NULL, "
                            "row_num int NOT NULL, loaded_at timestamptz NOT NULL DEFAULT now())")
                    .format(stg))
        for r in rules:
            if r["mode"] == "VALUE":
                cur.execute(sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS {} " + spec(r)[0])
                            .format(stg, sql.Identifier(r["column"])))
        cur.execute(sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} (process_id)")
                    .format(sql.Identifier("ix_" + stg_name(table) + "_pid"), stg))


def build_stage_sql(table: str, process_id: str, header: list, rules: list):
    """Собирает INSERT ... SELECT. Возвращает SQL или None, если нет столбцов."""
    present = set(header)
    cols = [r for r in rules if r["mode"] == "VALUE"]
    if not cols:
        return None
    names, exprs, conds, windows = [], [], [], []
    for r in cols:
        c = r["column"]
        pgt, rx, how = spec(r)
        names.append(sql.Identifier(c))
        if c not in present:
            exprs.append(sql.SQL("NULL::" + pgt))
            continue
        v = _v("r", c)
        exprs.append(_convert(v, rx, how))
        if r["severity"] == "ERROR":
            conds.append(sql.SQL("{v} IS NOT NULL").format(v=v))
            if rx:
                conds.append(sql.SQL("{v} ~ {rx}").format(v=v, rx=sql.Literal(rx)))
        if r["unique"]:
            k = "rn_%d" % len(windows)
            windows.append(sql.SQL("row_number() OVER (PARTITION BY {v} ORDER BY t.row_num) AS {k}")
                           .format(v=_v("t", c), k=sql.Identifier(k)))
            conds.append(sql.SQL("r.{k} = 1").format(k=sql.Identifier(k)))
    inner_extra = sql.SQL("").join(sql.SQL(", ") + w for w in windows) if windows else sql.SQL("")
    where = sql.SQL(" AND ").join(conds) if conds else sql.SQL("TRUE")
    return sql.SQL(
        "INSERT INTO {stg} (process_id, row_num, {names}) "
        "SELECT r.process_id, r.row_num, {exprs} "
        "FROM (SELECT t.*{extra} FROM {raw} t WHERE t.process_id = {pid}) r "
        "WHERE {where}").format(
        stg=sql.Identifier(stg_name(table)),
        names=sql.SQL(", ").join(names), exprs=sql.SQL(", ").join(exprs),
        extra=inner_extra, raw=sql.Identifier(raw_name(table)),
        pid=sql.Literal(str(process_id)), where=where)


def stage_rows(conn, table: str, process_id: str, header: list, rules: list):
    """Возвращает (загружено в stg, отклонено, всего в raw)."""
    q = build_stage_sql(table, process_id, header, rules)
    with conn.cursor() as cur:
        cur.execute(sql.SQL("SELECT count(*) FROM {} WHERE process_id = {}").format(
            sql.Identifier(raw_name(table)), sql.Literal(str(process_id))))
        total = cur.fetchone()[0]
        inserted = 0
        if q is not None:
            cur.execute(q)
            inserted = cur.rowcount
    return inserted, total - inserted, total
