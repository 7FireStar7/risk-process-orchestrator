"""Проверка содержимого сырой таблицы SQL-запросами по правилам validation_rule."""
from psycopg import sql
from .loader import raw_name

# Значения, которые считаются «пусто» (в cbonds пустое пишется как None)
NULL_MARKERS = ("", "None")

TYPE_RX = {"integer": r"^-?\d+$", "number": r"^-?\d+(\.\d+)?$"}
DATE_RX = {
    "YYYY-MM-DD": r"^\d{4}-\d{2}-\d{2}$",
    "YYYY-MM-DD HH:MI:SS": r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$",
    "DD.MM.YYYY": r"^\d{2}\.\d{2}\.\d{4}$",
    "DD.MM.YYYY H:MI": r"^\d{2}\.\d{2}\.\d{4} \d{1,2}:\d{2}$",
    "YYYYMMDD": r"^\d{8}$",
}


def _empty(col: str):
    return sql.SQL("nullif(nullif(btrim({c}), ''), 'None') IS NULL").format(c=sql.Identifier(col))


def structural_issues(rules: list, res) -> list:
    """Проблемы структуры файла: (столбец, серьёзность, тип, плохих, всего)."""
    out, header = [], set(res.header)
    known = {r["column"] for r in rules}
    total = len(res.rows) + res.rows_bad_length
    for r in rules:
        if r["column"] not in header:
            sev = r["severity"] if r["mode"] == "VALUE" else "WARNING"
            out.append((r["column"], sev, "MISSING_COLUMN", None, None))
    for h in res.header:
        if h not in known:
            out.append((h, "WARNING", "UNEXPECTED_COLUMN", None, None))
    if res.rows_bad_length:
        out.append((None, "ERROR", "BAD_ROW_LENGTH", res.rows_bad_length, total))
    if res.stray_quotes:
        out.append((None, "WARNING", "STRAY_QUOTE", res.stray_quotes, total))
    if not res.rows:
        out.append((None, "ERROR", "NO_DATA", None, 0))
    return out


def build_checks(table: str, rules: list, header: list) -> list:
    """Список проверок: (столбец, серьёзность, тип проблемы, запрос, параметры)."""
    raw, present, checks = sql.Identifier(raw_name(table)), set(header), []
    for r in rules:
        col, sev = r["column"], r["severity"]
        if r["mode"] != "VALUE" or col not in present:
            continue
        c = sql.Identifier(col)
        # 1. пустое значение (с условием required_when, если оно задано)
        rw = r.get("required_when")
        if rw and rw.get("column") in present:
            q = sql.SQL("SELECT count(*) FILTER (WHERE {e} AND strpos(coalesce({cc}, ''), %s) > 0), "
                        "count(*) FROM {t} WHERE process_id = %s").format(
                e=_empty(col), cc=sql.Identifier(rw["column"]), t=raw)
            checks.append((col, sev, "EMPTY_CONDITIONAL", q, ("{}", rw["contains"])))
        else:
            q = sql.SQL("SELECT count(*) FILTER (WHERE {e}), count(*) FROM {t} "
                        "WHERE process_id = %s").format(e=_empty(col), t=raw)
            checks.append((col, sev, "EMPTY", q, ()))
        # 2. тип значения
        rx = TYPE_RX.get(r["type"]) if r["type"] in TYPE_RX else DATE_RX.get(r["date_format"]) \
            if r["type"] == "date" else None
        if rx:
            q = sql.SQL("SELECT count(*) FILTER (WHERE NOT ({e}) AND btrim({c}) !~ %s), count(*) "
                        "FROM {t} WHERE process_id = %s").format(e=_empty(col), c=c, t=raw)
            issue = "NOT_NUMBER" if r["type"] in TYPE_RX else "BAD_DATE_FORMAT"
            checks.append((col, sev, issue, q, (rx,)))
        # 3. дубликаты ключа
        if r["unique"]:
            q = sql.SQL("SELECT count(*), (SELECT count(*) FROM {t} WHERE process_id = %s) FROM ("
                        "SELECT {c} FROM {t} WHERE process_id = %s AND NOT ({e}) "
                        "GROUP BY {c} HAVING count(*) > 1) d").format(t=raw, c=c, e=_empty(col))
            checks.append((col, "ERROR", "DUPLICATE", q, ("PID_FIRST",)))
    return checks


def run_checks(conn, process_id: str, checks: list) -> list:
    """Выполняет проверки, возвращает только найденные проблемы."""
    found = []
    with conn.cursor() as cur:
        for col, sev, issue, q, extra in checks:
            if issue == "EMPTY_CONDITIONAL":
                params = (extra[1], str(process_id))
            elif issue == "DUPLICATE":
                params = (str(process_id), str(process_id))
            elif extra:
                params = (extra[0], str(process_id))
            else:
                params = (str(process_id),)
            cur.execute(q, params)
            bad, total = cur.fetchone()
            if bad:
                found.append((col, sev, issue, bad, total))
    return found
