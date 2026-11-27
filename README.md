# Risk Process Orchestrator

Прототип оркестратора бизнес-процессов для расчёта рыночных рисков. Он объединяет два изолированных сервиса загрузки данных (`bb_load` и `bi_load`) в единый управляемый процесс: запускает шаги, присваивает им сквозной `process_id` и показывает статус и время каждого шага на дашборде.

Расчёт рисков в проект не входит: оркестратор отвечает за загрузку, проверку и раскладку данных.

## Документация

- [Технологический стек](docs/STACK.md)
- [Загрузка и валидация данных](docs/validation.md)

## Запуск

1. Установить Python 3.1x и PostgreSQL.
2. В pgAdmin создать базу `orchestrator`.
3. Выполнить в ней по порядку: `db/001_schema.sql`, `db/003_rule_required_when.sql`.
4. Создать окружение и установить зависимости:
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
5. Скопировать `.env.example` в `.env` и вписать пароль от PostgreSQL.
   Для кнопки сброса данных добавить строку `DEMO_MODE=true`.
6. Положить CSV-файлы в папку `data/` (в Git они не попадают).
7. Проверить файл:
   python run_file.py cbonds_tradings_stocks data\ИМЯ.csv
8. Открыть дашборд:
   python dashboard.py   →   http://localhost:8080