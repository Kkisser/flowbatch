@echo off
chcp 65001 >nul
rem Панель, доступная с других устройств сети Tailscale (телефон, ноутбук).
rem Слушается РОВНО адрес Tailscale, не 0.0.0.0: в чужом Wi-Fi панель не видна.
rem Доступ закрыт токеном — ссылка с ним печатается ниже, её и открывай на телефоне.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Не найдено окружение .venv — создай его: python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m flowbatch.cli ui --host tailscale
pause
