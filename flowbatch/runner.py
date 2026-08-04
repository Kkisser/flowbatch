"""Прогон очереди: одна задача за раз, с паузами, ретраями и реакцией на ошибки."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from rich.console import Console

from .config import Config
from .flow_client import (
    ERR_MODERATION,
    ERR_QUOTA,
    ERR_SERVER,
    ERR_STALE_PICKER,
    ERR_THROTTLE,
    ERR_UNKNOWN,
    ERR_UNUSUAL,
    FlowClient,
    FlowError,
)
from .notify import Notifier
from .scrub import scrub
from .soften import DIALOGUE_ONLY_ATTEMPT, moderation_category
from .queue import STATUS_DRY_RUN, STATUS_FAILED, STATUS_OK, Job, RunLog, now_iso

# Ошибки, при которых очередь останавливается целиком. Только квота:
# с пустым счётом перегенерация — это спин без шансов. Всё остальное
# лечится повтором (по явному требованию: «любая ошибка — перегенерируй»).
STOP_QUEUE = {ERR_QUOTA}
# Ошибки, которые лечатся повтором с экспоненциальным бэкоффом.
# ERR_UNKNOWN здесь сознательно: таймауты ожидания и прочие разовые сбои
# чаще проходят со второго раза, чем повторяются.
RETRYABLE = {ERR_THROTTLE, ERR_SERVER, ERR_UNKNOWN}
# «Подозрительная активность» — тоже повтор, но с длинными паузами:
# Flow просит притормозить, а не сменить промпт.
RETRY_SLOW = {ERR_UNUSUAL}
# Сколько раз за одну задачу перезагружать вкладку из-за отставшего пикера.
# Одной перезагрузки хватает: она подтягивает всё, что успело сгенерироваться.
STALE_RELOAD_MAX = 2

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
        "Flow жаловался на подозрительную активность. Повторы с длинными паузами "
        "не помогли — если это повторяется на соседних задачах, дай аккаунту "
        "отдохнуть час-другой и подними паузы в config.yaml."
    ),
    ERR_MODERATION: "Промпт не прошёл модерацию. Задача помечена FAILED, очередь продолжается.",
    ERR_STALE_PICKER: (
        "Пикер Flow не показал нужный референс даже после перезагрузки вкладки. "
        "Проверь, что задача-источник действительно отработала в ЭТОМ проекте."
    ),
}


@dataclass
class RunOutcome:
    """Итог прогона очереди."""

    total: int = 0
    done: int = 0
    failed: int = 0
    skipped: int = 0
    stopped_reason: str | None = None
    # Машиночитаемый вид stopped_reason: kind ошибки из STOP_QUEUE или None.
    # Нужен панели: квота и «подозрительная активность» бьют по аккаунту
    # целиком, и по этому полю останавливаются ВСЕ прогоны, а не один.
    stop_kind: str | None = None
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
        softener: Any = None,
        arbiter: Any = None,
        pacer: Any = None,
    ) -> None:
        self.cfg = cfg
        self.client = client
        # Оба поля работают только в параллельном режиме и оба по умолчанию
        # пусты, чтобы однопоточный прогон остался ровно тем, что уже проверен.
        # arbiter — реестр «этот результат уже занят» (см. wait_for_new_media),
        # pacer — общий на все вкладки ритм пауз между задачами.
        self.arbiter = arbiter
        self.pacer = pacer
        self.notifier = notifier
        self.log = log
        self.console = console
        self.dry_run = dry_run
        self.max_retries = int(cfg.get("antiban.max_retries", 3))
        # Разрешает пути референсов в uuid и решает, надо ли заливать файл.
        self.resolver = resolver
        # Смягчитель промптов при отказе модерации (см. soften.py). None = выкл.
        self.softener = softener
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

    def run(
        self, jobs: list[Job] | None = None, take: Any = None, total: int | None = None
    ) -> RunOutcome:
        """Прогнать задачи. Возвращает сводку.

        take — необязательный источник задач вместо готового списка: в
        параллельном режиме воркеры тянут из общей очереди по одной, поэтому
        освободившаяся вкладка сразу берёт следующую строку, а не простаивает
        до конца чужого видео. take() отдаёт None, когда очередь пуста.
        """
        jobs = list(jobs or [])
        out = RunOutcome(total=total if total is not None else len(jobs))
        started = time.time()
        long_every = int(self.cfg.get("antiban.long_pause_every", 10))
        i = 0

        while True:
            if take is not None:
                job = take()
                if job is None:
                    break
            else:
                if i >= len(jobs):
                    break
                job = jobs[i]
            if self.stop_requested:
                out.skipped = (len(jobs) - i) if take is None else 0
                out.stopped_reason = "остановлено пользователем"
                self.console.print("[yellow]Остановка по запросу[/yellow]")
                break

            if i or take is not None:
                self._pause(i + 1, long_every)
                if self.stop_requested:
                    out.stopped_reason = "остановлено пользователем"
                    break

            self.console.rule(f"[bold]{i + 1}/{out.total}  {job.id}[/bold]  ({job.kind})")
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
                    out.stop_kind = exc.kind
                    self.console.print(f"[bold red]Очередь остановлена:[/bold red] {out.stopped_reason}")
                    out.skipped = max(0, len(jobs) - i - 1)
                    self.stop_requested = True
                    break
            i += 1

        out.elapsed_sec = time.time() - started
        return out

    def _pause(self, done_count: int, long_every: int) -> None:
        """Человеческий темп между задачами."""
        if self.dry_run:
            # Репетиция генераций не запускает — полные антибан-паузы здесь
            # только растягивали бы проверку очереди на десятки минут.
            time.sleep(2.0)
            return
        if self.pacer is not None:
            # Ритм общий на все вкладки: три воркера, каждый со своей паузой,
            # втроём давали бы втрое больше запросов в минуту — ровно то, за
            # что Flow и помечает активность подозрительной.
            self.pacer.wait_turn(lambda: self.stop_requested, self.console)
            return
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
        """Одна задача: ретраи на сеть/троттлинг + смягчение промпта на модерацию."""
        attempt = 0
        soften_used = 0
        soften_max = int(self.cfg.get("moderation.soften.attempts", 2))
        # Последняя «сценная» версия промпта. Нужна из-за попытки №3 (голые
        # реплики): если и она отклонена, попытка №4 должна переписывать сцену
        # дальше, а не смягчать три строчки диалога.
        scene_prompt = job.prompt
        # Исходник очереди. job.prompt по ходу лестницы подменяется, поэтому
        # для второго захода нужен отдельно сохранённый оригинал.
        original_prompt = job.prompt
        # Всё, что уже отклонялось (или сейчас в работе), — повторно такой
        # текст не запускаем: генерация с тем же промптом обречена и только
        # жжёт время и лимиты.
        tried = {job.prompt.strip()}
        reloads = 0
        # Заходов по лестнице: исчерпали ступени одним бэкендом — берём
        # следующий и идём заново от ИСХОДНОГО промпта.
        passes_max = max(1, int(self.cfg.get("moderation.soften.passes", 2)))
        passes = 1
        while True:
            attempt += 1
            try:
                self._attempt(job, soften_used=soften_used)
                if soften_used:
                    self.console.print(
                        f"  [green]прошло после смягчения промпта "
                        f"(попыток смягчения: {soften_used})[/green]"
                    )
                    self.notifier.send(
                        f"✅ {job.id}: прошло модерацию после смягчения промпта "
                        f"({soften_used} попыт.)"
                    )
                return
            except FlowError as exc:
                err = exc
            except Exception as exc:  # noqa: BLE001
                # Playwright бросает свои исключения (ETIMEDOUT и подобные).
                # Без этой ветки одна сетевая икота роняет всю очередь.
                err = _as_flow_error(exc)

            # «Мы заметили подозрительную активность» приходит тем же сканом
            # страницы, что и модерация, — но это транзиентный сбой, а не
            # претензия к промпту. Переклассифицируем до выбора реакции.
            if err.kind == ERR_MODERATION and moderation_category(
                self.cfg, err.detail or ""
            ) == "unusual":
                err = FlowError(
                    ERR_UNUSUAL,
                    "Flow пожаловался на подозрительную активность",
                    detail=err.detail,
                )

            # Пикер не увидел медиа, сгенерированное в этой же сессии: его
            # список грузится вместе со страницей и сам не обновляется.
            # Перезагружаем вкладку и проходим задачу заново — генерация при
            # этом не повторяется, повторяются только настройки и промпт.
            if err.kind == ERR_STALE_PICKER and reloads < STALE_RELOAD_MAX:
                reloads += 1
                self.console.print(
                    "  [yellow]список пикера отстал от библиотеки — перезагружаю "
                    f"вкладку ({reloads}/{STALE_RELOAD_MAX}) и повторяю задачу[/yellow]"
                )
                try:
                    self.client.reload_page()
                except Exception as exc:  # noqa: BLE001 — перезагрузка не должна ронять очередь
                    raise _as_flow_error(exc) from exc
                continue

            if err.kind in (RETRYABLE | RETRY_SLOW) and attempt <= self.max_retries:
                if err.kind in RETRY_SLOW:
                    # Просьба притормозить: паузы заметно длиннее обычных.
                    backoff = min(600, 90 * (2 ** (attempt - 1)))
                else:
                    backoff = min(300, 15 * (2 ** (attempt - 1)))
                self.console.print(
                    f"[yellow]{err}[/yellow] — перегенерирую, попытка "
                    f"{attempt}/{self.max_retries}, пауза {backoff}с"
                )
                # Пауза короткими шагами: «Стоп» из панели срабатывает сразу,
                # а не через десять минут бэкоффа.
                waited = 0.0
                while waited < backoff and not self.stop_requested:
                    step = min(0.5, backoff - waited)
                    time.sleep(step)
                    waited += step
                if self.stop_requested:
                    raise err
                continue

            # Модерация: переписываем промпт и пробуем ту же задачу заново.
            # Реакция на ЛЮБУЮ формулировку предупреждения одна и та же —
            # лестница переписываний; контекст ошибки влияет только на
            # инструкцию (policy / third_party).
            # Лестница исчерпана — пробуем ЕЩЁ РАЗ, но другой моделью.
            # Часто дело не в формулировках, а в переписывающей модели:
            # локальная gemma и облачный Gemini правят по-разному. Заход
            # начинается с исходного промпта — свежей модели нужна настоящая
            # сцена, а не текст, изжёванный девятью переписываниями.
            if (err.kind == ERR_MODERATION and self.softener
                    and soften_used >= soften_max and passes < passes_max):
                nxt = self.softener.next_backend()
                if nxt:
                    passes += 1
                    soften_used = 0
                    # Ветка ниже сразу перепишет исходник новым бэкендом, так
                    # что генерация с уже отклонённым текстом не повторится.
                    scene_prompt = original_prompt
                    self.console.print(
                        f"  [bold yellow]лестница {soften_max}/{soften_max} не помогла[/bold yellow] — "
                        f"заход {passes}/{passes_max} на другом бэкенде: {nxt}"
                    )
                    self.notifier.send(
                        f"🔁 {job.id}: {soften_max} ступеней не пробили модерацию.\n"
                        f"Заход {passes}/{passes_max} на другом бэкенде: {nxt}"
                    )

            if err.kind == ERR_MODERATION and self.softener and soften_used < soften_max:
                # Категория считается каждый раз заново: задача могла сначала
                # споткнуться о «наши правила», а после переписывания — о
                # «сторонних поставщиков контента».
                category = moderation_category(self.cfg, err.detail or "")
                cat_note = " [сходство с чужим контентом]" if category == "third_party" else ""
                new_prompt, what, soften_used, idle = self._soften_escalate(
                    scene_prompt, tried, soften_used, soften_max, category
                )
                # Ступени, вернувшие тот же текст, генерацию не запускали —
                # без этой оговорки «попытка 4/9» читается как «1-3 молча
                # провалились», хотя их просто не с чем было запускать.
                idle_note = (
                    f"\nСтупени {idle[0]}–{idle[-1]} вернули тот же текст — "
                    "генерация по ним не запускалась."
                    if len(idle) > 1 else
                    f"\nСтупень {idle[0]} вернула тот же текст — "
                    "генерация по ней не запускалась." if idle else ""
                )
                if new_prompt is not None:
                    if soften_used != DIALOGUE_ONLY_ATTEMPT:
                        scene_prompt = new_prompt
                    tried.add(new_prompt.strip())
                    self.console.print(
                        f"  [bold yellow]модерация отклонила «{job.id}»{cat_note}[/bold yellow] — "
                        f"переписываю промпт ({self.softener.name}, "
                        f"попытка {soften_used}/{soften_max}): {what}"
                    )
                    if err.detail:
                        self.console.print(f"  [dim]{err.detail[:300]}[/dim]")
                    self.notifier.send(
                        f"⚠️ {job.id}: модерация Flow отклонила промпт{cat_note}."
                        f"{idle_note}\n"
                        f"Переписываю ({self.softener.name}, "
                        f"ступень {soften_used}/{soften_max}): {what}"
                    )
                    job = replace(job, prompt=new_prompt)
                    continue
                self.console.print(
                    "  [yellow]лестница переписывания исчерпана — новый текст "
                    "получить не удалось[/yellow]"
                )

            if soften_used and err.kind == ERR_MODERATION:
                err = FlowError(
                    err.kind,
                    f"{err} (переписывали {passes} заход(а) по {soften_max} ступеней, "
                    f"последний бэкенд: {self.softener.name if self.softener else '—'} "
                    "— не помогло)",
                    detail=err.detail,
                )
            raise err

    def _soften_escalate(
        self,
        scene_prompt: str,
        tried: set[str],
        soften_used: int,
        soften_max: int,
        category: str,
    ) -> tuple[str | None, str, int, list[int]]:
        """Идти по лестнице переписываний, пока текст реально не изменится.

        Ключевое отличие от «одна попытка — один прогон»: если ступень вернула
        текст, который уже пробовали (модель решила «менять нечего»), генерацию
        с ним НЕ перезапускаем — она обречена. Вместо этого сразу шагаем на
        следующую ступень: инструкция агрессивнее, температура выше, на №3 —
        голые реплики. Ровно на этом сценарии старая логика «не изменил —
        сдаюсь» хоронила задачу после первой же попытки из девяти.

        Возвращает (новый текст | None, описание, использовано попыток,
        номера ступеней, которые вернули уже виденный текст).
        """
        idle: list[int] = []
        while soften_used < soften_max:
            soften_used += 1
            cand, what = self.softener.soften(scene_prompt, soften_used, category=category)
            if cand.strip() and cand.strip() not in tried:
                return cand, what, soften_used, idle
            idle.append(soften_used)
            self.console.print(
                f"  [dim]ступень {soften_used}/{soften_max}: текст не изменился — "
                f"пробую следующую[/dim]"
            )
        return None, "", soften_used, idle

    def _attempt(self, job: Job, soften_used: int = 0) -> None:
        """Один проход: настройки → промпт → референсы → запуск → ожидание → файл.

        soften_used — сколько раз промпт уже смягчали. Ненулевое значение
        означает, что job.prompt отличается от того, что лежит в очереди,
        поэтому в журнал пишется фактически использованный текст.
        """
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

        # 5. Снимки до запуска: медиа (база диффа) и фразы модерации (база
        #    ложных срабатываний — старые упавшие плитки не считаются).
        before = set(self.client.media_snapshot().keys())
        mod_base = int(self.client.moderation_state()["count"])
        self.console.print(f"  медиа в DOM до запуска: {len(before)}")

        # 6. Запуск.
        launch_ts = time.time()
        self.client.click_create()
        self.console.print("  «Создать» нажата, жду результат…")

        # 7. Ожидание нового URL.
        def tick(sec: int) -> None:
            self.console.print(f"    [dim]{sec}с…[/dim]", end="\r")

        item = self.client.wait_for_new_media(
            before, job.kind, on_tick=tick, moderation_baseline=mod_base,
            arbiter=self.arbiter,
        )
        self.client.raise_for_errors(launch_ts)

        # 8. Скачивание — со своим ретраем. Выключается в config.yaml
        #    (generation.download_results: false): результат остаётся в
        #    библиотеке Flow, а файл можно забрать позже командой fetch —
        #    в журнал для этого пишутся url (с uuid) и целевое имя.
        if not bool(self.cfg.get("generation.download_results", True)):
            elapsed = time.time() - t0
            self.console.print(
                f"  [green]готово[/green]: в библиотеке Flow "
                f"(uuid {item.name[:8]}…) за {elapsed:.0f}с — скачивание выключено"
            )
            self.log.write_result(
                job, STATUS_OK, started_at, url=item.url, file=None,
                out_stem=str(self.cfg.out_dir() / job.out_stem),
                project=self.project_id,
                soften_attempts=soften_used,
                prompt_used=job.prompt if soften_used else None,
            )
            self._notify_status(job, STATUS_OK)
            return

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
            soften_attempts=soften_used,
            prompt_used=job.prompt if soften_used else None,
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
