-- =====================================================================
-- 004_reset_demo.sql : сброс данных перед демонстрацией
-- Запускать в базе orchestrator (pgAdmin -> Query Tool).
-- Реестр правил (validation_rule) не трогается: он заново загружается из YAML
-- при каждом запуске run_file.py.
-- =====================================================================

-- ВАРИАНТ 1 (по умолчанию): очистить данные, структура остаётся.
-- Удаляет все запуски, шаги, проблемы и строки в raw_* и stg_*.
DO $$
DECLARE
    t text;
BEGIN
    FOR t IN
        SELECT tablename FROM pg_tables
        WHERE schemaname = 'public'
          AND (tablename LIKE 'raw\_%' OR tablename LIKE 'stg\_%')
    LOOP
        EXECUTE format('TRUNCATE TABLE %I', t);
    END LOOP;
    TRUNCATE TABLE process_run RESTART IDENTITY CASCADE;  -- вместе с step_run и validation_issue
END;
$$;

-- ВАРИАНТ 2: полностью убрать таблицы raw_* и stg_*, чтобы на демо было видно,
-- как они появляются в базе при запуске. Чтобы использовать, закомментируй
-- блок выше и раскомментируй блок ниже.
--
-- DO $$
-- DECLARE
--     t text;
-- BEGIN
--     FOR t IN
--         SELECT tablename FROM pg_tables
--         WHERE schemaname = 'public'
--           AND (tablename LIKE 'raw\_%' OR tablename LIKE 'stg\_%')
--     LOOP
--         EXECUTE format('DROP TABLE %I', t);
--     END LOOP;
--     TRUNCATE TABLE process_run RESTART IDENTITY CASCADE;
-- END;
-- $$;
