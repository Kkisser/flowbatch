"""Проверка смягчения промптов. Запуск: python tests/test_soften.py

Без сети: LLM-бэкенды здесь не вызываются, проверяется логика правил,
лестница попыток (диалог на №3), категории и фоллбэк композиции.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flowbatch.config import Config
from flowbatch.soften import (
    DIALOGUE_ONLY_ATTEMPT,
    LLM_INSTRUCTION,
    THIRD_PARTY_INSTRUCTION,
    RuleSoftener,
    SoftenError,
    Softener,
    _instruction,
    _mask_dialogue,
    _unmask_dialogue,
    build_softener,
    extract_dialogue,
    moderation_category,
)

CFG_DATA = {
    "moderation": {
        "phrases_third_party": ["сторонних поставщиков контента", "third-party content"],
        "phrases_unusual": ["подозрительн", "suspicious activity"],
        "soften": {
            "enabled": True,
            "attempts": 9,
            "backend": "rules",
            "replacements": {"blood": "red paint", "knife": "spoon", "gore": ""},
            "suffix": "Wholesome family-friendly cartoon. No violence.",
        },
    },
    "antiban": {"concurrency": 1},
}


class FailingLLM:
    name = "fake-llm"

    def soften(self, prompt: str, attempt: int, category: str = "policy"):
        raise SoftenError("нарочно сломан")


class WorkingLLM:
    name = "fake-llm"

    def __init__(self):
        self.categories: list[str] = []

    def soften(self, prompt: str, attempt: int, category: str = "policy"):
        self.categories.append(category)
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

    # Композиция: рабочий LLM используется первым, категория доезжает до него.
    llm = WorkingLLM()
    comp2 = Softener(llm, rules, log=lambda s: None)
    out6, what6 = comp2.soften(prompt, 2, category="third_party")
    if not out6.startswith("REWRITTEN[2]"):
        failures.append(f"LLM-бэкенд не был использован: {what6}")
    if llm.categories != ["third_party"]:
        failures.append(f"категория не дошла до бэкенда: {llm.categories}")

    # --- лестница: попытка №3 = только реплики, LLM не трогается ---
    scene = (
        "CAMERA: static shot.\n"
        'VALERA: «Это чего?»\n'
        "ACTION: THE BONE falls down.\n"
        'THE BONE: «Выселение. Десять уровней давления.»\n'
        'VALERA: «Это чего?»\n'  # дубль — должен схлопнуться
    )
    d = extract_dialogue(scene)
    if d != "Это чего?\nВыселение. Десять уровней давления.":
        failures.append(f"extract_dialogue дал не то: {d!r}")

    out7, what7 = comp2.soften(scene, DIALOGUE_ONLY_ATTEMPT)
    if out7 != d:
        failures.append(f"попытка №3 не превратилась в голые реплики: {out7!r}")
    if len(llm.categories) != 1:
        failures.append("попытка №3 зачем-то сходила в LLM")

    # Без реплик попытка №3 идёт обычным путём.
    out8, _ = comp2.soften("no dialogue here at all", DIALOGUE_ONLY_ATTEMPT)
    if not out8.startswith(f"REWRITTEN[{DIALOGUE_ONLY_ATTEMPT}]"):
        failures.append(f"попытка №3 без реплик не упала в LLM: {out8!r}")

    # --- маска реплик: LLM получает плейсхолдеры, а не текст реплик ---
    masked, reps = _mask_dialogue(scene)
    if "Это чего?" in masked or "Выселение" in masked:
        failures.append(f"маска не спрятала реплики: {masked!r}")
    if "«§R1§»" not in masked:
        failures.append(f"плейсхолдер не в кавычках: {masked!r}")
    back, lost = _unmask_dialogue(masked, reps)
    if back != scene or lost:
        failures.append(f"маска-демаска не вернула оригинал (lost={lost})")
    # Потерянный плейсхолдер — реплика дописывается в конец, а не пропадает.
    back2, lost2 = _unmask_dialogue("scene without placeholders", reps)
    if not lost2 or "Это чего?" not in back2 or "DIALOGUE (verbatim)" not in back2:
        failures.append(f"потерянные реплики не дописались: {back2!r}")

    # Реплики в ПРЯМЫХ кавычках (так написана вся очередь PODMENA) тоже
    # должны защищаться — иначе LLM переписывает их свободно.
    straight = ('0-2s: Nika declares in a loud voice: "Завтра я — это я."\n'
                'Add the line "no reverb on the voices" to the prompt.')
    if extract_dialogue(straight) != "Завтра я — это я.":
        failures.append(f"прямые кавычки: извлечено не то: {extract_dialogue(straight)!r}")
    ms, rs = _mask_dialogue(straight)
    if "Завтра я" in ms:
        failures.append("прямые кавычки: реплика не спрятана")
    if "no reverb on the voices" not in ms:
        failures.append("английская строка в кавычках зря замаскирована")
    bs, ls = _unmask_dialogue(ms, rs)
    if bs != straight or ls:
        failures.append(f"прямые кавычки: демаска не вернула оригинал (lost={ls})")

    # Через композицию: LLM видит маску, наружу выходит оригинальный текст реплик.
    class EchoLLM(WorkingLLM):
        def soften(self, prompt: str, attempt: int, category: str = "policy"):
            self.categories.append(category)
            return prompt, "эхо"  # возвращает вход как есть — маска должна сняться

    echo = EchoLLM()
    comp3 = Softener(echo, rules, log=lambda s: None)
    out9, _ = comp3.soften(scene, 1)
    if "§R" in out9 or "Выселение. Десять уровней давления." not in out9:
        failures.append(f"реплики не вернулись из маски: {out9!r}")

    # --- категория по тексту ошибки ---
    if moderation_category(cfg, "видео может нарушать наши правила") != "policy":
        failures.append("policy-ошибка распознана как third_party")
    if moderation_category(
        cfg, "Сейчас не могу создать такое видео из-за интересов СТОРОННИХ "
             "ПОСТАВЩИКОВ КОНТЕНТА. Измените запрос."
    ) != "third_party":
        failures.append("third_party-ошибка не распознана")
    if moderation_category(
        cfg, "Ошибка. Мы заметили ПОДОЗРИТЕЛЬНУЮ активность. Узнайте больше."
    ) != "unusual":
        failures.append("подозрительная активность не распознана как unusual")
    # unusual важнее third_party, если оба в одном куске текста.
    if moderation_category(
        cfg, "подозрительную активность ... сторонних поставщиков контента"
    ) != "unusual":
        failures.append("приоритет unusual над third_party сломан")

    # --- температура по попыткам: 0.2 на первых двух, дальше рост до 0.9 ---
    from flowbatch.soften import _temp

    expected = {1: 0.2, 2: 0.2, 4: 0.4, 5: 0.5, 9: 0.9}
    got = {a: _temp(a) for a in expected}
    if got != expected:
        failures.append(f"температура по попыткам не та: {got}")

    # --- инструкции: база и эскалация ---
    if _instruction(1, "policy") != LLM_INSTRUCTION:
        failures.append("попытка 1 (policy): инструкция должна быть базовой")
    if _instruction(1, "third_party") != THIRD_PARTY_INSTRUCTION:
        failures.append("попытка 1 (third_party): не та инструкция")
    i5, i9 = _instruction(5, "policy"), _instruction(9, "policy")
    if "№5" not in i5 or "смелее" not in i5:
        failures.append(f"эскалация на попытке 5 не та: …{i5[-120:]!r}")
    if "№9" not in i9 or "радикально" not in i9.lower():
        failures.append(f"эскалация на попытке 9 не та: …{i9[-120:]!r}")

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
    print("OK: правила, лестница из 9 попыток, диалог на №3, категории, фоллбэк")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
