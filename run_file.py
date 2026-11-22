"""Проверка одного файла: python run_file.py <таблица> <путь к CSV>

Пример: python run_file.py cbonds_tradings_stocks data/cbonds_tradings_stocks.csv
"""
import argparse
import sys
import psycopg
from psycopg.types.json import Jsonb

from bb_load.config import db_conninfo
from bb_load.registry import load_registry, sync_rules, load_rules
from bb_load.reader import read_file
from bb_load.loader import ensure_raw_table, copy_rows
from bb_load.validator import structural_issues, build_checks, run_checks
from bb_load.stager import ensure_stg_table, stage_rows

STEPS = [("read", 1), ("load", 2), ("validate", 3), ("stage", 4)]


def start_step(conn, sid):
    conn.execute("UPDATE step_run SET status='IN_PROGRESS', started_at=clock_timestamp() "
                 "WHERE step_id=%s", (sid,))
    conn.commit()


def write_issues(conn, pid, sid, table, issues):
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO validation_issue (process_id, step_id, table_name, column_name, "
            "severity, issue_type, rows_bad, rows_total) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            [(str(pid), sid, table, c, s, t, b, n) for c, s, t, b, n in issues])


def finish_step(conn, sid, message=None, read=None, loaded=None, rejected=None, status=None):
    if status is None:
        status = conn.execute("SELECT step_status_from_issues(%s)", (sid,)).fetchone()[0]
    conn.execute("UPDATE step_run SET status=%s, finished_at=clock_timestamp(), message=%s, "
                 "rows_read=%s, rows_loaded=%s, rows_rejected=%s WHERE step_id=%s",
                 (status, message, read, loaded, rejected, sid))
    conn.commit()
    return status


def process(conn, pid, sids, table, entry, path):
    # ---- шаг 1: чтение файла и проверка структуры ----
    start_step(conn, sids["read"])
    try:
        res = read_file(path, entry["file_format"])
    except Exception as e:  # файл не открылся или не разобрался
        finish_step(conn, sids["read"], f"не удалось прочитать файл: {e}", status="ERROR")
        return
    rules = load_rules(conn, table)
    issues = structural_issues(rules, res)
    write_issues(conn, pid, sids["read"], table, issues)
    st = finish_step(conn, sids["read"],
                     f"режим: {res.mode}, строк данных: {len(res.rows)}, пустых строк пропущено: {res.lines_blank}",
                     read=len(res.rows) + res.rows_bad_length, rejected=res.rows_bad_length)
    blocking = any(i[2] == "NO_DATA" or (i[2] == "MISSING_COLUMN" and i[1] == "ERROR") for i in issues)
    if blocking:
        return
    # ---- шаг 2: загрузка в сырую таблицу ----
    start_step(conn, sids["load"])
    try:
        ensure_raw_table(conn, table, res.header)
        n = copy_rows(conn, table, pid, res.header, res.rows)
    except Exception as e:
        conn.rollback()
        finish_step(conn, sids["load"], f"ошибка загрузки: {e}", status="ERROR")
        return
    finish_step(conn, sids["load"], "загружено в raw_" + table, read=n, loaded=n, status="OK")
    # ---- шаг 3: SQL-проверки содержимого ----
    start_step(conn, sids["validate"])
    try:
        found = run_checks(conn, pid, build_checks(table, rules, res.header))
        write_issues(conn, pid, sids["validate"], table, found)
    except Exception as e:
        conn.rollback()
        finish_step(conn, sids["validate"], f"ошибка проверки: {e}", status="ERROR")
        return
    finish_step(conn, sids["validate"], f"найдено проблем: {len(found)}", read=len(res.rows))
    # ---- шаг 4: раскладка в чистую таблицу stg_<таблица> ----
    start_step(conn, sids["stage"])
    try:
        ensure_stg_table(conn, table, rules)
        ins, rej, tot = stage_rows(conn, table, pid, res.header, rules)
    except Exception as e:
        conn.rollback()
        finish_step(conn, sids["stage"], f"ошибка раскладки: {e}", status="ERROR")
        return
    finish_step(conn, sids["stage"], f"в stg_{table}: {ins}, отклонено: {rej}",
                read=tot, loaded=ins, rejected=rej, status="WARNING" if rej else "OK")


def report(conn, pid):
    p = conn.execute("SELECT status, started_at, finished_at FROM process_run WHERE process_id=%s",
                     (pid,)).fetchone()
    print(f"\nprocess_id: {pid}\nИТОГ: {p[0]}\n")
    print(f"{'шаг':10}{'статус':13}{'мс':>8}  сообщение")
    for r in conn.execute("SELECT step_name, status, duration_ms, message FROM v_step_timeline "
                          "WHERE process_id=%s ORDER BY step_order", (pid,)):
        print(f"{r[0]:10}{r[1]:13}{(r[2] if r[2] is not None else '-')!s:>8}  {r[3] or ''}")
    rows = conn.execute("SELECT severity, column_name, issue_type, rows_bad, rows_total "
                        "FROM validation_issue WHERE process_id=%s "
                        "ORDER BY severity, column_name", (pid,)).fetchall()
    print("\nПроблемы:" if rows else "\nПроблем не найдено.")
    for s, c, t, b, n in rows:
        print(f"  {s:8}{(c or '-'):22}{t:20}{'' if b is None else f'{b} из {n}'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("table")
    ap.add_argument("file")
    ap.add_argument("--registry", default="rules/bb_load.yaml")
    a = ap.parse_args()
    reg = load_registry(a.registry)
    if a.table not in reg["tables"]:
        sys.exit(f"Таблицы {a.table} нет в реестре. Доступны: {', '.join(reg['tables'])}")
    entry = reg["tables"][a.table]
    with psycopg.connect(db_conninfo()) as conn:
        sync_rules(conn, reg, a.table)
        pid = conn.execute("INSERT INTO process_run (process_type, params) VALUES (%s, %s) "
                           "RETURNING process_id",
                           ("FILE_CHECK", Jsonb({"table": a.table, "file": a.file}))).fetchone()[0]
        sids = {}
        for name, order in STEPS:
            sids[name] = conn.execute(
                "INSERT INTO step_run (process_id, service, step_name, step_order) "
                "VALUES (%s, 'bb_load', %s, %s) RETURNING step_id", (pid, name, order)).fetchone()[0]
        conn.commit()
        process(conn, pid, sids, a.table, entry, a.file)
        report(conn, pid)


if __name__ == "__main__":
    main()