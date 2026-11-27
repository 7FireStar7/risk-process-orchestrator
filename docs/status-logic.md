# Автоматическое обновление статусов

Документ описывает, как статусы шагов и процесса обновляются автоматически, и приводит SQL, который это делает. Определения находятся в `db/001_schema.sql`.

## Статусы

| Статус | Для шага | Для процесса |
|---|---|---|
| `PENDING` | ещё не начат | нет |
| `NEW` | нет | шаги созданы, ни один не начат |
| `IN_PROGRESS` | выполняется | есть шаги в работе или ожидании |
| `OK` | завершён без проблем | все шаги завершены без проблем |
| `WARNING` | завершён с предупреждениями | предупреждения, ошибок нет |
| `ERROR` | завершён с ошибкой | есть хотя бы одна ошибка |

## Алгоритм

1. Оркестратор создаёт запись в `process_run` и четыре записи в `step_run` со статусом `PENDING`. Триггер выставляет процессу статус `NEW`.
2. Перед вызовом сервиса оркестратор переводит шаг в `IN_PROGRESS` и записывает время начала.
3. Сервис возвращает результат шага: статус и список проблем. Статус шага определяется по худшей из найденных проблем (`ERROR` > `WARNING` > `OK`); шаг `stage` получает `WARNING`, если есть отклонённые строки.
4. Оркестратор записывает проблемы в `validation_issue` и обновляет шаг: статус, время окончания, число строк, сообщение.
5. **Любое изменение статуса шага запускает триггер**, который пересчитывает статус всего процесса и время его начала и окончания.
6. Если шаг вернул признак остановки (нет данных, нет обязательного столбца) или сервис недоступен, оставшиеся шаги остаются в `PENDING`, а процесс получает `ERROR`.

## Правило расчёта статуса процесса

Порядок проверок важен: побеждает худший статус.

| Условие (по шагам процесса) | Статус процесса |
|---|---|
| нет шагов | `NEW` |
| есть шаг `ERROR` | `ERROR` |
| все шаги `PENDING` | `NEW` |
| есть шаг `PENDING` или `IN_PROGRESS` | `IN_PROGRESS` |
| есть шаг `WARNING` | `WARNING` |
| иначе | `OK` |

## SQL

Пересчёт статуса процесса:

```sql
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
```

Триггер на изменение шага:

```sql
CREATE TRIGGER step_run_status_changed
AFTER INSERT OR UPDATE OF status ON step_run
FOR EACH ROW
EXECUTE FUNCTION trg_step_run_after();   -- вызывает recalc_process_status(NEW.process_id)
```

Статус шага по проблемам (используется при проверке файла из командной строки `run_file.py`; при работе через оркестратор статус шага считает сервис по тому же правилу):

```sql
CREATE FUNCTION step_status_from_issues(p_step_id bigint)
RETURNS text LANGUAGE sql STABLE AS $$
    SELECT CASE
             WHEN bool_or(severity = 'ERROR')   THEN 'ERROR'
             WHEN bool_or(severity = 'WARNING') THEN 'WARNING'
             ELSE 'OK'
           END
    FROM validation_issue WHERE step_id = p_step_id;
$$;
```

Представление для дашборда (шаги со временем):

```sql
SELECT * FROM v_step_timeline WHERE process_id = '...' ORDER BY step_order;
```

## Как проверить

Файл `db/002_demo.sql` создаёт процесс из трёх шагов и проводит его через статусы. Результат:

| Действие | Статус процесса |
|---|---|
| созданы шаги | `NEW` |
| `load` в работе | `IN_PROGRESS` |
| `load` завершён, остальные ждут | `IN_PROGRESS` |
| `validate` завершён с предупреждением | `IN_PROGRESS` |
| все шаги завершены | `WARNING` |
| `validate` переведён в `ERROR` | `ERROR` |

Последняя строка показывает правило «худший статус побеждает».
