@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

echo ============================================================
echo  ТаксиМакс — бот + Mini-App + туннель
echo  База одна: data\taxi_data.json
echo ============================================================
echo.

if not exist ".env" (
  echo [!] Файл .env не найден. Скопируйте .env.example -^> .env и впишите токен.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [*] Создаю виртуальное окружение...
  python -m venv .venv || goto :err
  .venv\Scripts\python.exe -m pip install --upgrade pip
  .venv\Scripts\pip.exe install -r requirements.txt || goto :err
)

if not exist "%~dp0logs" mkdir "%~dp0logs"

echo [*] Отключаю переход ПК в сон/гибернацию...
powercfg /change standby-timeout-ac 0 >nul 2>&1
powercfg /change hibernate-timeout-ac 0 >nul 2>&1
powercfg /change standby-timeout-dc 0 >nul 2>&1
powercfg /change hibernate-timeout-dc 0 >nul 2>&1

set "PY=%~dp0.venv\Scripts\python.exe"

call :running python.exe main.py
if errorlevel 1 (
  echo [*] Запускаю бота ^(long polling, автоперезапуск^)...
  start "TaxiMax Bot" /min "%~dp0run-bot.bat"
) else (
  echo [*] Бот уже запущен.
)

call :running python.exe api_server.py
if errorlevel 1 (
  echo [*] Запускаю API/Mini-App ^(порт 8080, автоперезапуск^)...
  start "TaxiMax API" /min "%~dp0run-api.bat"
) else (
  echo [*] API уже запущен.
)

echo [*] Жду, пока поднимется http://127.0.0.1:8080/ ...
set /a WAIT=0
:waitapi
netstat -ano | find ":8080" | find "LISTENING" >nul
if not errorlevel 1 goto :apiup
set /a WAIT+=1
if %WAIT% GEQ 20 (
  echo [!] API не открыл порт 8080. Смотрите logs\api.log
  goto :apiup
)
ping -n 2 127.0.0.1 >nul
goto :waitapi

:apiup
echo.
echo Локально:  http://127.0.0.1:8080/
echo Mini-App:  https://pearl-aspen-b471789c.tunnl.gg
echo В MAX: Чат-боты -^> бот -^> Настройки -^> ссылка Mini-App = адрес выше.
echo Закрыть это окно или Ctrl+C — остановить только туннель.
echo Бот и API продолжат работать.
echo ============================================================

set "SSH=%~dp0MaximusTax\tools\OpenSSH-Win64\ssh.exe"
set "KEY=%~dp0MaximusTax\tools\tunnel_key"
if not exist "%SSH%" (
  echo [!] Не найден %SSH%
  echo     Бот и API работают локально, туннель не запущен.
  pause
  exit /b 0
)
if not exist "%KEY%" (
  echo [!] Не найден ключ туннеля: %KEY%
  echo     Бот и API работают локально.
  pause
  exit /b 0
)

call :running ssh.exe proxy.tunnl.gg
if not errorlevel 1 (
  echo [*] Туннель уже запущен.
  pause
  exit /b 0
)

echo [%date% %time%] tunnel start >> "%~dp0logs\tunnel.log"
:again
echo [*] Туннель: https://pearl-aspen-b471789c.tunnl.gg  -^>  127.0.0.1:8080
"%SSH%" -i "%KEY%" -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ConnectTimeout=15 -tt -R 80:127.0.0.1:8080 stable@proxy.tunnl.gg
echo [%date% %time%] tunnel exited code=%ERRORLEVEL% >> "%~dp0logs\tunnel.log"
echo [!] Туннель оборвался, переподключаюсь через 5 сек...
ping -n 6 127.0.0.1 >nul
goto again

:running
set "PROC=%~1"
set "PART=%~2"
powershell -NoProfile -Command "if (Get-CimInstance Win32_Process | Where-Object { $_.Name -eq $env:PROC -and $_.CommandLine -like ('*' + $env:PART + '*') }) { exit 0 } else { exit 1 }"
exit /b %ERRORLEVEL%

:err
echo [!] Ошибка установки/запуска. Проверьте интернет и Python.
pause
exit /b 1
