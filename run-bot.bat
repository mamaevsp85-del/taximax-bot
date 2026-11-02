@echo off
chcp 65001 >nul
rem Автоперезапуск бота: если процесс упал — поднимаем снова через 10 секунд.
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%~dp0logs" mkdir "%~dp0logs"
:loop
"%PY%" -u "%~dp0main.py" >> "%~dp0logs\bot.out.log" 2>&1
echo %date% %time% bot exited, restart in 10s >> "%~dp0logs\bot.out.log"
timeout /t 10 /nobreak >nul
goto loop
