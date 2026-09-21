@echo off
rem Serves the static frontend on port 8002 (Windows equivalent of run_frontend.sh).
cd /d "%~dp0frontend"
echo Serving frontend on http://localhost:8002
python -m http.server 8002
