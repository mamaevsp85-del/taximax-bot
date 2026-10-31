@echo off
cd /d "%~dp0"
set "PY=%~dp0..\ТаксиMax\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=py"
set "SSH=%~dp0tools\OpenSSH-Win64\ssh.exe"
echo Сервер: http://127.0.0.1:8765/
echo Постоянный адрес: https://pearl-aspen-b471789c.tunnl.gg
echo Его один раз вставляют в MAX: Чат-боты, бот, Настройки, кнопка Открыть.
echo Если туннель оборвался, это окно подключит тот же адрес снова.
start "Maximus server" "%PY%" -u server.py
:again
"%SSH%" -i "%~dp0tools\tunnel_key" -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -tt -R 80:127.0.0.1:8765 stable@proxy.tunnl.gg
timeout /t 5
goto again
