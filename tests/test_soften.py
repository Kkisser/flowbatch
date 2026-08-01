"""Проверка смягчения промптов. Запуск: python tests/test_soften.py

Без сети: LLM-бэкенды здесь не вызываются, проверяется логика правил,
эскалация по попыткам и фоллбэк композиции.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flowbatch.config import Config
from flowbatch.soften import RuleSoftener, SoftenError, Softener, build_softener

CFG_DATA = {
    "moderation": {
        "soften": {
            "enabled": True,
            "attempts": 2,
            "backend": "rules",
            "replacements": {"blood": "red paint", "knife": "spoon", "gore": ""},
            "suffix": "Wholesome family-friendly cartoon. No violence.",
        }
    },
    "antiban": {"concurrency": 1},
}


class FailingLLM:
    name = "fake-llm"

    def soften(self, prompt: str, attempt: int):
        raise SoftenError("нарочно сломан")


class WorkingLLM:
    name = "fake-llm"

    def soften(self, prompt: str, attempt: int):
        return f"REWRITTEN[{attempt}]: {prompt[:20]}", "переписано фейком"


def main() -> int:
    failures: list[str] = []
    cfg = Config(CFG_DATA, Path("config.yaml"))
    rules = RuleSoftener(cfg)

    prompt = "A detective with a KNIFE, blood on the floor, gore everywhere."

    # Попытка 1: только приписка, слова не тронуты.
    out1, what1 = rules.soften(prompt, 1)
    if "KNIFE" not in out1 or "Wholesome family-friendly" not in out1:
        failures.append(f"попытка 1: ожидалась только приписка, получено: {what1}: {out1!r}")

    # Попытка 2: замены применены (без учёта регистра), пустая замена вычищает слово.
    out2, what2 = rules.soften(prompt, 2)
    if "knife" in out2.lower() or "blood" in out2.lower():
        failures.append(f"попытка 2: замены не применились: {out2!r}")
    if "spoon" not in out2 or "red paint" not in out2:
        failures.append(f"попытка 2: замены дали не то: {out2!r}")
    if "gore" in out2.lower().replace("everywhere", ""):
        failures.append(f"попытка 2: пустая замена не вычистила слово: {out2!r}")

    # Приписка не дублируется при повторном смягчении.
    out3, _ = rules.soften(out1, 2)
    if out3.lower().count("wholesome family-friendly") != 1:
        failures.append("приписка задублировалась при повторном смягчении")

    # Слово не должно заменяться внутри другого слова (границы \b).
    out4, _ = rules.soften("bloodhound is a dog breed", 2)
    if "red painthound" in out4:
        failures.append("замена сработала внутри слова: bloodhound")

    # Композиция: сломанный LLM падает на правила, а не роняет задачу.
    comp = Softener(FailingLLM(), rules, log=lambda s: None)
    out5, what5 = comp.soften(prompt, 1)
    if "Wholesome family-friendly" not in out5:
        failures.append(f"фоллбэк на правила не сработал: {what5}")

    # Композиция: рабочий LLM используется первым.
    comp2 = Softener(WorkingLLM(), rules, log=lambda s: None)
    out6, what6 = comp2.soften(prompt, 2)
    if not out6.startswith("REWRITTEN[2]"):
        failures.append(f"LLM-бэкенд не был использован: {what6}")

    # build_softener: rules-бэкенд без ключей всегда доступен.
    s = build_softener(cfg)
    if s is None or s.name != "rules":
        failures.append(f"build_softener(rules) дал {s and s.name}")

    # Выключенное смягчение — None.
    cfg_off = Config(
        {"moderation": {"soften": {"enabled": False}}, "antiban": {"concurrency": 1}},
        Path("config.yaml"),
    )
    if build_softener(cfg_off) is not None:
        failures.append("enabled=false не выключил смягчение")

    if failures:
        print("FAIL")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK: правила, эскалация, фоллбэк и сборка работают")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
