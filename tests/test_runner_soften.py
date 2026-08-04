"""Эскалация лестницы переписывания в Runner. Запуск: python tests/test_runner_soften.py

Воспроизводит живой баг sd_s01e01_02_karamel_anim: смягчитель вернул текст
без изменений, и старая логика хоронила задачу после 1 попытки из 9 вместо
шага на следующую ступень.
"""

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rich.console import Console

from flowbatch.config import Config
from flowbatch.runner import Runner

CFG = Config(
    {"moderation": {"soften": {"attempts": 9}}, "antiban": {"concurrency": 1}},
    Path("config.yaml"),
)


class LadderStub:
    """Смягчитель, который «просыпается» только с нужной ступени."""

    name = "stub"

    def __init__(self, wake_at: int):
        self.wake_at = wake_at
        self.calls: list[int] = []

    def soften(self, prompt: str, attempt: int, category: str = "policy"):
        self.calls.append(attempt)
        if attempt >= self.wake_at:
            return f"CHANGED[{attempt}] {prompt}", f"ступень {attempt}"
        return prompt, "ничего не менял"  # неизменённый текст


def make_runner(softener) -> Runner:
    return Runner(
        CFG, client=None, notifier=None, log=None,
        console=Console(file=io.StringIO(), force_terminal=False, width=100),
        softener=softener,
    )


def main() -> int:
    failures: list[str] = []

    # Ступени 1-2 возвращают то же самое, 3 — меняет. Старый код сдался бы на 1.
    stub = LadderStub(wake_at=3)
    r = make_runner(stub)
    scene = "original scene text"
    new, what, used, idle = r._soften_escalate(scene, {scene.strip()}, 0, 9, "policy")
    if new != f"CHANGED[3] {scene}":
        failures.append(f"эскалация не дошла до рабочей ступени: {new!r}")
    if used != 3 or stub.calls != [1, 2, 3]:
        failures.append(f"счёт попыток неверен: used={used}, calls={stub.calls}")
    # Ступени 1-2 вернули тот же текст — они должны быть перечислены как
    # «генерация не запускалась», иначе в Telegram это выглядит пропуском.
    if idle != [1, 2]:
        failures.append(f"холостые ступени посчитаны неверно: {idle}")

    # Продолжение с середины: уже потрачено 3, следующая рабочая — 5.
    stub2 = LadderStub(wake_at=5)
    r2 = make_runner(stub2)
    new2, _, used2, idle2 = r2._soften_escalate(scene, {scene.strip()}, 3, 9, "policy")
    if used2 != 5 or stub2.calls != [4, 5]:
        failures.append(f"продолжение лестницы неверно: used={used2}, calls={stub2.calls}")
    if not new2.startswith("CHANGED[5]"):
        failures.append(f"не тот текст со ступени 5: {new2!r}")
    if idle2 != [4]:
        failures.append(f"холостые ступени при продолжении неверны: {idle2}")

    # Все ступени вернули уже виденное — None, попытки исчерпаны, цикла нет.
    stub3 = LadderStub(wake_at=99)
    r3 = make_runner(stub3)
    new3, _, used3, idle3 = r3._soften_escalate(scene, {scene.strip()}, 0, 9, "policy")
    if new3 is not None or used3 != 9 or len(stub3.calls) != 9:
        failures.append(f"исчерпание сломано: new={new3!r}, used={used3}, calls={stub3.calls}")

    # Кандидат, совпадающий с УЖЕ ОТКЛОНЁННЫМ (не только с текущим), пропускается.
    class Repeater:
        name = "stub"

        def soften(self, prompt, attempt, category="policy"):
            return ("rejected once already", "повтор") if attempt == 1 else ("fresh text", "ново")

    r4 = make_runner(Repeater())
    new4, _, used4, _idle4 = r4._soften_escalate(
        scene, {scene.strip(), "rejected once already"}, 0, 9, "policy"
    )
    if new4 != "fresh text" or used4 != 2:
        failures.append(f"повтор отклонённого не отсечён: {new4!r}, used={used4}")

    if failures:
        print("FAIL")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK: эскалация лестницы — неизменённый текст не хоронит задачу и не перезапускается")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
