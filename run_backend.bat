@echo off
rem Starts the backend on port 8001 (Windows equivalent of run_backend.sh).
rem Works from cmd.exe or PowerShell. Does not touch the Lightning scripts.
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\activate.bat" call ".venv\Scripts\activate.bat"

rem Load KEY=VALUE lines from .env, like `source .env` in the .sh script.
rem Lines starting with # are skipped. Do not put comments after a value.
if exist ".env" (
  for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do set "%%A=%%B"
)

cd backend
echo Starting backend on http://localhost:8001 (docs at /docs)
python -m uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload
