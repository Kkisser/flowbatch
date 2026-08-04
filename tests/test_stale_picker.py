"""Реакция на отставший пикер. Запуск: python tests/test_stale_picker.py

Живой случай podmena_s01e02_07_uv_anim: «В пикере не найден элемент с uuid».
Список пикера Flow грузится вместе со страницей и не видит медиа, созданные
в этой же сессии — в проекте было 34 элемента, пикер показывал 20.
"""

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rich.console import Console

from flowbatch.config import Config
from flowbatch.flow_client import ERR_STALE_PICKER, FlowError
from flowbatch.runner import STALE_RELOAD_MAX, Runner

CFG = Config(
    {"moderation": {"soften": {"attempts": 9}}, "antiban": {"concurrency": 1,
     "max_retries": 3}},
    Path("config.yaml"),
)


class FakeJob:
    id = "podmena_s01e02_07_uv_anim"
    kind = "video"
    prompt = "Animate this image."


class FakeClient:
    """Клиент, у которого пикер «прозревает» после N-й перезагрузки."""

    def __init__(self, heals_after: int):
        self.heals_after = heals_after
        self.reloads = 0
        self.attempts = 0

    def reload_page(self):
        self.reloads += 1

    # то, что дёргает _attempt до падения
    def focus(self):
        pass


def make_runner(client) -> Runner:
    return Runner(
        CFG, client=client, notifier=None, log=None,
        console=Console(file=io.StringIO(), force_terminal=False, width=100),
    )


def main() -> int:
    failures: list[str] = []

    # Перезагрузка помогает со второго захода — задача должна пройти.
    client = FakeClient(heals_after=1)
    r = make_runner(client)

    def attempt_ok_after_reload(job, soften_used=0):
        client.attempts += 1
        if client.reloads < client.heals_after:
            raise FlowError(ERR_STALE_PICKER, "В пикере не найден элемент с uuid abc")

    r._attempt = attempt_ok_after_reload
    try:
        r._run_one(FakeJob())
    except FlowError as exc:
        failures.append(f"задача упала, хотя перезагрузка должна была помочь: {exc}")
    if client.reloads != 1:
        failures.append(f"перезагрузок должно быть 1, а было {client.reloads}")
    if client.attempts != 2:
        failures.append(f"проходов должно быть 2, а было {client.attempts}")

    # Перезагрузка не помогает — сдаёмся, но не крутимся вечно.
    client2 = FakeClient(heals_after=99)
    r2 = make_runner(client2)

    def attempt_always_stale(job, soften_used=0):
        client2.attempts += 1
        raise FlowError(ERR_STALE_PICKER, "В пикере не найден элемент с uuid abc")

    r2._attempt = attempt_always_stale
    try:
        r2._run_one(FakeJob())
        failures.append("безнадёжный случай не поднял ошибку")
    except FlowError as exc:
        if exc.kind != ERR_STALE_PICKER:
            failures.append(f"не тот вид ошибки в конце: {exc.kind}")
    if client2.reloads != STALE_RELOAD_MAX:
        failures.append(
            f"перезагрузок должно быть {STALE_RELOAD_MAX}, а было {client2.reloads}"
        )

    if failures:
        print("FAIL")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK: отставший пикер лечится перезагрузкой, безнадёжный случай не зацикливается")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
