@echo off
chcp 65001 >nul
rem ============================================================
rem  Туннель Mini-App: https://pearl-aspen-b471789c.tunnl.gg
rem  Пробрасывает внешний порт 80 на локальный api_server (8080).
rem  Переподключается автоматически. Требует окно консоли (pty).
rem ============================================================
cd /d "%~dp0"
set "SSH=%~dp0MaximusTax\tools\OpenSSH-Win64\ssh.exe"
set "KEY=%~dp0MaximusTax\tools\tunnel_key"

if not exist "%SSH%" (
  echo [!] Не найден %SSH%
  pause
  exit /b 1
)

echo Адрес: https://pearl-aspen-b471789c.tunnl.gg  ->  127.0.0.1:8080
echo Закройте окно, чтобы остановить туннель.
echo.

:again
"%SSH%" -i "%KEY%" -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ConnectTimeout=15 -tt -R 80:127.0.0.1:8080 stable@proxy.tunnl.gg >> "%~dp0logs\tunnel.log" 2>&1
echo [%date% %time%] tunnel exited code=%ERRORLEVEL% >> "%~dp0logs\tunnel.log"
timeout /t 5 >nul
goto again