"""Telegram-уведомления через Bot API.

Токен и chat_id берутся только из окружения (.env). Если их нет — уведомления
молча отключаются, и это не считается ошибкой: скрипт продолжает работу.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

API_BASE = "https://api.telegram.org"


class Notifier:
    """Отправка сообщений и скриншотов в Telegram. No-op без настроек."""

    def __init__(self, token: str | None = None, chat_id: str | None = None) -> None:
        load_dotenv()
        self.token = (token or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
        self.chat_id = (chat_id or os.getenv("TELEGRAM_CHAT_ID") or "").strip()
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str) -> bool:
        """Отправить текстовое сообщение. Возвращает True при успехе."""
        if not self.enabled:
            return False
        try:
            r = httpx.post(
                f"{API_BASE}/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"},
                timeout=20,
            )
            if r.status_code != 200:
                self.last_error = f"HTTP {r.status_code}: {r.text[:200]}"
                return False
            return True
        except Exception as exc:  # noqa: BLE001 — уведомления не должны валить прогон
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False

    def send_photo(self, photo_path: str | Path, caption: str = "") -> bool:
        """Отправить скриншот с подписью. Возвращает True при успехе."""
        if not self.enabled:
            return False
        p = Path(photo_path)
        if not p.exists():
            self.last_error = f"Файл не найден: {p}"
            return False
        try:
            with p.open("rb") as fh:
                r = httpx.post(
                    f"{API_BASE}/bot{self.token}/sendPhoto",
                    data={"chat_id": self.chat_id, "caption": caption[:1024]},
                    files={"photo": (p.name, fh, "image/png")},
                    timeout=60,
                )
            if r.status_code != 200:
                self.last_error = f"HTTP {r.status_code}: {r.text[:200]}"
                return False
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False

    def check(self) -> tuple[bool, str]:
        """Проверка для doctor: жив ли бот. Токен наружу не отдаём."""
        if not self.enabled:
            missing = []
            if not self.token:
                missing.append("TELEGRAM_BOT_TOKEN")
            if not self.chat_id:
                missing.append("TELEGRAM_CHAT_ID")
            return False, "не настроен (нет " + ", ".join(missing) + " в .env)"
        try:
            r = httpx.get(f"{API_BASE}/bot{self.token}/getMe", timeout=20)
            if r.status_code != 200:
                return False, f"getMe вернул HTTP {r.status_code}"
            username = r.json().get("result", {}).get("username", "?")
            ok = self.send("flowbatch doctor: связь есть ✅")
            if not ok:
                return False, f"бот @{username} жив, но sendMessage не прошёл: {self.last_error}"
            return True, f"бот @{username}, тестовое сообщение доставлено"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
