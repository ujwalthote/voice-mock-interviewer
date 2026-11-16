@echo off
cd /d "%~dp0"
start "" http://127.0.0.1:8800
.venv\Scripts\python -m uvicorn server:app --host 127.0.0.1 --port 8800
