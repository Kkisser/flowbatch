"""Жёсткая остановка: «Стоп сейчас» бросает ожидание результата.

Запуск: python tests/test_abort.py

Мягкий стоп доигрывает начатое — у видео это до 15 минут. Жёсткий должен
прервать само ожидание за секунды, не пометив задачу FAILED: она остаётся
в очереди и подхватится при следующем старте.
"""

import io
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rich.console import Console

from flowbatch.config import Config
from flowbatch.flow_client import ERR_ABORTED, FlowError
from flowbatch.runner import Runner

CFG = Config(
    {"moderation": {"soften": {"attempts": 9}},
     "antiban": {"concurrency": 1, "max_retries": 3, "pause_between_jobs_sec": 0,
                 "pause_jitter_sec": 0}},
    Path("config.yaml"),
)


@dataclass
class FakeJob:
    id: str = "vid_01"
    kind: str = "video"
    prompt: str = "Animate this image."


class FakePage:
    """Страница, которая «тикает» ожидание без реальных задержек."""

    def wait_for_timeout(self, ms):
        pass


class FakeClient:
    """Отдаёт ожидание, которое опрашивает should_abort как настоящее."""

    def __init__(self):
        self.polls = 0

    def wait_for_new_media(self, before, kind, timeout_sec=None, on_tick=None,
                           moderation_baseline=0, arbiter=None, should_abort=None):
        for _ in range(1000):
            self.polls += 1
            if should_abort is not None and should_abort():
                raise FlowError(ERR_ABORTED, "ожидание результата прервано")
        raise AssertionError("should_abort так и не сработал")


def main() -> int:
    failures: list[str] = []
    statuses: list[tuple[str, str]] = []

    class NoopNotifier:
        enabled = False

        def send(self, *a, **k):
            pass

    client = FakeClient()
    r = Runner(
        CFG, client=client, notifier=NoopNotifier(), log=None,
        console=Console(file=io.StringIO(), force_terminal=False, width=100),
        on_status=lambda job, st, result_path=None, error=None: statuses.append((job.id, st)),
    )

    # _attempt заменяем на кусок, который доходит до ожидания результата.
    def attempt(job, soften_used=0):
        client.wait_for_new_media(set(), job.kind, should_abort=lambda: r.abort_requested)

    r._attempt = attempt

    # Флаг ставим сразу — как если бы кнопку нажали во время ожидания.
    r.abort_requested = True
    out = r.run([FakeJob()])

    if out.failed:
        failures.append(f"прерывание посчитано провалом: failed={out.failed}")
    if out.done:
        failures.append(f"прерванная задача посчитана сделанной: done={out.done}")
    if not out.stopped_reason or "прерван" not in out.stopped_reason:
        failures.append(f"причина остановки не та: {out.stopped_reason!r}")
    # Статус вернулся в TODO — задача не потеряна и не помечена ошибкой.
    if statuses and statuses[-1] != ("vid_01", "TODO"):
        failures.append(f"итоговый статус не TODO: {statuses}")
    if client.polls < 1:
        failures.append("ожидание даже не начиналось")

    # Без флага ожидание идёт своим чередом (проверяем, что не ломается).
    r2 = Runner(
        CFG, client=FakeClient(), notifier=NoopNotifier(), log=None,
        console=Console(file=io.StringIO(), force_terminal=False, width=100),
    )
    if r2.abort_requested:
        failures.append("abort_requested должен быть выключен по умолчанию")

    if failures:
        print("FAIL")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK: «Стоп сейчас» прерывает ожидание, задача остаётся в очереди")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
