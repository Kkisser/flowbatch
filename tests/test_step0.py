"""Устойчивость прогона: предохранитель и переподключение к браузеру.

Запуск: python tests/test_step0.py

Три сценария из реального журнала падений 15.08:
1. Одинаковая ошибка на соседних задачах (кончились кредиты без сетевого
   кода) — очередь останавливается предохранителем, а не молотит FAILED
   по 2-4 минуты на задачу.
2. TargetClosedError посреди задачи — клиент переподключается и задача
   доделывается, а не падает.
3. Переподключиться не вышло (браузер закрыт) — очередь останавливается
   с внятной причиной, stop_kind = connection_lost.
"""

import io
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rich.console import Console

from flowbatch.config import Config
from flowbatch.flow_client import ERR_CONN, ERR_UNKNOWN, FlowError
from flowbatch.runner import Runner

CFG = Config(
    {"moderation": {"soften": {"attempts": 9}},
     "antiban": {"concurrency": 1, "max_retries": 0, "pause_between_jobs_sec": 0,
                 "pause_jitter_sec": 0, "long_pause_every": 0,
                 "max_same_errors_in_row": 3}},
    Path("config.yaml"),
)


@dataclass
class FakeJob:
    id: str
    kind: str = "image"
    prompt: str = "Кадр."
    batch: int = 1


class NoopNotifier:
    enabled = False

    def send(self, *a, **k):
        pass


class FakeLog:
    def __init__(self):
        self.records = []

    def write_result(self, job, status, started_at, **kw):
        self.records.append((job.id, status))


class FakeClient:
    def __init__(self, reconnect_ok=True):
        self.reconnect_ok = reconnect_ok
        self.reconnects = 0

    def reconnect(self, *a, **k):
        self.reconnects += 1
        return self.reconnect_ok

    def screenshot(self, path):
        raise RuntimeError("нет страницы")


def make_runner(client, log):
    return Runner(
        CFG, client=client, notifier=NoopNotifier(), log=log,
        console=Console(file=io.StringIO(), force_terminal=False, width=100),
    )


def main() -> int:
    failures: list[str] = []

    # 1. Предохранитель: 3 одинаковых провала подряд глушат очередь.
    log = FakeLog()
    r = make_runner(FakeClient(), log)
    calls = {"n": 0}

    def always_same(job, soften_used=0):
        calls["n"] += 1
        raise FlowError(ERR_UNKNOWN, "Кнопка «Создать» не разблокировалась за 60с")

    r._attempt = always_same
    out = r.run([FakeJob(f"img_{i}") for i in range(8)])
    if calls["n"] != 3:
        failures.append(f"предохранитель: попыток {calls['n']}, ждали 3")
    if not out.stopped_reason or "одинаков" not in out.stopped_reason:
        failures.append(f"предохранитель: причина не та: {out.stopped_reason!r}")
    if out.failed != 3 or out.skipped != 5:
        failures.append(f"предохранитель: failed={out.failed}, skipped={out.skipped}")

    # 1а. РАЗНЫЕ ошибки предохранитель не трогают: очередь дорабатывает.
    r = make_runner(FakeClient(), FakeLog())
    counter = {"n": 0}

    def always_different(job, soften_used=0):
        counter["n"] += 1
        raise FlowError(ERR_UNKNOWN, f"разовая ошибка №{counter['n']}")

    r._attempt = always_different
    out = r.run([FakeJob(f"img_{i}") for i in range(5)])
    if out.stopped_reason:
        failures.append(f"разные ошибки не должны глушить очередь: {out.stopped_reason!r}")
    if out.failed != 5:
        failures.append(f"разные ошибки: failed={out.failed}, ждали 5")

    # 2. Обрыв связи + удачное переподключение: задача доделывается.
    client = FakeClient(reconnect_ok=True)
    r = make_runner(client, FakeLog())
    state = {"n": 0}

    def flaky(job, soften_used=0):
        state["n"] += 1
        if state["n"] == 1:
            raise Exception(
                "TargetClosedError: Locator.count: Target page, context "
                "or browser has been closed"
            )

    r._attempt = flaky
    out = r.run([FakeJob("img_1")])
    if client.reconnects != 1:
        failures.append(f"reconnect вызывался {client.reconnects} раз, ждали 1")
    if out.done != 1 or out.failed:
        failures.append(f"после reconnect задача должна пройти: done={out.done}, failed={out.failed}")

    # 3. Переподключиться не вышло: очередь останавливается, kind = connection_lost.
    client = FakeClient(reconnect_ok=False)
    r = make_runner(client, FakeLog())

    def dead(job, soften_used=0):
        raise Exception("TargetClosedError: Target page, context or browser has been closed")

    r._attempt = dead
    out = r.run([FakeJob(f"img_{i}") for i in range(4)])
    if out.stop_kind != ERR_CONN:
        failures.append(f"stop_kind={out.stop_kind!r}, ждали {ERR_CONN!r}")
    if out.failed != 1 or out.skipped != 3:
        failures.append(f"обрыв: failed={out.failed}, skipped={out.skipped} — очередь не остановилась сразу")

    if failures:
        print("FAIL")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK: предохранитель глушит серию одинаковых ошибок, обрыв связи "
          "лечится переподключением, мёртвый браузер останавливает очередь")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
