"""Цепочка моделей Gemini и второй заход на другом бэкенде.

Запуск: python tests/test_backends.py   (без сети — httpx подменяется)
"""

import io
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rich.console import Console

from flowbatch import soften as S
from flowbatch.config import Config
from flowbatch.flow_client import ERR_MODERATION, FlowError
from flowbatch.runner import Runner

CFG_DATA = {
    "moderation": {
        "soften": {
            "enabled": True, "attempts": 3, "passes": 2, "backend": "rules",
            "gemini_models": ["m-quota", "m-busy", "m-good", "m-never"],
            "replacements": {}, "suffix": "Wholesome cartoon.",
        }
    },
    "antiban": {"concurrency": 1, "max_retries": 3},
}
CFG = Config(CFG_DATA, Path("config.yaml"))


class FakeResponse:
    def __init__(self, status: int, text: str = ""):
        self.status_code = status
        self._text = text
        self.text = text or '{"error":{"status":"X","message":"y"}}'

    def json(self):
        if self.status_code != 200:
            return {"error": {"status": "X", "message": "y"}}
        return {"candidates": [{"content": {"parts": [{"text": self._text}]}}]}


@dataclass
class FakeJob:
    """Настоящий dataclass: runner подменяет промпт через dataclasses.replace."""

    id: str = "job1"
    kind: str = "image"
    prompt: str = "original prompt"


def main() -> int:
    failures: list[str] = []

    # --- цепочка моделей: 429 и 503 проматываются, рабочая запоминается ---
    calls: list[str] = []

    def fake_post(url, headers=None, json=None, timeout=None):
        model = url.rsplit("/", 1)[-1].split(":")[0]
        calls.append(model)
        return {
            "m-quota": FakeResponse(429),
            "m-busy": FakeResponse(503),
            "m-good": FakeResponse(200, "rewritten text"),
        }.get(model, FakeResponse(404))

    real_post = S.httpx.post
    S.httpx.post = fake_post
    try:
        g = S.GeminiSoftener(CFG)
        g.key = "fake-key"
        out, what = g.soften("prompt", 1)
        if out != "rewritten text":
            failures.append(f"цепочка не дошла до рабочей модели: {out!r}")
        if calls != ["m-quota", "m-busy", "m-good"]:
            failures.append(f"порядок перебора неверен: {calls}")
        if "m-good" not in what:
            failures.append(f"в описании не та модель: {what!r}")

        # Второй вызов начинается сразу с рабочей — к выбывшим не возвращаемся.
        calls.clear()
        g.soften("prompt2", 2)
        if calls != ["m-good"]:
            failures.append(f"вернулся к выбывшим моделям: {calls}")
    finally:
        S.httpx.post = real_post

    # --- ответ, который не является переписанным промптом, отбрасывается ---
    # Живой повод: gemma-4 пересказывает инструкцию списком вместо правки,
    # и такой пересказ ушёл бы во Flow как промпт, сжигая генерацию.
    scene_long = "CAMERA: kitchen.\nACTION: something happens on the table.\n" * 3

    class Rambler:
        name = "rambler"
        available = True

        def soften(self, prompt, attempt, category="policy"):
            return ("* Задача: переписать промпт.\n" * 60), "пересказ"

    rambled = S.Softener(Rambler(), S.RuleSoftener(CFG), log=lambda s: None)
    out_r, what_r = rambled.soften(scene_long, 1)
    if "Задача: переписать" in out_r:
        failures.append("пересказ инструкции не отброшен — уйдёт во Flow как промпт")
    if "Wholesome" not in out_r:
        failures.append(f"после отбраковки не сработал фоллбэк на правила: {what_r}")

    class Truncator:
        name = "truncator"
        available = True

        def soften(self, prompt, attempt, category="policy"):
            return "CAM", "обрезок"

    trunc = S.Softener(Truncator(), S.RuleSoftener(CFG), log=lambda s: None)
    if trunc.soften(scene_long, 1)[0].strip() == "CAM":
        failures.append("обрезанный ответ не отброшен")

    # Добросовестный ответ той же длины проходит.
    class Honest:
        name = "honest"
        available = True

        def soften(self, prompt, attempt, category="policy"):
            return prompt.replace("something happens", "something calm happens"), "ок"

    honest = S.Softener(Honest(), S.RuleSoftener(CFG), log=lambda s: None)
    if "calm" not in honest.soften(scene_long, 1)[0]:
        failures.append("нормальное переписывание зря отбраковано")

    # --- второй заход: бэкенд переключается, лестница начинается заново ---
    class Backend:
        def __init__(self, name, available=True):
            self.name = name
            self.available = available
            self.seen: list[str] = []

        def soften(self, prompt, attempt, category="policy"):
            self.seen.append(prompt)
            return f"{self.name}[{attempt}] {prompt}", self.name

    a, b = Backend("alpha"), Backend("beta")
    soft = S.Softener(a, S.RuleSoftener(CFG), log=lambda s: None, spares=[b])
    if soft.name != "alpha":
        failures.append(f"стартовый бэкенд не тот: {soft.name}")
    if soft.next_backend() != "beta":
        failures.append("переключение на запасной не сработало")
    if soft.name != "beta":
        failures.append(f"после переключения имя не обновилось: {soft.name}")
    # Запасные кончились — остаются правила, а не None.
    if soft.next_backend() != "rules":
        failures.append("после запасных должны остаться правила")
    if soft.next_backend() is not None:
        failures.append("после правил переключать некуда")

    # Недоступный запасной пропускается.
    dead, alive = Backend("dead", available=False), Backend("alive")
    soft2 = S.Softener(Backend("first"), S.RuleSoftener(CFG), spares=[dead, alive])
    if soft2.next_backend() != "alive":
        failures.append("недоступный запасной не пропущен")

    # --- runner: после исчерпания ступеней идёт заход на другом бэкенде ---
    class NoopNotifier:
        enabled = False

        def send(self, *a, **k):
            pass

    back_a, back_b = Backend("alpha"), Backend("beta")
    soft3 = S.Softener(back_a, S.RuleSoftener(CFG), log=lambda s: None, spares=[back_b])
    r = Runner(
        CFG, client=None, notifier=NoopNotifier(), log=None,
        console=Console(file=io.StringIO(), force_terminal=False, width=100),
        softener=soft3,
    )
    r._attempt = lambda job, soften_used=0: (_ for _ in ()).throw(
        FlowError(ERR_MODERATION, "нельзя")
    )
    try:
        r._run_one(FakeJob())
        failures.append("задача должна была упасть после всех заходов")
    except FlowError:
        pass
    if not back_b.seen:
        failures.append("второй бэкенд так и не получил ни одного промпта")
    # Второй заход стартует с ИСХОДНОГО промпта, а не с переписанного.
    if back_b.seen and not back_b.seen[0].startswith("original prompt"):
        failures.append(f"второй заход начался не с исходника: {back_b.seen[0]!r}")

    if failures:
        print("FAIL")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK: цепочка моделей Gemini и второй заход на другом бэкенде")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
