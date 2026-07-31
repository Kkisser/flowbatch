"""Проверка разбора текстовой очереди с @-директивами.

Запуск: python tests/test_promptfile.py — без сети и без браузера.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flowbatch.promptfile import parse

GOOD = """
# очередь эпизода

=== IMG K1
@ref C:\\refs\\geroi.png
@ref C:\\refs\\kost.png
Vertical 9:16 frame.
Second line of prompt.

Third paragraph after blank line.

=== КАРТИНКА K2
@ref C:\\refs\\geroi.png
Another prompt.

=== VID K1_anim
@use K1
@duration 8
Animate this image.

=== ВИДЕО K2_anim
@use K2
@out K2_final
Animate that image.
"""

BAD = """
Текст до первого заголовка.

=== IMG K1
@ref
@typo что-то
Prompt here.

=== IMG K1
Duplicate id.

=== VID V1
@use LATER
@duration 7
Video prompt.

=== IMG LATER
Prompt of later image.

=== VID V2
@use V1
Video referencing video.

=== IMG EMPTY
@ref C:\\x.png
"""


def main() -> int:
    failures: list[str] = []

    jobs, errors = parse(GOOD, out_dir="out")
    if errors:
        failures.append(f"GOOD дал ошибки: {errors}")
    if [j.id for j in jobs] != ["K1", "K2", "K1_anim", "K2_anim"]:
        failures.append(f"GOOD: неверные id: {[j.id for j in jobs]}")
    k1 = jobs[0]
    if k1.kind != "image" or len(k1.refs) != 2:
        failures.append(f"GOOD: K1 разобран неверно: {k1}")
    if "Second line of prompt." not in k1.prompt or "@ref" in k1.prompt:
        failures.append("GOOD: директивы попали в промпт или текст потерялся")
    if k1.prompt.count("\n") < 3:
        failures.append("GOOD: переводы строк промпта не сохранились")
    anim = jobs[2]
    if anim.kind != "video" or anim.duration != 8:
        failures.append(f"GOOD: K1_anim разобран неверно: {anim}")
    if not anim.refs or not anim.refs[0].replace("\\", "/").endswith("out/K1"):
        failures.append(f"GOOD: @use K1 дал не тот путь: {anim.refs}")
    k2a = jobs[3]
    if k2a.output_name != "K2_final":
        failures.append(f"GOOD: @out не применился: {k2a.output_name}")

    jobs, errors = parse(BAD, out_dir="out")
    expected_bits = [
        "текст вне блока",
        "@ref без пути",
        "неизвестная директива '@typo'",
        "повтор id 'K1'",
        "генерится позже",
        "@duration 7",
        "указывает на видео",
        "без текста промпта",
    ]
    for bit in expected_bits:
        if not any(bit in e for e in errors):
            failures.append(f"BAD: не поймана ошибка со словами {bit!r}; всего: {errors}")

    # @use на файл прошлого прогона: кладём файл на диск и убеждаемся, что найден.
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "OLD.jpg").write_bytes(b"x")
        jobs, errors = parse("=== VID V\n@use OLD\nAnimate.", out_dir=td)
        if errors:
            failures.append(f"@use по файлу с диска дал ошибки: {errors}")
        elif not jobs[0].refs or not jobs[0].refs[0].endswith("OLD.jpg"):
            failures.append(f"@use по файлу с диска дал не тот путь: {jobs[0].refs}")

    if failures:
        print("FAIL")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK: формат промптов разбирается верно, ошибки ловятся")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
