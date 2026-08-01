"""Проверка параллельного режима без браузера.

Главный риск параллели — не скорость, а подмена результата: три вкладки
смотрят в один список медиа, и задача может утащить чужой файл. Проверяем
именно это, плюс что общий ритм пауз действительно разводит запуски.
"""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flowbatch.config import Config
from flowbatch.flow_client import FlowClient, MediaItem
from flowbatch.parallel import MediaArbiter, Pacer
from flowbatch.queue import Job
from flowbatch.runner import Runner

ROOT = Path(__file__).resolve().parents[1]


class FakePage:
    def wait_for_timeout(self, ms: int) -> None:
        return None


def make_client(snapshots, generating):
    """Клиент без браузера: подменяем всё, что лезет на страницу.

    snapshots — что «видно в библиотеке» на каждом опросе,
    generating — заблокирована ли МОЯ кнопка «Создать» на этом опросе.
    """
    cfg = Config.load(ROOT / "config.yaml")
    cfg.set("generation.poll_interval_sec", 0)
    c = FlowClient(cfg)
    c._page = FakePage()
    state = {"i": -1}

    def snap():
        state["i"] += 1
        idx = min(state["i"], len(snapshots) - 1)
        return {n: MediaItem(name=n, url=f"?name={n}", tag=t) for n, t in snapshots[idx]}

    c.media_snapshot = snap
    c.is_generating = lambda: generating[min(state["i"], len(generating) - 1)]
    c.scroll_library_to_fresh = lambda: None
    c.raise_for_errors = lambda since: None
    c.moderation_state = lambda: {"count": 0, "snippet": None}
    return c


def test_ignores_neighbours_result_while_still_generating():
    """Чужой файл всплыл, пока моя генерация ещё идёт — не забираю."""
    snapshots = [
        [],                                    # опрос 1: пусто
        [("img_B", "img")],                    # опрос 2: сосед закончил раньше
        [("img_B", "img")],                    # опрос 3: он всё ещё чужой
        [("img_B", "img"), ("vid_A", "video")],  # опрос 4: готов и мой
    ]
    generating = [True, True, True, False]
    c = make_client(snapshots, generating)
    item = c.wait_for_new_media(set(), "video", timeout_sec=30, arbiter=MediaArbiter())
    assert item.name == "vid_A", f"забрал чужой результат: {item.name}"


def test_skips_claimed_by_others():
    """Файл, уже забранный другой вкладкой, не рассматривается вовсе."""
    arb = MediaArbiter()
    arb.claim("img_B")
    c = make_client([[("img_B", "img"), ("vid_A", "video")]], [False])
    item = c.wait_for_new_media(set(), "video", timeout_sec=30, arbiter=arb)
    assert item.name == "vid_A", f"взял занятое: {item.name}"


def test_two_tabs_never_take_the_same_file():
    """Один и тот же файл двум задачам не достанется."""
    arb = MediaArbiter()
    both = [[("m1", "img"), ("m2", "img")]]
    first = make_client(both, [False]).wait_for_new_media(set(), "image", timeout_sec=30, arbiter=arb)
    second = make_client(both, [False]).wait_for_new_media(set(), "image", timeout_sec=30, arbiter=arb)
    assert first.name != second.name, "две задачи скачали бы один файл"


def test_single_tab_behaviour_unchanged():
    """Без арбитра поведение прежнее: берём первое новое, ничего не ждём."""
    c = make_client([[("m1", "img")]], [True])  # кнопка «занята», но нам всё равно
    item = c.wait_for_new_media(set(), "image", timeout_sec=30)
    assert item.name == "m1"


def test_arbiter_is_atomic():
    """Захват из потоков: победитель ровно один."""
    arb = MediaArbiter()
    wins = []
    lock = threading.Lock()

    def grab():
        if arb.claim("x"):
            with lock:
                wins.append(1)

    ts = [threading.Thread(target=grab) for _ in range(24)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(wins) == 1, f"захватили {len(wins)} раз"


def test_pacer_staggers_workers():
    """Три вкладки не стартуют одновременно: паузы общие."""
    cfg = Config.load(ROOT / "config.yaml")
    cfg.set("antiban.pause_between_jobs_sec", 1)
    cfg.set("antiban.pause_jitter_sec", 0)
    cfg.set("antiban.long_pause_every", 0)
    pacer = Pacer(cfg)

    class Quiet:
        def print(self, *a, **k): pass

    waited = []
    lock = threading.Lock()

    def worker():
        t0 = time.monotonic()
        pacer.wait_turn(lambda: False, Quiet())
        with lock:
            waited.append(time.monotonic() - t0)

    ts = [threading.Thread(target=worker) for _ in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    waited.sort()
    assert waited[0] < 0.4, f"первый должен идти сразу, ждал {waited[0]:.2f}с"
    assert 0.7 < waited[1] < 1.4, f"второй должен ждать ~1с, ждал {waited[1]:.2f}с"
    assert 1.7 < waited[2] < 2.4, f"третий должен ждать ~2с, ждал {waited[2]:.2f}с"


def test_runner_take_consumes_queue_once():
    """Общая очередь: каждая задача уходит ровно одному воркеру."""
    from queue import Empty, Queue

    cfg = Config.load(ROOT / "config.yaml")
    jobs = [Job(id=f"J{i}", kind="image", prompt="p") for i in range(9)]
    q: Queue = Queue()
    for j in jobs:
        q.put(j)

    seen = []
    lock = threading.Lock()

    def take():
        try:
            return q.get_nowait()
        except Empty:
            return None

    class Quiet:
        def print(self, *a, **k): pass
        def rule(self, *a, **k): pass

    class FakeLog:
        def write_result(self, *a, **k): return {}

    def run_one(_self, job):
        time.sleep(0.01)
        with lock:
            seen.append(job.id)

    runners = []
    for _ in range(3):
        r = Runner(cfg, None, None, FakeLog(), Quiet(), pacer=_NoPause())
        r._run_one = run_one.__get__(r, Runner)
        runners.append(r)

    ts = [threading.Thread(target=lambda r=r: r.run(take=take, total=len(jobs))) for r in runners]
    [t.start() for t in ts]
    [t.join() for t in ts]

    assert sorted(seen) == sorted(j.id for j in jobs), f"очередь разошлась: {sorted(seen)}"
    assert len(seen) == len(set(seen)), "задача выполнена дважды"


class _NoPause:
    def wait_turn(self, stop, console): return None


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"OK: {len(tests)} проверок параллельного режима")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
