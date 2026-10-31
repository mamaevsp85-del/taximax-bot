@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

echo ============================================================
echo  ТаксиМакс - единый запуск: бот + Mini-App + туннель
echo  БД одна для всех: data\taxi_data.json
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

echo [*] Отключаю переход ПК в сон/гибернацию (чтобы приложение не "засыпало")...
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /change standby-timeout-dc 0
powercfg /change hibernate-timeout-dc 0

echo [*] Запускаю бота (long polling)...
start "TaxiMax Bot" /min cmd /c ""%~dp0.venv\Scripts\python.exe" -u "%~dp0main.py""

echo [*] Запускаю API/Mini-App сервер (порт 8080)...
start "TaxiMax API" /min cmd /c ""%~dp0.venv\Scripts\python.exe" -u "%~dp0api_server.py""

timeout /t 3 >nul
echo.
echo Локально:   http://127.0.0.1:8080/
echo Постоянный адрес Mini-App: https://pearl-aspen-b471789c.tunnl.gg
echo В MAX: Чат-боты -^> бот -^> Настройки -^> ссылка Mini-App = адрес выше.
echo Выйти из туннеля = Ctrl+C (бот и API продолжат работать).
echo ============================================================

set "SSH=%~dp0MaximusTax\tools\OpenSSH-Win64\ssh.exe"
if not exist "%SSH%" (
  echo [!] Не найден %SSH% - туннель не запущен. Бот и API работают локально.
  pause
  exit /b 0
)

:again
"%SSH%" -i "%~dp0MaximusTax\tools\tunnel_key" -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -tt -R 80:127.0.0.1:8080 stable@proxy.tunnl.gg
echo [!] Туннель оборвался, переподключаюсь через 5 сек...
timeout /t 5
goto again

:err
echo [!] Ошибка установки/запуска. Проверьте интернет и Python.
pause
exit /b 1