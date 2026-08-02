@echo off
rem Панель, доступная с других устройств сети Tailscale (телефон, ноутбук).
rem Слушается РОВНО адрес Tailscale, не 0.0.0.0: в чужом Wi-Fi панель не видна.
rem Доступ закрыт токеном - ссылка с ним печатается ниже, её и открывай.
rem ВНИМАНИЕ: файл сохранён в кодировке cp866. Не пересохраняй его в UTF-8,
rem иначе кириллица превратится в мусор и батник перестанет работать.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo.
  echo Не найдено окружение .venv. Создай его командой:
  echo   python -m venv .venv
  echo   .venv\Scripts\pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)
echo Останавливаю прежнюю панель, если она была запущена...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*flowbatch.cli ui*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1
echo.
echo Ссылка с токеном появится ниже - её открывай на телефоне.
echo Это окно не закрывай - пока оно открыто, работает панель.
echo.
.venv\Scripts\python.exe -m flowbatch.cli ui --host tailscale
pause
