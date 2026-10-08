@echo off
rem Starts the job tracker and opens the dashboard. A server that's already running is restarted,
rem so code updates always take effect (it used to just open the browser on the old server).
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }"

start "" cmd /c "timeout /t 4 >nul & start http://localhost:8765"
uv run uvicorn app.main:app --host 127.0.0.1 --port 8765
