"""Оркестратор (FastAPI): запускает процесс, создаёт сквозной process_id,
вызывает сервис bb_load, записывает шаги и показывает дашборд.

Запуск:  uvicorn orchestrator.app:app --port 8000
Дашборд: http://localhost:8000      Swagger: http://localhost:8000/docs
"""
import re
import shutil
import uuid
from pathlib import Path

import httpx
import psycopg
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

import dashboard as dash
from bb_load.config import db_conninfo, load_env
from .db import create_process
from .pipeline import BB_LOAD_URL, run_pipeline

load_env()
INCOMING = Path("incoming")

app = FastAPI(
    title="Оркестратор бизнес-процессов",
    description="Запускает обработку файла, передаёт сервису сквозной идентификатор "
                "(X-Correlation-ID) и записывает статус и время каждого шага.",
    version="0.1.0",
)


def _service_tables():
    """Список таблиц от сервиса или None, если сервис недоступен."""
    try:
        r = httpx.get(f"{BB_LOAD_URL}/v1/tables", timeout=2)
        r.raise_for_status()
        return [t["table"] for t in r.json()]
    except Exception:
        return None


def _launch_form(tables) -> str:
    if tables is not None:
        status = '<span class="badge ok">bb_load доступен</span>'
        field = ('<select name="table">' + "".join(f'<option>{dash.esc(t)}</option>' for t in tables)
                 + "</select>")
    else:
        status = '<span class="badge err">bb_load недоступен</span>'
        field = '<input name="table" placeholder="cbonds_tradings_stocks" required>'
    return ('<div class="card"><h2>Запуск процесса &nbsp;' + status + "</h2>"
            '<form method="post" action="/launch" enctype="multipart/form-data" '
            'style="display:flex;gap:12px;flex-wrap:wrap;align-items:center">'
            + field + '<input type="file" name="file" id="file" accept=".csv" required>'
            '<button type="submit" class="go">Запустить</button></form></div>')


def _start(table: str, upload: UploadFile, background: BackgroundTasks) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", Path(upload.filename or "file.csv").name)
    INCOMING.mkdir(exist_ok=True)
    with psycopg.connect(db_conninfo()) as conn:
        pid, sids = create_process(conn, table, safe)
    dest = (INCOMING / f"{pid}__{safe}").resolve()
    with open(dest, "wb") as f:
        shutil.copyfileobj(upload.file, f)
    background.add_task(_run, pid, sids, table, str(dest))
    return pid


def _run(pid, sids, table, path):
    with psycopg.connect(db_conninfo()) as conn:
        run_pipeline(conn, pid, sids, table, path)


def _uuid_or_404(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise HTTPException(404, "Неверный идентификатор")


# -------------------------------------------------------------- веб-интерфейс
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index():
    tables = _service_tables()
    with psycopg.connect(db_conninfo()) as conn:
        runs, state = dash.fetch_runs(conn), dash.fetch_state(conn)
    return dash.render_index(runs, state, dash.demo_mode(),
                             extra_html=_launch_form(tables), refresh="smart")


@app.post("/launch", include_in_schema=False)
def launch(background: BackgroundTasks, table: str = Form(...), file: UploadFile = File(...)):
    pid = _start(table, file, background)
    return RedirectResponse(f"/run/{pid}", status_code=303)


@app.get("/run/{process_id}", response_class=HTMLResponse, include_in_schema=False)
def run_page(process_id: str):
    pid = _uuid_or_404(process_id)
    with psycopg.connect(db_conninfo()) as conn:
        data = dash.fetch_run(conn, pid)
    if not data:
        raise HTTPException(404, "Запуск не найден")
    return dash.render_run(data)


@app.post("/reset", include_in_schema=False)
def reset():
    if not dash.demo_mode():
        raise HTTPException(403, "Сброс доступен только при DEMO_MODE=true в .env")
    with psycopg.connect(db_conninfo()) as conn:
        dash.reset_all(conn)
    for f in INCOMING.glob("*"):
        try:
            f.unlink()
        except OSError:
            pass
    return RedirectResponse("/", status_code=303)


# ------------------------------------------------------------------------ API
@app.get("/health", summary="Проверка, что оркестратор работает")
def health():
    return {"status": "ok", "service": "orchestrator", "bb_load_url": BB_LOAD_URL,
            "bb_load_available": _service_tables() is not None}


@app.post("/api/processes", status_code=202, summary="Запустить процесс обработки файла")
def api_start(background: BackgroundTasks, table: str = Form(...), file: UploadFile = File(...)):
    pid = _start(table, file, background)
    return {"process_id": pid, "status_url": f"/api/processes/{pid}", "page": f"/run/{pid}"}


@app.get("/api/processes", summary="Список запусков")
def api_list():
    with psycopg.connect(db_conninfo()) as conn:
        runs = dash.fetch_runs(conn)
    return [{"process_id": str(r[0]), "status": r[1], "created_at": r[2].isoformat(),
             "table": r[3], "duration_ms": r[4], "issues": r[5]} for r in runs]


@app.get("/api/processes/{process_id}", summary="Цепочка шагов запуска по сквозному ID")
def api_process(process_id: str):
    pid = _uuid_or_404(process_id)
    with psycopg.connect(db_conninfo()) as conn:
        data = dash.fetch_run(conn, pid)
    if not data:
        raise HTTPException(404, "Запуск не найден")
    proc, steps, issues, _ = data
    return {
        "process_id": str(proc[0]), "status": proc[1], "table": proc[5], "file": proc[6],
        "created_at": proc[2].isoformat(),
        "steps": [{"order": s[0], "service": s[1], "step": s[2], "status": s[3], "duration_ms": s[4],
                   "rows_read": s[5], "rows_loaded": s[6], "rows_rejected": s[7], "message": s[8]}
                  for s in steps],
        "issues": [{"severity": i[0], "step": i[1], "column": i[2], "issue_type": i[3],
                    "rows_bad": i[4], "rows_total": i[5]} for i in issues],
    }
