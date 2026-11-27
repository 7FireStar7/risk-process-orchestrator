"""Простой дашборд: показывает запуски, шаги, проблемы и состояние БД.

Запуск:  python dashboard.py     затем открыть  http://localhost:8080
Страница сама обновляется каждые 5 секунд: запусти run_file.py в другом
терминале и смотри, как появляется новый запуск.

Кнопка «Сбросить данные» видна только если в .env есть DEMO_MODE=true.
"""
import html
import os
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import psycopg
from psycopg import sql

from bb_load.config import db_conninfo, load_env

PORT = 8080
STATUS_CLASS = {"OK": "ok", "WARNING": "warn", "ERROR": "err",
                "IN_PROGRESS": "run", "NEW": "idle", "PENDING": "idle"}

RESET_SQL = """
DO $$
DECLARE t text;
BEGIN
    FOR t IN SELECT tablename FROM pg_tables
             WHERE schemaname = 'public'
               AND (tablename LIKE 'raw\\_%' OR tablename LIKE 'stg\\_%')
    LOOP
        EXECUTE format('TRUNCATE TABLE %I', t);
    END LOOP;
    TRUNCATE TABLE process_run RESTART IDENTITY CASCADE;
END;
$$;
"""


# ---------------------------------------------------------------- данные
def fetch_runs(conn):
    return conn.execute(
        "SELECT p.process_id, p.status, p.created_at, p.params->>'table', "
        "       round(extract(epoch FROM (p.finished_at - p.started_at)) * 1000)::int, "
        "       (SELECT count(*) FROM validation_issue i WHERE i.process_id = p.process_id) "
        "FROM process_run p ORDER BY p.created_at DESC LIMIT 50").fetchall()


def fetch_state(conn):
    names = [r[0] for r in conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")]
    out = []
    for n in names:
        cnt = conn.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(n))).fetchone()[0]
        out.append((n, cnt))
    return out


def fetch_run(conn, pid):
    proc = conn.execute(
        "SELECT process_id, status, created_at, started_at, finished_at, params->>'table', params->>'file' "
        "FROM process_run WHERE process_id = %s", (pid,)).fetchone()
    if not proc:
        return None
    steps = conn.execute(
        "SELECT step_order, service, step_name, status, duration_ms, rows_read, rows_loaded, "
        "rows_rejected, message FROM v_step_timeline WHERE process_id = %s ORDER BY step_order",
        (pid,)).fetchall()
    issues = conn.execute(
        "SELECT i.severity, s.step_name, i.column_name, i.issue_type, i.rows_bad, i.rows_total "
        "FROM validation_issue i LEFT JOIN step_run s ON s.step_id = i.step_id "
        "WHERE i.process_id = %s ORDER BY i.severity, i.column_name", (pid,)).fetchall()
    preview = None
    if proc[5]:
        stg = "stg_" + proc[5]
        if conn.execute("SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename=%s",
                        (stg,)).fetchone():
            cur = conn.execute(
                sql.SQL("SELECT * FROM {} WHERE process_id = %s ORDER BY row_num LIMIT 10")
                .format(sql.Identifier(stg)), (pid,))
            preview = (stg, [d.name for d in cur.description], cur.fetchall())
    return proc, steps, issues, preview


def reset_all(conn):
    conn.execute(RESET_SQL)
    conn.commit()


# ------------------------------------------------------------- оформление
CSS = """
:root{--bg:#f6f7f9;--card:#fff;--text:#1d2330;--muted:#6b7280;--line:#e5e7eb;
--ok:#16a34a;--warn:#d97706;--err:#dc2626;--run:#2563eb;--idle:#9ca3af}
@media(prefers-color-scheme:dark){:root{--bg:#14171c;--card:#1d2128;--text:#e6e8ec;--muted:#9aa3b2;--line:#2c313a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.45 system-ui,Segoe UI,sans-serif}
header{padding:16px 24px;border-bottom:1px solid var(--line);background:var(--card);
display:flex;justify-content:space-between;align-items:center}
header h1{font-size:18px;margin:0}header a{color:inherit;text-decoration:none}
main{max-width:1100px;margin:0 auto;padding:20px 24px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:18px}
.card h2{font-size:15px;margin:0 0 12px;color:var(--muted);font-weight:600}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:13px}tr:last-child td{border-bottom:0}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;color:#fff;font-size:12px;font-weight:600}
.badge.ok{background:var(--ok)}.badge.warn{background:var(--warn)}.badge.err{background:var(--err)}
.badge.run{background:var(--run)}.badge.idle{background:var(--idle)}
.mono{font-family:ui-monospace,Consolas,monospace;font-size:13px}
.muted{color:var(--muted)}a.row{color:var(--run);text-decoration:none}
.bar{height:10px;border-radius:5px;background:var(--idle);min-width:3px}
.bar.ok{background:var(--ok)}.bar.warn{background:var(--warn)}.bar.err{background:var(--err)}.bar.run{background:var(--run)}
.grid{display:grid;grid-template-columns:2fr 1fr;gap:18px}@media(max-width:800px){.grid{grid-template-columns:1fr}}
.scroll{overflow-x:auto}button{background:var(--err);color:#fff;border:0;border-radius:8px;padding:8px 14px;cursor:pointer}
select,input{background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:7px 10px;font:inherit}
button.go{background:var(--run)}
.summary{display:flex;gap:28px;flex-wrap:wrap}.summary div span{display:block;color:var(--muted);font-size:12px}
"""


def esc(v):
    return html.escape("" if v is None else str(v))


def badge(status):
    return f'<span class="badge {STATUS_CLASS.get(status, "idle")}">{esc(status)}</span>'


def fmt_dt(dt):
    return dt.strftime("%d.%m.%Y %H:%M:%S") if dt else "—"


def fmt_ms(ms):
    if ms is None:
        return "—"
    return f"{ms} мс" if ms < 1000 else f"{ms / 1000:.2f} с"


def page(title, body, refresh=True):
    """refresh: True - обновлять страницу каждые 5 с, False - нет,
    "smart" - обновлять, пока пользователь не выбрал файл или не открыл список."""
    script = ""
    if refresh == "smart":
        meta = ""
        script = ("<script>setInterval(function(){var f=document.getElementById('file');"
                  "if(!(f&&f.value)&&!document.activeElement.matches('select,input'))"
                  "location.reload()},5000)</script>")
    else:
        meta = '<meta http-equiv="refresh" content="5">' if refresh else ""
    return (f'<!doctype html><html lang="ru"><head><meta charset="utf-8">{meta}'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{esc(title)}</title><style>{CSS}</style></head><body>"
            f'<header><h1><a href="/">Risk Process Orchestrator</a></h1>'
            f'<span class="muted">обновляется каждые 5 секунд</span></header>'
            f"<main>{body}</main>{script}</body></html>")


def render_index(runs, state, demo_mode, extra_html="", refresh=True):
    if runs:
        rows = "".join(
            f'<tr><td><a class="row mono" href="/run/{esc(r[0])}">{esc(str(r[0])[:8])}…</a></td>'
            f"<td>{esc(r[3] or '—')}</td><td>{badge(r[1])}</td><td>{fmt_dt(r[2])}</td>"
            f"<td>{fmt_ms(r[4])}</td><td>{r[5]}</td></tr>" for r in runs)
        runs_html = ("<table><tr><th>process_id</th><th>Таблица</th><th>Статус</th><th>Создан</th>"
                     f"<th>Длительность</th><th>Проблем</th></tr>{rows}</table>")
    else:
        runs_html = '<p class="muted">Запусков пока нет. Выполни python run_file.py …</p>'
    state_rows = "".join(f"<tr><td class='mono'>{esc(n)}</td><td>{c}</td></tr>" for n, c in state)
    reset = ""
    if demo_mode:
        reset = ('<form method="post" action="/reset" style="margin-top:12px" '
                 'onsubmit="return confirm(\'Удалить все запуски и данные в raw_/stg_?\')">'
                 "<button>Сбросить данные</button></form>")
    body = (extra_html + f'<div class="grid"><div class="card"><h2>Запуски процессов</h2>{runs_html}</div>'
            f'<div class="card"><h2>Состояние БД (строк в таблицах)</h2>'
            f"<table><tr><th>Таблица</th><th>Строк</th></tr>{state_rows}</table>{reset}</div></div>")
    return page("Запуски", body, refresh)


def render_run(data):
    proc, steps, issues, preview = data
    pid, status, created, started, finished, table, file = proc
    total = max([s[4] or 0 for s in steps] + [1])
    step_rows = "".join(
        f"<tr><td>{s[0]}</td><td>{esc(s[1])}</td><td><b>{esc(s[2])}</b></td><td>{badge(s[3])}</td>"
        f'<td style="width:22%"><div class="bar {STATUS_CLASS.get(s[3], "idle")}" '
        f'style="width:{max(3, int((s[4] or 0) * 100 / total))}%"></div></td>'
        f"<td>{fmt_ms(s[4])}</td><td>{'' if s[5] is None else s[5]}</td>"
        f"<td>{'' if s[6] is None else s[6]}</td><td>{'' if s[7] is None else s[7]}</td>"
        f"<td class='muted'>{esc(s[8])}</td></tr>" for s in steps)
    issue_rows = "".join(
        f"<tr><td>{badge(i[0])}</td><td>{esc(i[1])}</td><td class='mono'>{esc(i[2] or '—')}</td>"
        f"<td>{esc(i[3])}</td><td>{'' if i[4] is None else f'{i[4]} из {i[5]}'}</td></tr>"
        for i in issues)
    issues_html = (f"<table><tr><th>Серьёзность</th><th>Шаг</th><th>Столбец</th><th>Тип</th>"
                   f"<th>Строк</th></tr>{issue_rows}</table>" if issues
                   else '<p class="muted">Проблем не найдено.</p>')
    prev_html = ""
    if preview:
        name, cols, rows = preview
        head = "".join(f"<th>{esc(c)}</th>" for c in cols)
        body_rows = "".join("<tr>" + "".join(f"<td class='mono'>{esc(v)}</td>" for v in r) + "</tr>"
                            for r in rows)
        prev_html = (f'<div class="card"><h2>Чистые данные: {esc(name)} (первые 10 строк этого запуска)</h2>'
                     f'<div class="scroll"><table><tr>{head}</tr>{body_rows}</table></div></div>')
    body = (f'<p><a class="row" href="/">← все запуски</a></p>'
            f'<div class="card"><h2>Запуск</h2><div class="summary">'
            f'<div><span>process_id</span><b class="mono">{esc(pid)}</b></div>'
            f"<div><span>Итог</span>{badge(status)}</div><div><span>Таблица</span>{esc(table or '—')}</div>"
            f"<div><span>Файл</span>{esc(file or '—')}</div><div><span>Создан</span>{fmt_dt(created)}</div>"
            f"</div></div>"
            f'<div class="card"><h2>Шаги</h2><div class="scroll"><table><tr><th>№</th><th>Сервис</th>'
            f"<th>Шаг</th><th>Статус</th><th>Время</th><th></th><th>Прочитано</th><th>Загружено</th>"
            f"<th>Отклонено</th><th>Сообщение</th></tr>{step_rows}</table></div></div>"
            f'<div class="card"><h2>Найденные проблемы</h2>{issues_html}</div>{prev_html}')
    return page("Запуск " + str(pid)[:8], body, refresh=status in ("NEW", "IN_PROGRESS"))


# ------------------------------------------------------------------ сервер
def demo_mode():
    return os.environ.get("DEMO_MODE", "").strip().lower() == "true"


class Handler(BaseHTTPRequestHandler):
    def respond(self, code, text, location=None):
        data = text.encode("utf-8")
        self.send_response(code)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            with psycopg.connect(db_conninfo()) as conn:
                if path in ("", "/"):
                    return self.respond(200, render_index(fetch_runs(conn), fetch_state(conn), demo_mode()))
                if path.startswith("/run/"):
                    try:
                        pid = uuid.UUID(path[5:])
                    except ValueError:
                        return self.respond(404, page("Не найдено", "<p>Неверный идентификатор.</p>", False))
                    data = fetch_run(conn, str(pid))
                    if not data:
                        return self.respond(404, page("Не найдено", "<p>Запуск не найден.</p>", False))
                    return self.respond(200, render_run(data))
            self.respond(404, page("Не найдено", "<p>Страница не найдена.</p>", False))
        except Exception as e:  # показываем причину на странице, а не пустой экран
            self.respond(500, page("Ошибка", f"<div class='card'><h2>Ошибка</h2><p>{esc(e)}</p></div>", False))

    def do_POST(self):
        if urlparse(self.path).path != "/reset":
            return self.respond(404, page("Не найдено", "<p>Не найдено.</p>", False))
        if not demo_mode():
            return self.respond(403, page("Запрещено", "<p>Сброс доступен только при DEMO_MODE=true в .env.</p>", False))
        try:
            with psycopg.connect(db_conninfo()) as conn:
                reset_all(conn)
            self.respond(303, "", location="/")
        except Exception as e:
            self.respond(500, page("Ошибка", f"<div class='card'><h2>Ошибка</h2><p>{esc(e)}</p></div>", False))

    def log_message(self, *args):  # без шума в терминале
        pass


def main():
    load_env()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Дашборд: http://localhost:{PORT}   (остановить: Ctrl+C)")
    print("Кнопка сброса данных:", "включена" if demo_mode() else "выключена (нужен DEMO_MODE=true в .env)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")


if __name__ == "__main__":
    main()