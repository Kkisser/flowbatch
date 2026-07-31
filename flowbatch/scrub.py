"""Вычистка секретов из любого текста, который может попасть в лог или наружу.

Мотивация не теоретическая. Playwright при сетевой ошибке печатает call log
целиком, включая заголовок Cookie — а там живёт __Secure-next-auth.session-token.
Одна такая трассировка, улетевшая в runs.jsonl или в Telegram, отдаёт сессию
Google целиком. Поэтому чистим ВСЁ на границе: сообщения ошибок, детали, лог.
"""

from __future__ import annotations

import re

MASK = "<вырезано>"

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Строка заголовка Cookie в call log Playwright (обычно '    - cookie: ...').
    (re.compile(r"(?im)^(\s*-?\s*(?:set-)?cookie:).*$"), r"\1 " + MASK),
    # Заголовок Authorization — съедаем строку целиком: схема (Bearer/Basic)
    # диагностической ценности не несёт, а частичная замена оставляет токен.
    (re.compile(r"(?im)^(\s*-?\s*authorization:).*$"), r"\1 " + MASK),
    (re.compile(r"(?i)\b(authorization\s*[=:]\s*)\S.*"), r"\1" + MASK),
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._\-]{16,}"), r"\1" + MASK),
    # Именованные токены: key=value, "key": "value", key: value.
    # Кавычка перед разделителем обязана быть необязательной — иначе
    # JSON-форма "access_token": "..." проскакивает мимо.
    (
        re.compile(
            r"(?i)\b(session-token|session_token|access_token|refresh_token|id_token"
            r"|csrf-token|csrf_token|api[_-]?key)(\"?\s*[=:]\s*\"?)[^\s;,&\"']+"
        ),
        r"\1\2" + MASK,
    ),
    # JWT-подобные строки (три base64url-сегмента через точку).
    (re.compile(r"\b[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}\b"), MASK),
]


def scrub(text: str | None) -> str:
    """Вернуть текст без секретов. None и пустая строка проходят как есть."""
    if not text:
        return text or ""
    out = str(text)
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    return out
