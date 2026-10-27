@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

if not exist ".env" (
  echo [!] Файл .env не найден. Скопируйте .env.example в .env и впишите токен.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [*] Создаю виртуальное окружение...
  python -m venv .venv || goto :error
  .venv\Scripts\python.exe -m pip install --upgrade pip
  .venv\Scripts\pip.exe install -r requirements.txt || goto :error
)

echo [*] Запуск ТаксиМакс...
".venv\Scripts\python.exe" main.py
pause
exit /b 0

:error
echo [!] Ошибка установки/запуска. Проверьте интернет и Python.
pause
exit /b 1
