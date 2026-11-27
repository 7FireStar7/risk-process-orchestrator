@echo off
REM Starts bb_load service (port 8001) and orchestrator (port 8000),
REM then opens the dashboard and both Swagger pages in the browser.
REM Run from the repository root. Needs .venv and .env.
cd /d "%~dp0"
start "bb_load :8001" cmd /k ".venv\Scripts\activate && uvicorn bb_load.api:app --port 8001 --no-use-colors"
start "orchestrator :8000" cmd /k ".venv\Scripts\activate && uvicorn orchestrator.app:app --port 8000 --no-use-colors"
echo Waiting for servers to start...
timeout /t 5 /nobreak >nul
start http://localhost:8000
start http://localhost:8000/docs
start http://localhost:8001/docs