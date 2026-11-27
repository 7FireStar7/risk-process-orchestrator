"""Запись состояния процесса в БД: process_run, step_run, validation_issue."""
from psycopg.types.json import Jsonb

SERVICE = "bb_load"
STEPS = [("read", 1), ("load", 2), ("validate", 3), ("stage", 4)]


def create_process(conn, table: str, file_name: str):
    """Создаёт запуск и четыре шага в статусе PENDING. Возвращает (process_id, {шаг: step_id})."""
    pid = str(conn.execute(
        "INSERT INTO process_run (process_type, params) VALUES (%s, %s) RETURNING process_id",
        ("FILE_CHECK", Jsonb({"table": table, "file": file_name, "service": SERVICE}))).fetchone()[0])
    sids = {}
    for name, order in STEPS:
        sids[name] = conn.execute(
            "INSERT INTO step_run (process_id, service, step_name, step_order) "
            "VALUES (%s, %s, %s, %s) RETURNING step_id", (pid, SERVICE, name, order)).fetchone()[0]
    conn.commit()
    return pid, sids


def start_step(conn, sid):
    conn.execute("UPDATE step_run SET status = 'IN_PROGRESS', started_at = clock_timestamp() "
                 "WHERE step_id = %s", (sid,))
    conn.commit()


def finish_step(conn, sid, status, message=None, read=None, loaded=None, rejected=None):
    conn.execute("UPDATE step_run SET status = %s, finished_at = clock_timestamp(), message = %s, "
                 "rows_read = %s, rows_loaded = %s, rows_rejected = %s WHERE step_id = %s",
                 (status, message, read, loaded, rejected, sid))
    conn.commit()


def write_issues(conn, pid, sid, table, issues):
    for i in issues:
        conn.execute(
            "INSERT INTO validation_issue (process_id, step_id, table_name, column_name, severity, "
            "issue_type, rows_bad, rows_total) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (pid, sid, table, i.get("column"), i["severity"], i["issue_type"],
             i.get("rows_bad"), i.get("rows_total")))
