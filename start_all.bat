@echo off
REM Запускает сервис bb_load (порт 8001), оркестратор (порт 8000) и открывает дашборд.
REM Запускать из корня репозитория. Нужны .venv и .env.
cd /d "%~dp0"
start "bb_load :8001" cmd /k ".venv\Scripts\activate && uvicorn bb_load.api:app --port 8001"
start "orchestrator :8000" cmd /k ".venv\Scripts\activate && uvicorn orchestrator.app:app --port 8000"
timeout /t 4 /nobreak >nul
start http://localhost:8000
