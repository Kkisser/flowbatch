@echo off
chcp 65001 >nul
rem Запуск панели flowbatch двойным кликом.
rem Только для этой машины. Доступ с других устройств Tailscale — Панель-сеть.bat
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Не найдено окружение .venv — создай его: python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
  pause
  exit /b 1
)
echo Панель поднимается на http://127.0.0.1:8765 — окно не закрывай.
.venv\Scripts\python.exe -m flowbatch.cli ui
pause
