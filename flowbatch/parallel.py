"""Параллельный прогон очереди в нескольких вкладках браузера.

Зачем: генерация видео занимает 1–5 минут, и всё это время вкладка просто
ждёт. Три вкладки ждут те же минуты одновременно — очередь из 13 роликов
проходит примерно втрое быстрее.

Три вещи, из-за которых это не «просто запустить Runner в три потока»:

1. Playwright (sync) не потокобезопасен. У каждого воркера собственный
   sync_playwright() и собственное подключение к CDP — они живут внутри
   своего потока и наружу не отдаются. Браузер при этом один и тот же:
   несколько CDP-подключений к одному браузеру допустимы.

2. Вкладки смотрят в ОДИН проект, а значит в один список медиа. Готовность
   определяется диффом этого списка, поэтому чужой результат теоретически
   может всплыть в моей вкладке. Защита двойная: общий реестр занятых uuid
   (MediaArbiter) и проверка «моя кнопка Создать ещё заблокирована — значит
   моя генерация не закончилась» внутри wait_for_new_media.

3. Антибан считается на аккаунт, а не на вкладку. Паузы между задачами
   раздаёт общий Pacer, поэтому три вкладки дают тот же темп запросов, что
   и одна, — просто ожидание результатов идёт внахлёст.

Потолок — 3 вкладки, жёстко. Выше поднимать нельзя.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from queue import Empty, Queue
from typing import Any, Callable

from rich.console import Console

from .config import Config
from .flow_client import FlowClient, FlowClientError
from .notify import Notifier
from .queue import Job, RunLog
from .runner import RunOutcome, Runner

# Потолок из спецификации. Не параметр — константа.
MAX_TABS = 3


class MediaArbiter:
    """Реестр «этот результат уже забран». Общий на все вкладки.

    Без него две вкладки, увидев в общем списке один и тот же новый файл,
    скачали бы его обе: одна задача получила бы чужой результат, а вторая
    ушла бы в таймаут. Захват атомарный — выигрывает тот, кто первый.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._claimed: set[str] = set()

    def is_free(self, uuid: str) -> bool:
        with self._lock:
            return uuid not in self._claimed

    def claim(self, uuid: str) -> bool:
        with self._lock:
            if uuid in self._claimed:
                return False
            self._claimed.add(uuid)
            return True


class Pacer:
    """Общий на все вкладки ритм: когда следующей задаче можно стартовать.

    Слот бронируется под замком, а спится уже без него — иначе воркеры
    выстроились бы в очередь на самом замке. Побочный и полезный эффект:
    запуски разъезжаются во времени, поэтому и результаты приходят вразнобой,
    а не пачкой в один и тот же опрос.
    """

    def __init__(self, cfg: Config) -> None:
        self.base = float(cfg.get("antiban.pause_between_jobs_sec", 30))
        self.jitter = float(cfg.get("antiban.pause_jitter_sec", 10))
        self.long_every = int(cfg.get("antiban.long_pause_every", 10))
        self.long_sec = float(cfg.get("antiban.long_pause_sec", 300))
        self._lock = threading.Lock()
        self._next_at = 0.0
        self._count = 0

    def wait_turn(self, stop: Callable[[], bool], console: Console) -> None:
        with self._lock:
            now = time.monotonic()
            self._count += 1
            wait = max(0.0, self._next_at - now)
            if self.long_every and self._count % self.long_every == 0:
                delay = self.long_sec
                long_pause = True
            else:
                delay = max(1.0, self.base + random.uniform(-self.jitter, self.jitter))
                long_pause = False
            self._next_at = max(now, self._next_at) + delay

        if wait <= 0:
            return
        if long_pause:
            console.print(f"[dim]длинная пауза {wait:.0f}с (задач с начала: {self._count})[/dim]")
        else:
            console.print(f"[dim]жду свою очередь {wait:.0f}с…[/dim]")
        waited = 0.0
        while waited < wait and not stop():
            step = min(0.5, wait - waited)
            time.sleep(step)
            waited += step


class LabeledConsole:
    """Обёртка над общей консолью: помечает строки номером вкладки.

    Лог у панели один, пишут в него три воркера — без пометки понять, чья
    строка, невозможно. rule() превращается в обычную строку: разделители во
    всю ширину от трёх потоков сразу читаются как каша.
    """

    def __init__(self, console: Console, label: str, color: str) -> None:
        self._c = console
        self._prefix = f"[{color}]{label}[/{color}] "

    def print(self, *args: Any, **kwargs: Any) -> None:
        # end="\r" — это счётчик секунд ожидания, он рассчитан на перерисовку
        # одной строки в терминале. Трём воркерам в общий лог он насыпал бы по
        # строке каждые три секунды; сколько ждёт каждая вкладка, и так видно
        # в панели.
        if kwargs.pop("end", "\n") != "\n":
            return
        if args and isinstance(args[0], str):
            args = (self._prefix + args[0],) + args[1:]
        self._c.print(*args, **kwargs)

    def rule(self, text: str = "", **kwargs: Any) -> None:
        self._c.print(f"{self._prefix}[dim]──[/dim] {text}")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._c, name)


@dataclass
class TabState:
    """Что показывать про вкладку в панели."""

    target_id: str
    label: str
    project: str = ""
    job_id: str = ""
    status: str = "ожидает"
    started_at: float = 0.0
    done: int = 0
    failed: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "label": self.label,
            "project": self.project,
            "job_id": self.job_id,
            "status": self.status,
            "elapsed": int(time.time() - self.started_at) if self.started_at else 0,
            "done": self.done,
            "failed": self.failed,
        }


@dataclass
class ParallelOutcome(RunOutcome):
    """Сводка параллельного прогона плюс разбивка по вкладкам."""

    per_tab: list[dict[str, Any]] = field(default_factory=list)


class ParallelRunner:
    """Гоняет одну очередь несколькими вкладками одного браузера."""

    COLORS = ["cyan", "magenta", "green"]

    def __init__(
        self,
        cfg: Config,
        target_ids: list[str],
        notifier: Notifier,
        log: RunLog,
        console: Console,
        dry_run: bool = False,
        on_status: Any = None,
        softener_factory: Callable[[], Any] | None = None,
        resolver_factory: Callable[[Any, str], Any] | None = None,
        queue_project: str | None = None,
        on_upload: Callable[[Any], None] | None = None,
        pacer: Pacer | None = None,
        project_lock: Any = None,
        bring_front: bool = True,
    ) -> None:
        if not target_ids:
            raise ValueError("не выбрано ни одной вкладки")
        if len(target_ids) > MAX_TABS:
            raise ValueError(f"больше {MAX_TABS} вкладок на один проект нельзя")
        self.cfg = cfg
        self.target_ids = target_ids
        self.notifier = notifier
        self.log = log
        self.console = console
        self.dry_run = dry_run
        self.on_status = on_status
        self.softener_factory = softener_factory
        self.resolver_factory = resolver_factory
        self.queue_project = queue_project
        self.on_upload = on_upload
        # Панель гоняет несколько ПРОЕКТОВ одновременно, и у неё pacer и замок
        # проектов общие на все прогоны: паузы считаются на аккаунт, а не на
        # проект, и два прогона не создадут одноимённые проекты наперегонки.
        # bring_front=False — когда прогонов несколько, тянуть вкладку на
        # передний план нельзя вообще: они дёргали бы браузер друг у друга.
        self.bring_front = bring_front

        self.arbiter = MediaArbiter()
        self.pacer = pacer or Pacer(cfg)
        self.project_lock = project_lock or threading.Lock()
        self.refs_lock = threading.Lock()      # заливка референсов — по одному
        self.stop_requested = False
        self.tabs: list[TabState] = [
            TabState(target_id=t, label=f"#{i + 1}") for i, t in enumerate(target_ids)
        ]
        self.project_id: str = ""
        self._runners: list[Runner] = []
        self._q: Queue[Job] = Queue()
        self._outcomes: list[RunOutcome] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ доступ

    def stop(self) -> None:
        self.stop_requested = True
        for r in self._runners:
            r.stop_requested = True

    def tab_states(self) -> list[dict[str, Any]]:
        return [t.as_dict() for t in self.tabs]

    # ------------------------------------------------------------------- прогон

    def run(self, jobs: list[Job]) -> ParallelOutcome:
        for j in jobs:
            self._q.put(j)
        total = len(jobs)
        started = time.time()

        threads = []
        for idx, tab in enumerate(self.tabs):
            th = threading.Thread(
                target=self._worker, args=(idx, tab, total), daemon=True,
                name=f"flowbatch-tab-{idx + 1}",
            )
            threads.append(th)
            th.start()
        for th in threads:
            th.join()

        out = ParallelOutcome(total=total)
        for o in self._outcomes:
            out.done += o.done
            out.failed += o.failed
            out.failures.extend(o.failures)
            if o.stopped_reason and not out.stopped_reason:
                out.stopped_reason = o.stopped_reason
            if o.stop_kind and not out.stop_kind:
                out.stop_kind = o.stop_kind
        out.skipped = max(0, total - out.done - out.failed)
        out.elapsed_sec = time.time() - started
        out.per_tab = self.tab_states()
        return out

    def _take(self, tab: TabState) -> Job | None:
        """Взять следующую задачу из общей очереди."""
        if self.stop_requested:
            return None
        try:
            job = self._q.get_nowait()
        except Empty:
            tab.status = "очередь пуста"
            tab.job_id = ""
            tab.started_at = 0.0
            return None
        tab.job_id = job.id
        tab.status = "работает"
        tab.started_at = time.time()
        return job

    def _worker(self, idx: int, tab: TabState, total: int) -> None:
        console = LabeledConsole(self.console, tab.label, self.COLORS[idx % len(self.COLORS)])
        client = FlowClient(self.cfg, target_id=tab.target_id)
        # Передний план достаётся максимум первой вкладке — и только если этот
        # прогон в панели один (bring_front). Остальные держатся «живыми»
        # через CDP, иначе потоки перещёлкивали бы вкладки друг у друга.
        client.bring_to_front = self.bring_front and idx == 0
        tab.status = "подключаюсь"
        try:
            client.connect()
        except FlowClientError as exc:
            tab.status = "нет связи"
            console.print(f"[red]{exc}[/red]")
            return

        try:
            project_id = self._open_project(client, console, tab)
            if not project_id:
                return

            resolver = self.resolver_factory(client, project_id) if self.resolver_factory else None
            softener = self.softener_factory() if self.softener_factory else None

            runner = Runner(
                self.cfg, client, self.notifier, self.log, console,
                dry_run=self.dry_run, resolver=resolver, on_status=self.on_status,
                project_id=project_id, softener=softener,
                arbiter=self.arbiter, pacer=self.pacer,
            )
            with self._lock:
                self._runners.append(runner)
            if self.stop_requested:
                runner.stop_requested = True

            outcome = runner.run(take=lambda: self._take(tab), total=total)
            tab.done, tab.failed = outcome.done, outcome.failed
            tab.status = "закончил"
            tab.job_id = ""
            tab.started_at = 0.0
            with self._lock:
                self._outcomes.append(outcome)
            if outcome.stopped_reason:
                # Стоп-ошибки (кончились кредиты, подозрительная активность)
                # касаются аккаунта целиком — тормозим и соседние вкладки.
                self.stop()
        except Exception as exc:  # noqa: BLE001 — падение одной вкладки не роняет остальные
            tab.status = "упал"
            console.print(f"[red]вкладка отвалилась: {exc}[/red]")
        finally:
            client.close()

    def _open_project(self, client: Any, console: Any, tab: TabState) -> str:
        """Привести вкладку в нужный проект. Под общим замком.

        Замок здесь не про скорость: без него три вкладки, не найдя проект,
        создали бы три одноимённых проекта разом.
        """
        with self.project_lock:
            if self.queue_project:
                how = client.ensure_project(self.queue_project)
                verb = {"current": "уже открыт", "opened": "открыт", "created": "создан"}[how]
                console.print(f"проект {self.queue_project!r} {verb}")
            project_id = client.current_project_id() or ""
            if not project_id:
                tab.status = "нет проекта"
                console.print("[red]во вкладке открыт список проектов, а не проект[/red]")
                return ""
            if not self.project_id:
                self.project_id = project_id
            elif project_id != self.project_id:
                tab.status = "чужой проект"
                console.print(
                    "[red]вкладка смотрит в другой проект. Все выбранные вкладки должны "
                    "работать в одном проекте: укажи @project в очереди либо открой "
                    "один и тот же проект во всех вкладках.[/red]"
                )
                return ""
            tab.project = client.project_name() or project_id[:8]
            tab.status = "готова"
            return project_id
