"""Разметка панели: скрипт обязан быть синтаксически целым.

Запуск: python tests/test_web_html.py

Живой случай: в JS-строку попали настоящие переводы строки вместо \\n\\n.
Односимвольная строка в JavaScript многострочной быть не может — скрипт
переставал парситься целиком, и панель открывалась мёртвой: сервер отдаёт
данные, а на странице ни одного прогона. HTTP 200 при этом был, поэтому
проверка «curl вернул 200» такое не ловит. Ловит эта.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flowbatch.web import HTML


def script_body(html: str) -> str:
    m = re.search(r"<script>(.*)</script>", html, re.S)
    if not m:
        raise SystemExit("в разметке панели нет <script>")
    return m.group(1)


# После этих символов «/» начинает регулярное выражение, а не деление.
_REGEX_AFTER = set("(,=:[!&|?{};+-*%~^")


def unterminated_strings(js: str) -> list[tuple[int, str]]:
    """Литералы '...' и "...", не закрытые до конца физической строки.

    Именно эта поломка убила панель: в строку попал настоящий перевод
    строки вместо \\n. Обратные кавычки переносить можно — их пропускаем.

    Разбор посимвольный и понимает комментарии и regex-литералы: без них
    любой /[&<>"]/g читался бы как начало строки и давал ложную тревогу.
    """
    bad: list[tuple[int, str]] = []
    quote: str | None = None
    in_regex = in_line_comment = in_block_comment = False
    prev = ""        # предыдущий значимый символ
    line = 1
    i = 0

    def note() -> None:
        start = js.rfind("\n", 0, i) + 1
        bad.append((line, js[start:i][:90]))

    while i < len(js):
        ch = js[i]
        nxt = js[i + 1] if i + 1 < len(js) else ""

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
                line += 1
            i += 1
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
            if ch == "\n":
                line += 1
            i += 1
            continue
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
            elif ch == "\n":
                if quote in ("'", '"'):
                    note()
                    quote = None      # не сыпать одной и той же ошибкой
                line += 1
            i += 1
            continue
        if in_regex:
            if ch == "\\":
                i += 2
                continue
            if ch == "/" or ch == "\n":
                in_regex = False
                if ch == "\n":
                    line += 1
            i += 1
            continue

        if ch == "/" and nxt == "/":
            in_line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue
        if ch == "/" and (prev in _REGEX_AFTER or prev == ""):
            in_regex = True
            i += 1
            continue
        if ch in "'\"`":
            quote = ch
        if ch == "\n":
            line += 1
        if not ch.isspace():
            prev = ch
        i += 1
    return bad


def main() -> int:
    failures: list[str] = []
    js = script_body(HTML)

    for line, text in unterminated_strings(js):
        failures.append(f"строка {line} скрипта: незакрытый литерал: {text!r}")

    # Сам детектор тоже проверяем: он обязан молчать на здоровом коде и
    # срабатывать на той самой поломке, иначе от него нет пользы.
    healthy = "const re = /[&<>\"]/g;\nconst s = 'ok';\nconst t = `много\nстрок`;\n"
    if unterminated_strings(healthy):
        failures.append(f"детектор ложно сработал на здоровом коде: "
                        f"{unterminated_strings(healthy)}")
    broken = "alert('первая строка\n\nвторая');\n"
    if not unterminated_strings(broken):
        failures.append("детектор не поймал перевод строки внутри литерала")

    # Элементы, на которые скрипт вешает обработчики, должны быть в разметке.
    for eid in ("killall", "stopall", "queuefiles", "rescan"):
        if f'id="{eid}"' not in HTML:
            failures.append(f"в разметке нет элемента с id={eid!r}, а скрипт его ищет")
    # То же для шаблона прогона: кнопка и её обработчик.
    for name in ("kill", "pick", "file", "load", "start", "stop",
                 "addq", "aqpick", "aqfile", "aqsrc", "aqtext", "aqok", "aqcancel",
                 "queue"):
        if f'id="s${{n}}-{name}"' not in HTML:
            failures.append(f"в шаблоне прогона нет элемента {name!r}")
        if name != "queue" and f"g('{name}')" not in HTML:
            failures.append(f"элемент {name!r} есть, но обработчика на него нет")
    # Очередь очередей: перетаскивание и перестановка должны быть в скрипте.
    for marker in ("dragstart", "move_from", "qitem", "DRAG"):
        if marker not in HTML:
            failures.append(f"в скрипте нет {marker!r} — очередь-плеер сломана")

    if failures:
        print("FAIL")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK: скрипт панели цел, элементы и обработчики на месте")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
