-- =====================================================================
-- 002_demo.sql : демонстрация автоматического пересчёта статуса процесса
-- Запускать после 001_schema.sql. В конце выводится журнал изменений статуса.
-- Демо-данные помечены process_type = 'DEMO' и удаляются в конце файла.
-- =====================================================================

DROP TABLE IF EXISTS demo_log;
CREATE TEMP TABLE demo_log (
    n              serial PRIMARY KEY,
    action         text,
    process_status text
);

DO $$
DECLARE
    pid uuid;
BEGIN
    -- 1. Создаём процесс с тремя шагами сервиса bb_load
    INSERT INTO process_run (process_type) VALUES ('DEMO') RETURNING process_id INTO pid;
    INSERT INTO step_run (process_id, service, step_name, step_order) VALUES
        (pid, 'bb_load', 'load',     1),
        (pid, 'bb_load', 'validate', 2),
        (pid, 'bb_load', 'stage',    3);
    INSERT INTO demo_log (action, process_status)
        SELECT 'созданы 3 шага (все PENDING)', status FROM process_run WHERE process_id = pid;

    -- 2. Шаг load стартовал
    UPDATE step_run SET status = 'IN_PROGRESS', started_at = clock_timestamp()
    WHERE process_id = pid AND step_name = 'load';
    INSERT INTO demo_log (action, process_status)
        SELECT 'load: IN_PROGRESS', status FROM process_run WHERE process_id = pid;

    PERFORM pg_sleep(0.3);

    -- 3. Шаг load завершился успешно
    UPDATE step_run SET status = 'OK', finished_at = clock_timestamp(),
                        rows_read = 17, rows_loaded = 17
    WHERE process_id = pid AND step_name = 'load';
    INSERT INTO demo_log (action, process_status)
        SELECT 'load: OK (validate и stage ещё ждут)', status FROM process_run WHERE process_id = pid;

    -- 4. Шаг validate нашёл предупреждение
    UPDATE step_run SET status = 'IN_PROGRESS', started_at = clock_timestamp()
    WHERE process_id = pid AND step_name = 'validate';
    PERFORM pg_sleep(0.2);
    INSERT INTO validation_issue
        (process_id, step_id, table_name, column_name, severity, issue_type, rows_bad, rows_total)
    SELECT pid, step_id, 'cbonds_tradings_stocks', 'change_365_day', 'WARNING', 'EMPTY', 5, 17
    FROM step_run WHERE process_id = pid AND step_name = 'validate';
    UPDATE step_run
    SET status = step_status_from_issues(step_id), finished_at = clock_timestamp()
    WHERE process_id = pid AND step_name = 'validate';
    INSERT INTO demo_log (action, process_status)
        SELECT 'validate: WARNING (пустой change_365_day)', status FROM process_run WHERE process_id = pid;

    -- 5. Шаг stage завершился успешно -> процесс закончился с WARNING
    UPDATE step_run SET status = 'IN_PROGRESS', started_at = clock_timestamp()
    WHERE process_id = pid AND step_name = 'stage';
    PERFORM pg_sleep(0.2);
    UPDATE step_run SET status = 'OK', finished_at = clock_timestamp(), rows_loaded = 17
    WHERE process_id = pid AND step_name = 'stage';
    INSERT INTO demo_log (action, process_status)
        SELECT 'stage: OK (все шаги завершены)', status FROM process_run WHERE process_id = pid;

    -- 6. Худший статус побеждает: validate -> ERROR
    UPDATE step_run SET status = 'ERROR', message = 'демо: ошибка в данных'
    WHERE process_id = pid AND step_name = 'validate';
    INSERT INTO demo_log (action, process_status)
        SELECT 'validate переведён в ERROR', status FROM process_run WHERE process_id = pid;
END;
$$;

-- Результат: как менялся статус процесса
SELECT * FROM demo_log ORDER BY n;

-- ---------------------------------------------------------------------
-- Дополнительно (выполнять по одному, выделив запрос и нажав F5):
--
--   SELECT process_id, status, started_at, finished_at FROM process_run;
--   SELECT * FROM v_step_timeline ORDER BY step_order;
--   SELECT * FROM validation_issue;
--
-- Очистка демо-данных:
--
--   DELETE FROM process_run WHERE process_type = 'DEMO';
-- ---------------------------------------------------------------------
