"""Сервис bb_load (FastAPI): выполняет шаги обработки файла по запросу оркестратора.

Запуск:  uvicorn bb_load.api:app --port 8001
Swagger: http://localhost:8001/docs

Правила сервиса:
  - каждый запрос обязан содержать заголовок X-Correlation-ID (сквозной process_id);
    сервис пишет его в журнал и возвращает в ответе;
  - шаги не хранят состояние и безопасны для повторного вызова: каждый шаг заново
    читает файл, а load и stage перед записью удаляют строки этого же process_id;
  - сервис сам не пишет в process_run / step_run / validation_issue: это делает оркестратор
    по ответу сервиса.
"""
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Literal, Optional

import psycopg
from fastapi import FastAPI, Header, HTTPException, Response
from psycopg import sql
from pydantic import BaseModel

from .config import db_conninfo, load_env
from .loader import copy_rows, ensure_raw_table, raw_name
from .reader import read_file
from .registry import load_registry, load_rules, sync_rules
from .stager import ensure_stg_table, stage_rows, stg_name
from .validator import build_checks, run_checks, structural_issues

load_env()
REGISTRY_PATH = os.environ.get("BB_LOAD_REGISTRY", "rules/bb_load.yaml")
ALLOWED_DIRS = [Path("incoming"), Path("data")]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [bb_load] %(message)s")
log = logging.getLogger("bb_load")

app = FastAPI(
    title="bb_load: загрузка данных брокерского бизнеса",
    description="Сервис выполняет шаги обработки файла (read, load, validate, stage). "
                "Вызывается оркестратором с заголовком X-Correlation-ID.",
    version="0.1.0",
)


class StepRequest(BaseModel):
    table: str
    file_path: str


class Issue(BaseModel):
    column: Optional[str] = None
    severity: str
    issue_type: str
    rows_bad: Optional[int] = None
    rows_total: Optional[int] = None


class StepResult(BaseModel):
    step: str
    status: str
    message: str
    rows_read: Optional[int] = None
    rows_loaded: Optional[int] = None
    rows_rejected: Optional[int] = None
    stop: bool = False                 # True: дальнейшие шаги выполнять нельзя
    issues: list[Issue] = []


# ------------------------------------------------------------ вспомогательное
def _correlation_id(value: Optional[str]) -> str:
    if not value:
        raise HTTPException(400, "Нужен заголовок X-Correlation-ID")
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise HTTPException(400, "X-Correlation-ID должен быть UUID")


def _check_path(file_path: str) -> Path:
    p = Path(file_path).resolve()
    if not any(p.is_relative_to(d.resolve()) for d in ALLOWED_DIRS):
        raise HTTPException(400, "Файл должен лежать в папке incoming или data")
    return p


def _worst(issues: list) -> str:
    severities = {i[1] for i in issues}
    return "ERROR" if "ERROR" in severities else "WARNING" if "WARNING" in severities else "OK"


def _issues(items: list) -> list:
    return [Issue(column=c, severity=s, issue_type=t, rows_bad=b, rows_total=n)
            for c, s, t, b, n in items]


def _rows_in_raw(conn, table: str, cid: str) -> int:
    return conn.execute(sql.SQL("SELECT count(*) FROM {} WHERE process_id = %s")
                        .format(sql.Identifier(raw_name(table))), (cid,)).fetchone()[0]


# ----------------------------------------------------------------------- шаги
def _step_read(conn, cid, table, entry, rules, path) -> StepResult:
    res = read_file(str(path), entry["file_format"])
    found = structural_issues(rules, res)
    blocking = any(i[2] == "NO_DATA" or (i[2] == "MISSING_COLUMN" and i[1] == "ERROR") for i in found)
    return StepResult(
        step="read", status=_worst(found), stop=blocking, issues=_issues(found),
        message=f"режим: {res.mode}, строк данных: {len(res.rows)}, пустых строк пропущено: {res.lines_blank}",
        rows_read=len(res.rows) + res.rows_bad_length, rows_rejected=res.rows_bad_length)


def _step_load(conn, cid, table, entry, rules, path) -> StepResult:
    res = read_file(str(path), entry["file_format"])
    ensure_raw_table(conn, table, res.header)
    conn.execute(sql.SQL("DELETE FROM {} WHERE process_id = %s")
                 .format(sql.Identifier(raw_name(table))), (cid,))
    n = copy_rows(conn, table, cid, res.header, res.rows)
    return StepResult(step="load", status="OK", message=f"загружено в {raw_name(table)}",
                      rows_read=n, rows_loaded=n)


def _step_validate(conn, cid, table, entry, rules, path) -> StepResult:
    res = read_file(str(path), entry["file_format"])
    if _rows_in_raw(conn, table, cid) == 0:
        raise HTTPException(409, "Нет данных этого процесса в raw: сначала выполните шаг load")
    found = run_checks(conn, cid, build_checks(table, rules, res.header))
    return StepResult(step="validate", status=_worst(found), issues=_issues(found),
                      message=f"найдено проблем: {len(found)}", rows_read=len(res.rows))


def _step_stage(conn, cid, table, entry, rules, path) -> StepResult:
    res = read_file(str(path), entry["file_format"])
    if _rows_in_raw(conn, table, cid) == 0:
        raise HTTPException(409, "Нет данных этого процесса в raw: сначала выполните шаг load")
    ensure_stg_table(conn, table, rules)
    conn.execute(sql.SQL("DELETE FROM {} WHERE process_id = %s")
                 .format(sql.Identifier(stg_name(table))), (cid,))
    inserted, rejected, total = stage_rows(conn, table, cid, res.header, rules)
    return StepResult(step="stage", status="WARNING" if rejected else "OK",
                      message=f"в {stg_name(table)}: {inserted}, отклонено: {rejected}",
                      rows_read=total, rows_loaded=inserted, rows_rejected=rejected)


STEP_FUNCTIONS = {"read": _step_read, "load": _step_load,
                  "validate": _step_validate, "stage": _step_stage}


# ------------------------------------------------------------------ эндпоинты
@app.get("/health", summary="Проверка, что сервис работает")
def health():
    return {"status": "ok", "service": "bb_load"}


@app.get("/v1/tables", summary="Таблицы, описанные в реестре правил")
def tables():
    registry = load_registry(REGISTRY_PATH)
    return [{"table": t, "source": e.get("source"), "columns": len(e["columns"])}
            for t, e in registry["tables"].items()]


@app.post("/v1/steps/{step}", response_model=StepResult, summary="Выполнить один шаг обработки файла")
def run_step(step: Literal["read", "load", "validate", "stage"], body: StepRequest, response: Response,
             x_correlation_id: Optional[str] = Header(default=None, alias="X-Correlation-ID")):
    cid = _correlation_id(x_correlation_id)
    response.headers["X-Correlation-ID"] = cid
    path = _check_path(body.file_path)
    registry = load_registry(REGISTRY_PATH)
    if body.table not in registry["tables"]:
        raise HTTPException(404, f"Таблицы {body.table} нет в реестре")
    log.info("[%s] шаг=%s таблица=%s файл=%s", cid, step, body.table, path.name)
    if not path.exists():
        return StepResult(step=step, status="ERROR", stop=True, message=f"файл не найден: {path.name}")
    started = time.perf_counter()
    try:
        with psycopg.connect(db_conninfo()) as conn:
            sync_rules(conn, registry, body.table)
            rules = load_rules(conn, body.table)
            result = STEP_FUNCTIONS[step](conn, cid, body.table, registry["tables"][body.table], rules, path)
            conn.commit()
    except HTTPException:
        raise
    except psycopg.errors.UndefinedTable:
        raise HTTPException(409, "Нет нужной таблицы: сначала выполните шаг load")
    except Exception as e:
        log.exception("[%s] шаг=%s завершился ошибкой", cid, step)
        raise HTTPException(500, f"{type(e).__name__}: {e}")
    log.info("[%s] шаг=%s статус=%s за %d мс", cid, step, result.status,
             (time.perf_counter() - started) * 1000)
    return result
