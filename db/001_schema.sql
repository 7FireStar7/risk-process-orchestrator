-- =====================================================================
-- 001_schema.sql : схема БД оркестратора (PostgreSQL)
-- Таблицы: process_run, step_run, validation_issue, validation_rule
-- Автоматический пересчёт статуса процесса: триггер на step_run
-- Запускать в базе orchestrator (pgAdmin -> Query Tool).
-- Скрипт можно запускать повторно: сначала всё пересоздаётся.
-- =====================================================================

DROP VIEW     IF EXISTS v_step_timeline;
DROP TABLE    IF EXISTS validation_issue CASCADE;
DROP TABLE    IF EXISTS validation_rule  CASCADE;
DROP TABLE    IF EXISTS step_run         CASCADE;
DROP TABLE    IF EXISTS process_run      CASCADE;
DROP FUNCTION IF EXISTS recalc_process_status(uuid);
DROP FUNCTION IF EXISTS trg_step_run_after();
DROP FUNCTION IF EXISTS step_status_from_issues(bigint);

-- ---------------------------------------------------------------------
-- 1. Запуск процесса. process_id = сквозной идентификатор (correlation id)
-- ---------------------------------------------------------------------
CREATE TABLE process_run (
    process_id     uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    process_type   text        NOT NULL,
    trigger_source text        NOT NULL DEFAULT 'MANUAL'
                               CHECK (trigger_source IN ('MANUAL', 'SCHEDULE')),
    status         text        NOT NULL DEFAULT 'NEW'
                               CHECK (status IN ('NEW', 'IN_PROGRESS', 'OK', 'WARNING', 'ERROR')),
    params         jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at     timestamptz NOT NULL DEFAULT now(),
    started_at     timestamptz,
    finished_at    timestamptz
);

-- ---------------------------------------------------------------------
-- 2. Шаги процесса: какой сервис, какой шаг, статус, время, число строк
-- ---------------------------------------------------------------------
CREATE TABLE step_run (
    step_id        bigserial   PRIMARY KEY,
    process_id     uuid        NOT NULL REFERENCES process_run (process_id) ON DELETE CASCADE,
    service        text        NOT NULL,              -- bb_load / bi_load
    step_name      text        NOT NULL,              -- load / validate / stage ...
    step_order     int         NOT NULL,
    status         text        NOT NULL DEFAULT 'PENDING'
                               CHECK (status IN ('PENDING', 'IN_PROGRESS', 'OK', 'WARNING', 'ERROR')),
    started_at     timestamptz,
    finished_at    timestamptz,
    message        text,
    rows_read      int,
    rows_loaded    int,
    rows_rejected  int,
    UNIQUE (process_id, service, step_name)
);
CREATE INDEX ix_step_run_process ON step_run (process_id);

-- ---------------------------------------------------------------------
-- 3. Найденные проблемы данных
-- ---------------------------------------------------------------------
CREATE TABLE validation_issue (
    issue_id       bigserial   PRIMARY KEY,
    process_id     uuid        NOT NULL REFERENCES process_run (process_id) ON DELETE CASCADE,
    step_id        bigint      REFERENCES step_run (step_id) ON DELETE CASCADE,
    table_name     text        NOT NULL,
    column_name    text,
    severity       text        NOT NULL CHECK (severity IN ('ERROR', 'WARNING')),
    issue_type     text        NOT NULL,              -- EMPTY / NOT_NUMBER / DUPLICATE / ...
    rows_bad       int,
    rows_total     int,
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_issue_process ON validation_issue (process_id);
CREATE INDEX ix_issue_step    ON validation_issue (step_id);

-- ---------------------------------------------------------------------
-- 4. Реестр правил (загружается из rules/bb_load.yaml)
-- ---------------------------------------------------------------------
CREATE TABLE validation_rule (
    table_name     text        NOT NULL,
    column_name    text        NOT NULL,
    severity       text        NOT NULL CHECK (severity IN ('ERROR', 'WARNING')),
    check_mode     text        NOT NULL DEFAULT 'VALUE'
                               CHECK (check_mode IN ('VALUE', 'PRESENCE_ONLY')),
    data_type      text,                              -- integer / number / date / text
    date_format    text,
    is_unique      boolean     NOT NULL DEFAULT false,
    note           text,
    PRIMARY KEY (table_name, column_name)
);

-- ---------------------------------------------------------------------
-- 5. Статус шага по найденным проблемам (ERROR > WARNING > OK)
-- ---------------------------------------------------------------------
CREATE FUNCTION step_status_from_issues(p_step_id bigint)
RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT CASE
             WHEN bool_or(severity = 'ERROR')   THEN 'ERROR'
             WHEN bool_or(severity = 'WARNING') THEN 'WARNING'
             ELSE 'OK'
           END
    FROM validation_issue
    WHERE step_id = p_step_id;
$$;

-- ---------------------------------------------------------------------
-- 6. Пересчёт статуса процесса по худшему из статусов шагов
--    нет шагов или все PENDING -> NEW
--    есть ERROR               -> ERROR
--    есть незавершённые       -> IN_PROGRESS
--    есть WARNING             -> WARNING
--    иначе                    -> OK
-- ---------------------------------------------------------------------
CREATE FUNCTION recalc_process_status(p_process_id uuid)
RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
    new_status text;
BEGIN
    SELECT CASE
             WHEN count(*) = 0                                   THEN 'NEW'
             WHEN bool_or(status = 'ERROR')                      THEN 'ERROR'
             WHEN bool_and(status = 'PENDING')                   THEN 'NEW'
             WHEN bool_or(status IN ('PENDING', 'IN_PROGRESS'))  THEN 'IN_PROGRESS'
             WHEN bool_or(status = 'WARNING')                    THEN 'WARNING'
             ELSE 'OK'
           END
    INTO new_status
    FROM step_run
    WHERE process_id = p_process_id;

    UPDATE process_run
    SET status      = new_status,
        started_at  = CASE WHEN new_status <> 'NEW'
                           THEN COALESCE(started_at, clock_timestamp())
                           ELSE started_at END,
        finished_at = CASE WHEN new_status IN ('OK', 'WARNING', 'ERROR')
                           THEN clock_timestamp()
                           ELSE NULL END
    WHERE process_id = p_process_id;
END;
$$;

CREATE FUNCTION trg_step_run_after()
RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM recalc_process_status(NEW.process_id);
    RETURN NULL;
END;
$$;

CREATE TRIGGER step_run_status_changed
AFTER INSERT OR UPDATE OF status ON step_run
FOR EACH ROW
EXECUTE FUNCTION trg_step_run_after();

-- ---------------------------------------------------------------------
-- 7. Представление для дашборда: шаги со временем выполнения
-- ---------------------------------------------------------------------
CREATE VIEW v_step_timeline AS
SELECT s.process_id,
       s.step_order,
       s.service,
       s.step_name,
       s.status,
       s.started_at,
       s.finished_at,
       round(extract(epoch FROM (s.finished_at - s.started_at)) * 1000)::int AS duration_ms,
       s.rows_read,
       s.rows_loaded,
       s.rows_rejected,
       s.message
FROM step_run s;
