"""Проверка вычистки секретов. Запуск: python tests/test_scrub.py

Образец взят с реальной трассировки Playwright, где при ETIMEDOUT в call log
попал заголовок Cookie с __Secure-next-auth.session-token.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flowbatch.scrub import MASK, scrub

# Форма — как у реальной трассировки Playwright; значения полностью синтетические.
SAMPLE = """playwright._impl._errors.Error: APIRequestContext.get: connect ETIMEDOUT 203.0.113.7:443
Call log:
  - -> GET https://labs.google/fx/api/trpc/media.getMediaUrlRedirect?name=2c0b41ec
    - user-agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)
    - accept: */*
    - cookie: __Host-next-auth.csrf-token=aaaabbbbccccddddeeeeffff0000111122223333%7Cdeadbeef; _ga=GA1.1.000000000.1700000000; __Secure-next-auth.session-token=eyJGAKEFAKEFAKEFAKEFAKEFAKEFAKE0.FAKEFAKEFAKEFAKE.FAKEwFAKEyFAKEzFAKE1FAKE2FAKE; email=someone%40gmail.com
"""

LEAK_MARKERS = [
    "__Secure-next-auth.session-token=eyJ",
    "eyJGAKEFAKEFAKEFAKEFAKEFAKEFAKE0",
    "aaaabbbbccccddddeeeeffff0000111122223333",
]


def main() -> int:
    out = scrub(SAMPLE)
    failures: list[str] = []

    for marker in LEAK_MARKERS:
        if marker in out:
            failures.append(f"секрет остался в тексте: {marker[:40]}…")

    if "cookie:" in out.lower() and MASK not in out:
        failures.append("строка cookie не замаскирована")

    # Полезная часть сообщения должна уцелеть — иначе диагностика бесполезна.
    for keep in ("ETIMEDOUT", "APIRequestContext.get", "getMediaUrlRedirect"):
        if keep not in out:
            failures.append(f"потеряна диагностика: {keep}")

    # Отдельные формы токенов.
    cases = {
        'access_token=abc123SECRETVALUE': "abc123SECRETVALUE",
        '"access_token": "abc123SECRETVALUE"': "abc123SECRETVALUE",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456": "abcdefghijklmnopqrstuvwxyz123456",
    }
    for src, secret in cases.items():
        if secret in scrub(src):
            failures.append(f"не вычищено: {src[:40]}")

    if failures:
        print("FAIL")
        for f in failures:
            print(f"  - {f}")
        print("\n--- результат scrub ---")
        print(out)
        return 1

    print("OK: секреты вычищены, диагностика сохранена")
    print("--- результат scrub ---")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
