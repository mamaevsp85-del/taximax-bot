@echo off
chcp 65001 >nul
rem Автоперезапуск API/Mini-App: если процесс упал — поднимаем снова через 10 секунд.
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%~dp0logs" mkdir "%~dp0logs"
:loop
"%PY%" -u "%~dp0api_server.py" >> "%~dp0logs\api.out.log" 2>&1
echo %date% %time% api exited, restart in 10s >> "%~dp0logs\api.out.log"
timeout /t 10 /nobreak >nul
goto loop
