"""Прогон очереди: одна задача за раз, с паузами, ретраями и реакцией на ошибки."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rich.console import Console

from .config import Config
from .flow_client import (
    ERR_MODERATION,
    ERR_QUOTA,
    ERR_SERVER,
    ERR_THROTTLE,
    ERR_UNUSUAL,
    FlowClient,
    FlowError,
)
from .notify import Notifier
from .scrub import scrub
from .queue import STATUS_DRY_RUN, STATUS_FAILED, STATUS_OK, Job, RunLog, now_iso

# Ошибки, при которых очередь останавливается целиком.
STOP_QUEUE = {ERR_QUOTA, ERR_UNUSUAL}
# Ошибки, которые лечатся повтором с экспоненциальным бэкоффом.
RETRYABLE = {ERR_THROTTLE, ERR_SERVER}

# Признаки сетевого сбоя в тексте исключения Playwright — такие лечатся ретраем.
_NET_MARKERS = (
    "ETIMEDOUT", "ECONNRESET", "ECONNREFUSED", "ENOTFOUND", "EAI_AGAIN",
    "socket hang up", "net::", "Timeout", "connect ",
)


def _as_flow_error(exc: BaseException) -> FlowError:
    """Обернуть постороннее исключение в FlowError, вычистив секреты.

    Трассировки Playwright содержат полный call log с заголовком Cookie —
    scrub внутри FlowError не даёт токену дойти до лога или Telegram.
    """
    text = f"{type(exc).__name__}: {exc}"
    kind = ERR_SERVER if any(m in text for m in _NET_MARKERS) else ERR_UNKNOWN
    headline = scrub(text.splitlines()[0])[:200]
    return FlowError(kind, headline, detail=text[:2000])


_ADVICE = {
    ERR_QUOTA: "Кончились кредиты. Очередь остановлена — пополни и запусти заново, резюм подхватит.",
    ERR_UNUSUAL: (
        "Flow пометил активность как подозрительную. Очередь остановлена. "
        "Сделай паузу 1–6 часов, потом запусти заново с увеличенными паузами в config.yaml."
    ),
    ERR_MODERATION: "Промпт не прошёл модерацию. Задача помечена FAILED, очередь продолжается.",
}


@dataclass
class RunOutcome:
    """Итог прогона очереди."""

    total: int = 0
    done: int = 0
    failed: int = 0
    skipped: int = 0
    stopped_reason: str | None = None
    elapsed_sec: float = 0.0
    failures: list[tuple[str, str]] = field(default_factory=list)


class Runner:
    """Последовательный прогон задач по живой странице."""

    def __init__(
        self,
        cfg: Config,
        client: FlowClient,
        notifier: Notifier,
        log: RunLog,
        console: Console,
        dry_run: bool = False,
        resolver: Any = None,
        on_status: Any = None,
        project_id: str | None = None,
    ) -> None:
        self.cfg = cfg
        self.client = client
        self.notifier = notifier
        self.log = log
        self.console = console
        self.dry_run = dry_run
        self.max_retries = int(cfg.get("antiban.max_retries", 3))
        # Разрешает пути референсов в uuid и решает, надо ли заливать файл.
        self.resolver = resolver
        # Колбэк для внешнего журнала (например, записи статуса в Excel):
        # on_status(job, status, result_path, error).
        self.on_status = on_status
        self.project_id = project_id
        self.stop_requested = False

    def _notify_status(
        self, job: Job, status: str, result_path: str | None = None, error: str | None = None
    ) -> None:
        if not self.on_status:
            return
        try:
            self.on_status(job, status, result_path, error)
        except Exception as exc:  # noqa: BLE001 — внешний журнал не должен ронять прогон
            self.console.print(f"  [yellow]не удалось записать статус наружу: {exc}[/yellow]")

    # ------------------------------------------------------------------ цикл

    def run(self, jobs: list[Job]) -> RunOutcome:
        """Прогнать список задач. Возвращает сводку."""
        out = RunOutcome(total=len(jobs))
        started = time.time()
        long_every = int(self.cfg.get("antiban.long_pause_every", 10))

        for i, job in enumerate(jobs):
            if self.stop_requested:
                out.skipped = len(jobs) - i
                out.stopped_reason = "остановлено пользователем"
                self.console.print("[yellow]Остановка по запросу[/yellow]")
                break
            self.console.rule(f"[bold]{i + 1}/{len(jobs)}  {job.id}[/bold]  ({job.kind})")
            self._notify_status(job, "IN_PROGRESS")
            try:
                self._run_one(job)
                out.done += 1
            except FlowError as exc:
                out.failed += 1
                out.failures.append((job.id, str(exc)))
                self._report_failure(job, exc)
                if exc.kind in STOP_QUEUE:
                    out.stopped_reason = _ADVICE.get(exc.kind, str(exc))
                    self.console.print(f"[bold red]Очередь остановлена:[/bold red] {out.stopped_reason}")
                    out.skipped = len(jobs) - i - 1
                    break

            if i < len(jobs) - 1:
                self._pause(i + 1, long_every)

        out.elapsed_sec = time.time() - started
        return out

    def _pause(self, done_count: int, long_every: int) -> None:
        """Человеческий темп между задачами."""
        base = float(self.cfg.get("antiban.pause_between_jobs_sec", 30))
        jitter = float(self.cfg.get("antiban.pause_jitter_sec", 10))
        delay = max(1.0, base + random.uniform(-jitter, jitter))

        if long_every and done_count % long_every == 0:
            delay = float(self.cfg.get("antiban.long_pause_sec", 300))
            self.console.print(f"[dim]Длинная пауза {delay:.0f}с после {done_count} задач…[/dim]")
        else:
            self.console.print(f"[dim]Пауза {delay:.0f}с…[/dim]")

        # Спим короткими шагами, чтобы «Стоп» из веб-панели срабатывал сразу,
        # а не ждал конца пятиминутной паузы.
        waited = 0.0
        while waited < delay and not self.stop_requested:
            step = min(0.5, delay - waited)
            time.sleep(step)
            waited += step

    # ------------------------------------------------------------ одна задача

    def _run_one(self, job: Job) -> None:
        """Одна задача с ретраями на троттлинг, 5xx и сетевые сбои."""
        attempt = 0
        while True:
            attempt += 1
            try:
                self._attempt(job)
                return
            except FlowError as exc:
                err = exc
            except Exception as exc:  # noqa: BLE001
                # Playwright бросает свои исключения (ETIMEDOUT и подобные).
                # Без этой ветки одна сетевая икота роняет всю очередь.
                err = _as_flow_error(exc)

            if err.kind in RETRYABLE and attempt <= self.max_retries:
                backoff = min(300, 15 * (2 ** (attempt - 1)))
                self.console.print(
                    f"[yellow]{err}[/yellow] — попытка {attempt}/{self.max_retries}, "
                    f"бэкофф {backoff}с"
                )
                time.sleep(backoff)
                continue
            raise err

    def _attempt(self, job: Job) -> None:
        """Один проход: настройки → промпт → референсы → запуск → ожидание → файл."""
        started_at = now_iso()
        t0 = time.time()
        self.client.focus()

        # 0. Чистый старт: «Очистить запрос» снимает и промпт, и референсы
        #    предыдущей задачи. Иначе они утекут в следующую генерацию.
        self.client.close_add_dialog()
        if self.client.clear_request():
            self.console.print("  панель очищена от прошлой задачи")

        # 1. Настройки: тип, формат, количество, длительность.
        gs = self.client.ensure_settings(job.kind, duration=job.duration)
        self.console.print(f"  настройки: [cyan]{gs.raw}[/cyan]")

        # 2. Промпт с обязательной верификацией, что Slate его принял.
        self.client.set_prompt(job.prompt)
        self.console.print(f"  промпт: {len(job.prompt)} симв. — принят редактором")

        # 3. Референсы. Прежде чем цеплять свои, снимаем всё от прошлой задачи.
        if job.refs:
            if self.resolver is None:
                raise FlowError("unknown", "Нет резолвера референсов")
            before_up = self.resolver.uploads
            uuids = self.client.attach_refs(job.refs, resolver=self.resolver)
            uploaded = self.resolver.uploads - before_up
            self.console.print(
                f"  референсы: {len(uuids)} шт. "
                f"({uploaded} залито, {len(uuids) - uploaded} переиспользовано) — "
                + ", ".join(u[:8] for u in uuids)
            )

        # 4. Формат ещё раз, уже как жёсткий гейт перед запуском.
        ok, detail = self.client.assert_aspect()
        if not ok:
            raise FlowError("unknown", f"Проверка формата не прошла: {detail}")

        if self.dry_run:
            self.client.wait_ready()
            shot = self.client.screenshot(
                self.cfg.screenshots_dir() / f"dryrun_{job.id}.png"
            )
            self.console.print(f"  [yellow]--dry-run: «Создать» НЕ нажимаю[/yellow]")
            self.console.print(f"  скриншот: {shot}")
            self.log.write_result(
                job, STATUS_DRY_RUN, started_at, url=None, file=str(shot),
                project=self.project_id,
            )
            # Без этого статус в панели навсегда застревал бы на IN_PROGRESS.
            self._notify_status(job, STATUS_DRY_RUN, result_path=str(shot))
            return

        # 5. Снимок медиа до запуска — база для диффа.
        before = set(self.client.media_snapshot().keys())
        self.console.print(f"  медиа в DOM до запуска: {len(before)}")

        # 6. Запуск.
        launch_ts = time.time()
        self.client.click_create()
        self.console.print("  «Создать» нажата, жду результат…")

        # 7. Ожидание нового URL.
        def tick(sec: int) -> None:
            self.console.print(f"    [dim]{sec}с…[/dim]", end="\r")

        item = self.client.wait_for_new_media(before, job.kind, on_tick=tick)
        self.client.raise_for_errors(launch_ts)

        # 8. Скачивание — со своим ретраем.
        dest = self._download_with_retry(item, job)
        size = dest.stat().st_size
        if size == 0:
            raise FlowError("unknown", f"Файл {dest} пустой")

        elapsed = time.time() - t0
        self.console.print(
            f"  [green]готово[/green]: {dest} ({size / 1024:.0f} КБ) за {elapsed:.0f}с"
        )
        self.log.write_result(
            job, STATUS_OK, started_at, url=item.url, file=str(dest),
            project=self.project_id,
        )
        self._notify_status(job, STATUS_OK, result_path=str(dest))

    def _download_with_retry(self, item: Any, job: Job) -> Path:
        """Отдельный ретрай на скачивание.

        Важно, что он именно отдельный: результат уже сгенерирован и лежит в
        библиотеке, повторять надо только загрузку файла. Общий ретрай задачи
        погнал бы генерацию заново — на видео это лишние 12 бонусов за каждую
        сетевую икоту.
        """
        attempt = 0
        while True:
            attempt += 1
            try:
                return self.client.download(item, self.cfg.out_dir() / job.out_stem)
            except FlowError as exc:
                err = exc
            except Exception as exc:  # noqa: BLE001
                err = _as_flow_error(exc)

            if err.kind in RETRYABLE and attempt <= self.max_retries:
                backoff = min(120, 10 * (2 ** (attempt - 1)))
                self.console.print(
                    f"  [yellow]скачивание не удалось ({err})[/yellow] — "
                    f"повтор {attempt}/{self.max_retries} через {backoff}с "
                    f"[dim](генерацию НЕ повторяю)[/dim]"
                )
                time.sleep(backoff)
                continue
            raise err

    # ----------------------------------------------------------------- ошибки

    def _report_failure(self, job: Job, exc: FlowError) -> None:
        """Записать в лог, показать в консоли, уведомить в Telegram со скриншотом."""
        self.console.print(f"  [red]FAILED[/red]: {exc}")
        if exc.detail:
            self.console.print(f"  [dim]{exc.detail[:500]}[/dim]")

        shot: Path | None = None
        try:
            shot = self.client.screenshot(self.cfg.screenshots_dir() / f"error_{job.id}.png")
        except Exception:  # noqa: BLE001 — скриншот не должен ронять обработку ошибки
            pass

        self.log.write_result(
            job,
            STATUS_FAILED,
            now_iso(),
            error=str(exc),
            error_kind=exc.kind,
            file=str(shot) if shot else None,
            project=self.project_id,
        )
        self._notify_status(job, STATUS_FAILED, error=str(exc))

        advice = _ADVICE.get(exc.kind, "")
        text = (
            f"❌ <b>{job.id}</b> ({job.kind})\n"
            f"{exc}\n"
            f"{exc.detail[:300] if exc.detail else ''}\n"
            f"{advice}"
        ).strip()
        # Скриншот может захватить страницу целиком; access_token в кадр не попадает —
        # он живёт только в __NEXT_DATA__, а не в видимом DOM.
        if shot and self.notifier.enabled:
            self.notifier.send_photo(shot, caption=text[:1024])
        else:
            self.notifier.send(text)
