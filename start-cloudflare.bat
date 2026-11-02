@echo off
chcp 65001 >nul
rem ============================================================
rem  Публичный туннель к Mini-App (порт 8080) через Cloudflare.
rem  Работает без своего домена: выдаёт адрес вида
rem  https://<случайные-слова>.trycloudflare.com
rem  ВАЖНО: адрес меняется при каждом запуске — новый нужно вписать
rem  в MAX: Чат-боты -> бот -> Настройки -> ссылка Mini-App.
rem  Сам адрес печатается в окне и пишется в logs\tunnel-url.txt
rem ============================================================
cd /d "%~dp0"
set "CF=%~dp0MaximusTax\tools\cloudflared.exe"
if not exist "%~CF%" set "CF=cloudflared"

if not exist "%~dp0logs" mkdir "%~dp0logs"

:again
echo [%date% %time%] cloudflared start >> "%~dp0logs\tunnel.log"
"%CF%" tunnel --url http://127.0.0.1:8080 --no-autoupdate >> "%~dp0logs\tunnel.log" 2>&1
echo [%date% %time%] cloudflared exited code=%ERRORLEVEL% >> "%~dp0logs\tunnel.log"
timeout /t 10 /nobreak >nul
goto again