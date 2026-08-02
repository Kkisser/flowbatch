@echo off
rem Запуск и перезапуск панели flowbatch двойным кликом.
rem Только для этой машины. Доступ с телефона - "Панель-сеть.bat".
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
echo Панель: http://127.0.0.1:8765
echo Это окно не закрывай - пока оно открыто, работает панель.
echo Закрыть панель: Ctrl+C или просто закрыть это окно.
echo.
.venv\Scripts\python.exe -m flowbatch.cli ui
pause
