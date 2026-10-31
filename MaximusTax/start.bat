@echo off
cd /d "%~dp0"
set "PY=%~dp0..\ТаксиMax\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=py"
start "" "http://127.0.0.1:8765/"
"%PY%" server.py
