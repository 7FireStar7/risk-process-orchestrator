"""Оркестрация: вызывает шаги сервиса по порядку и записывает результат каждого шага."""
import os

import httpx

from bb_load.config import load_env
from .db import STEPS, finish_step, start_step, write_issues

load_env()
BB_LOAD_URL = os.environ.get("BB_LOAD_URL", "http://127.0.0.1:8001")


def call_service(step, pid, table, file_path):
    """Один HTTP-вызов шага. Сквозной process_id передаётся в заголовке X-Correlation-ID."""
    r = httpx.post(f"{BB_LOAD_URL}/v1/steps/{step}",
                   json={"table": table, "file_path": file_path},
                   headers={"X-Correlation-ID": pid}, timeout=60)
    try:
        data = r.json()
    except ValueError:
        data = None
    return r.status_code, r.headers.get("X-Correlation-ID"), data, r.text


def run_pipeline(conn, pid, sids, table, file_path, call=call_service):
    """Выполняет шаги по порядку. Остановка: сервис недоступен, ошибка ответа,
    не подтверждён сквозной ID или сервис сообщил stop=true."""
    for name, _ in STEPS:
        sid = sids[name]
        start_step(conn, sid)
        try:
            code, echoed, data, text = call(name, pid, table, file_path)
        except Exception as e:
            finish_step(conn, sid, "ERROR", f"сервис bb_load недоступен ({type(e).__name__})")
            return
        if code != 200 or not isinstance(data, dict):
            detail = data.get("detail") if isinstance(data, dict) else None
            finish_step(conn, sid, "ERROR", f"сервис ответил {code}: {detail or (text or '')[:200]}")
            return
        if echoed != pid:
            finish_step(conn, sid, "ERROR", "сервис не подтвердил сквозной ID")
            return
        write_issues(conn, pid, sid, table, data.get("issues", []))
        finish_step(conn, sid, data["status"], data.get("message"),
                    data.get("rows_read"), data.get("rows_loaded"), data.get("rows_rejected"))
        if data.get("stop"):
            return
