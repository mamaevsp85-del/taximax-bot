@echo off
chcp 65001 >nul
rem ============================================================
rem  Туннель Mini-App: https://pearl-aspen-b471789c.tunnl.gg -> 127.0.0.1:8080
rem  ВАЖНО: этот файл запускает ТОЛЬКО туннель (не бота и не API),
rem  иначе получается рекурсия через start-max.bat и дубли процессов.
rem  Сервер tunnl.gg периодически рвёт сессию — переподключаемся в цикле.
rem ============================================================
cd /d "%~dp0"
set "SSH=%~dp0MaximusTax\tools\OpenSSH-Win64\ssh.exe"
set "KEY=%~dp0MaximusTax\tools\tunnel_key"
set "LOG=%~dp0logs\tunnel.log"

if not exist "%~dp0logs" mkdir "%~dp0logs"

if not exist "%SSH%" (
  echo [!] Не найден ssh.exe: %SSH%
  echo     Бот и Mini-App работают локально, туннель не запущен.
  exit /b 1
)
if not exist "%KEY%" (
  echo [!] Не найден ключ туннеля: %KEY%
  exit /b 1
)

:again
echo [%date% %time%] tunnel connect >> "%LOG%"
"%SSH%" -i "%KEY%" -o IdentitiesOnly=yes -o BatchMode=yes ^
  -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=20 ^
  -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes ^
  -o ConnectTimeout=20 -tt -R 80:127.0.0.1:8080 stable@proxy.tunnl.gg >> "%LOG%" 2>&1
echo [%date% %time%] tunnel exited code=%ERRORLEVEL% >> "%LOG%"
timeout /t 8 /nobreak >nul
goto again