"""CLI flowbatch: doctor и run."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from .config import Config
from .flow_client import FlowClient, FlowClientError, FlowError
from .notify import Notifier
from pathlib import Path

from .promptfile import parse as parse_prompts
from .queue import RunLog, load_jobs, select_jobs
from .refs import RefCache, RefResolver
from .runner import Runner
from .sheet import ST_DONE, ST_ERROR, ST_IN_PROGRESS, SheetQueue, filter_rows

console = Console()

OK = "[green]✓[/green]"
BAD = "[red]✗[/red]"
WARN = "[yellow]![/yellow]"


# --------------------------------------------------------------------- doctor


def cmd_doctor(args: argparse.Namespace) -> int:
    """Ничего не генерит. Проверяет подключение, селекторы, настройки, Telegram."""
    cfg = Config.load(args.config)
    console.print(Panel.fit("flowbatch doctor", style="bold cyan"))

    problems: list[str] = []

    client = FlowClient(cfg)
    try:
        client.connect()
    except FlowClientError as exc:
        console.print(f"{BAD} [bold]CDP / вкладка[/bold]")
        console.print(Panel(escape(str(exc)), border_style="red", title="Что делать"))
        return 2

    console.print(f"{OK} [bold]CDP[/bold]: подключение к {cfg.get('cdp.endpoint')} установлено")
    console.print(f"{OK} [bold]Вкладка[/bold]: {client.page.url}")

    # На списке проектов рабочих селекторов нет вообще — проверять нечего.
    if client.current_project_id() is None:
        client.close()
        console.print(
            Panel(
                "Открыт список проектов, а не проект.\n"
                "Открой проект руками либо создай новый:\n"
                "  python -m flowbatch.cli newproject --name \"Название\"",
                title="[bold yellow]Проект не открыт[/bold yellow]",
                border_style="yellow",
            )
        )
        return 1
    console.print(f"{OK} [bold]Проект[/bold]: {client.project_name()!r} ({client.current_project_id()})")
    flow_pages = client.all_flow_pages
    if len(flow_pages) > 1:
        console.print(f"  {WARN} подходящих вкладок несколько ({len(flow_pages)}), взята с /flow/project/:")
        for u in flow_pages:
            console.print(f"      {u}")
    client.focus()

    # --- сессия ---
    si = client.session_info()
    if si.has_session and si.has_access_token:
        who = si.email_masked or "аккаунт не раскрыт в pageProps"
        exp = f", сессия до {si.expires}" if si.expires else ""
        console.print(f"{OK} [bold]Логин[/bold]: сессия есть ({who}{exp})")
    elif si.next_data_found:
        console.print(f"{BAD} [bold]Логин[/bold]: __NEXT_DATA__ есть, но сессии в нём нет — не залогинен")
        problems.append("нет активной сессии Google на вкладке")
    else:
        console.print(f"{WARN} [bold]Логин[/bold]: __NEXT_DATA__ не найден (страница ещё грузится?)")
    console.print("  [dim]access_token не читается в Python, не логируется и не сохраняется[/dim]")

    # --- селекторы ---
    checks = client.check_selectors()
    table = Table(title="Селекторы", header_style="bold")
    table.add_column("", width=2)
    table.add_column("Что")
    table.add_column("Селектор", overflow="fold")
    table.add_column("Найдено", justify="right")
    for c in checks:
        mark = OK if c.found else (BAD if c.required else WARN)
        table.add_row(mark, c.label, escape(c.selector), str(c.count))
    console.print(table)

    missing_required = [c for c in checks if c.required and not c.found]
    for c in (c for c in checks if not c.required and not c.found):
        console.print(
            f"  {WARN} [bold]{c.label}[/bold] — 0 шт. Нормально для пустого проекта: "
            "элемент появится, как только в нём будет медиа."
        )

    # --- настройки генерации ---
    gs = client.read_gen_settings()
    if gs is None:
        console.print(f"{BAD} [bold]Настройки генерации[/bold]: триггер настроек не найден")
        problems.append("не прочитать формат")
    else:
        required = cfg.get("generation.required_aspect", "crop_9_16")
        want_batch = int(cfg.get("generation.batch", 1))
        aspect_ok = gs.aspect == required
        batch_ok = gs.batch == want_batch
        mark = OK if (aspect_ok and batch_ok) else BAD
        console.print(f"{mark} [bold]Настройки генерации[/bold]: {escape(repr(gs.raw))}")
        console.print(f"      формат: {gs.aspect}  (требуется {required})")
        console.print(f"      количество: x{gs.batch}  (требуется x{want_batch})")
        if gs.duration:
            console.print(f"      длительность: {gs.duration}s")
        if not aspect_ok:
            problems.append(f"формат {gs.aspect} вместо {required}")
        if not batch_ok:
            problems.append(f"количество x{gs.batch} вместо x{want_batch}")
        console.print("      [dim]run выставит это сам перед каждой задачей[/dim]")

    # --- прикреплённые референсы ---
    refs = client.attached_refs()
    if refs:
        console.print(f"{WARN} [bold]Референсы[/bold]: в панели висят {len(refs)} шт. — run их снимет перед стартом")
    else:
        console.print(f"{OK} [bold]Референсы[/bold]: панель чистая")

    # --- Telegram ---
    notifier = Notifier()
    tg_ok, tg_msg = notifier.check()
    if tg_ok:
        console.print(f"{OK} [bold]Telegram[/bold]: {tg_msg}")
    elif not notifier.enabled:
        console.print(f"{WARN} [bold]Telegram[/bold]: {tg_msg} — прогон это не блокирует")
    else:
        console.print(f"{BAD} [bold]Telegram[/bold]: {tg_msg}")
        problems.append("Telegram настроен, но не отвечает")

    console.print(
        f"{OK} [bold]Анти-бан[/bold]: пауза {cfg.get('antiban.pause_between_jobs_sec')}с "
        f"±{cfg.get('antiban.pause_jitter_sec')}с, параллельность {cfg.get('antiban.concurrency')} "
        f"(потолок 3), длинная пауза {cfg.get('antiban.long_pause_sec')}с каждые "
        f"{cfg.get('antiban.long_pause_every')} задач"
    )

    # --- разметка для ненайденных селекторов ---
    if missing_required:
        console.print()
        console.print(
            Panel(
                "Ниже — актуальная разметка мест, где селектор не нашёлся.\n"
                "Замену «по аналогии» не выдумываю: посмотри и скажи, какой селектор верный.",
                title="[bold red]Селекторы не найдены — работа остановлена[/bold red]",
                border_style="red",
            )
        )
        for c in missing_required:
            console.print(f"\n[bold]{c.label}[/bold]  ({escape(c.selector)})")
            console.print(Panel(escape(client.dump_markup(c.key)), border_style="yellow"))

    client.close()

    console.print()
    if missing_required or problems:
        lines = [f"• {p}" for p in problems]
        lines += [f"• не найден: {c.label}" for c in missing_required]
        console.print(Panel("\n".join(lines), title="[bold red]doctor: есть проблемы[/bold red]", border_style="red"))
        return 1

    console.print(Panel("doctor: всё зелёное", border_style="green"))
    return 0


# ----------------------------------------------------------------- newproject


def cmd_newproject(args: argparse.Namespace) -> int:
    """Создать новый пустой проект Flow и остаться в нём.

    Единственная команда, которая навигирует по приложению. run этого не делает,
    если не передан --new-project.
    """
    cfg = Config.load(args.config)
    client = FlowClient(cfg)
    try:
        client.connect()
    except FlowClientError as exc:
        console.print(Panel(escape(str(exc)), border_style="red", title="Что делать"))
        return 2

    client.focus()
    was = client.current_project_id()
    console.print(f"Сейчас открыто: {client.project_name()!r} ({was or 'список проектов'})")

    try:
        pid = client.create_project(args.name)
    finally:
        pass

    console.print(f"{OK} [bold]Создан проект[/bold] {pid}")
    console.print(f"   имя: {client.project_name()!r}")
    console.print(f"   URL: {client.page.url}")
    gs = client.read_gen_settings()
    if gs:
        console.print(f"   настройки по умолчанию: {escape(repr(gs.raw))}")
        console.print("   [dim]run выставит нужные сам перед каждой задачей[/dim]")
    client.close()
    return 0


# ------------------------------------------------------------------------ run


def cmd_run(args: argparse.Namespace) -> int:
    """Прогнать очередь задач."""
    cfg = Config.load(args.config)

    conc = int(cfg.get("antiban.concurrency", 1))
    if conc > 1:
        console.print(
            Panel(
                f"antiban.concurrency={conc}, но реализован только последовательный прогон.\n"
                "В одной вкладке одно поле промпта: параллельный запуск делает неоднозначным,\n"
                "какой из появившихся результатов какой задаче принадлежит.\n"
                "Поставь concurrency: 1 либо скажи — сделаю честную параллельность.",
                title="[bold red]Не поддерживается[/bold red]",
                border_style="red",
            )
        )
        return 2

    if args.sheet and args.prompts:
        console.print(Panel("--sheet и --prompts взаимоисключающие", border_style="red"))
        return 2

    sheet: SheetQueue | None = None
    sheet_by_id: dict[str, Any] = {}
    queue_project: str | None = args.project or None
    try:
        if args.prompts:
            text = Path(args.prompts).read_text(encoding="utf-8-sig")
            jobs, errors, meta = parse_prompts(
                text, out_dir=cfg.out_dir(), products_dir=cfg.products_dir()
            )
            queue_project = queue_project or meta.get("project")
            if errors:
                console.print(
                    Panel(
                        escape("\n".join(f"• {e}" for e in errors)),
                        title="[bold red]Ошибки разбора промптов[/bold red]",
                        border_style="red",
                    )
                )
                return 2
            if args.kind:
                jobs = [j for j in jobs if j.kind == args.kind]
            jobs = select_jobs(jobs, only=args.only, from_id=args.from_id, limit=args.limit)
        elif args.sheet:
            sheet = SheetQueue(args.sheet)
            rows = sheet.load()
            rows = filter_rows(
                rows,
                only=args.only,
                from_id=args.from_id,
                limit=args.limit,
                batch=args.batch,
                kind=args.kind,
            )
            sheet_by_id = {r.job.id: r for r in rows}
            jobs = [r.job for r in rows]
            queue_project = queue_project or sheet.project_name
        else:
            jobs = load_jobs(args.jobs)
            if args.kind:
                jobs = [j for j in jobs if j.kind == args.kind]
            jobs = select_jobs(jobs, only=args.only, from_id=args.from_id, limit=args.limit)
    except (ValueError, FileNotFoundError, KeyError) as exc:
        console.print(Panel(escape(str(exc)), title="[bold red]Очередь[/bold red]", border_style="red"))
        return 2

    log = RunLog(cfg.runs_log())
    notifier = Notifier()

    # В новом проекте ничего ещё не сгенерировано, поэтому резюм по старому
    # логу только мешал бы: он пропустил бы задачи, которых здесь нет.
    if args.new_project and not args.no_resume:
        args.no_resume = True
        console.print("[dim]--new-project: резюм отключён, проект пустой[/dim]")

    # У Excel-очереди свой резюм: load() берёт только строки TODO, а DONE
    # отсеиваются на чтении. Второй механизм поверх него только мешал бы.
    if args.sheet and not args.no_resume:
        args.no_resume = True
        console.print("[dim]--sheet: резюм идёт по колонке 03_STATUS, а не по runs.jsonl[/dim]")

    # Резюм: пропускаем то, что уже успешно сделано по логу. Если очередь
    # привязана к проекту, фильтр применяется ПОСЛЕ открытия проекта — id
    # проекта известен только тогда, а «сделано в другом проекте» не считается.
    resume_after_connect = bool(queue_project)
    if not args.no_resume and not args.dry_run and not resume_after_connect:
        done = log.completed_ids()
        before = len(jobs)
        skipped_resume = [j.id for j in jobs if j.id in done]
        jobs = [j for j in jobs if j.id not in done]
        if skipped_resume:
            console.print(
                f"[cyan]Резюм:[/cyan] пропускаю {before - len(jobs)} уже выполненных: "
                f"{', '.join(skipped_resume)}"
            )

    if not jobs:
        console.print("[yellow]Нечего делать: все задачи уже выполнены (или отфильтрованы).[/yellow]")
        return 0

    mode = "[yellow]DRY-RUN[/yellow] (кнопка «Создать» не нажимается)" if args.dry_run else "боевой прогон"
    console.print(Panel.fit(f"flowbatch run — {mode}\nзадач: {len(jobs)}", style="bold cyan"))

    client = FlowClient(cfg)
    try:
        client.connect()
    except FlowClientError as exc:
        console.print(Panel(escape(str(exc)), title="[bold red]Ошибка[/bold red]", border_style="red"))
        return 2

    if args.new_project:
        try:
            pid = client.create_project(args.project_name)
        except Exception as exc:  # noqa: BLE001
            client.close()
            console.print(Panel(escape(str(exc)), title="[bold red]Новый проект[/bold red]", border_style="red"))
            return 2
        console.print(f"{OK} создан проект {pid} — {client.project_name()!r}")
    elif queue_project:
        try:
            how = client.ensure_project(queue_project)
        except Exception as exc:  # noqa: BLE001
            client.close()
            console.print(Panel(escape(str(exc)), title="[bold red]Проект очереди[/bold red]", border_style="red"))
            return 2
        verb = {"current": "уже открыт", "opened": "открыт", "created": "создан"}[how]
        console.print(f"{OK} проект {queue_project!r} {verb} ({client.current_project_id()})")
        if resume_after_connect and not args.no_resume and not args.dry_run:
            done = log.completed_ids(project=client.current_project_id())
            before = len(jobs)
            jobs = [j for j in jobs if j.id not in done]
            if before != len(jobs):
                console.print(f"[cyan]Резюм:[/cyan] в этом проекте уже сделано {before - len(jobs)}")
            if not jobs:
                client.close()
                console.print("[yellow]Все задачи очереди уже выполнены в этом проекте.[/yellow]")
                return 0
    elif client.current_project_id() is None:
        client.close()
        console.print(
            Panel(
                "Открыт список проектов, а не проект. Открой проект руками,\n"
                "либо добавь --new-project, чтобы создать новый.",
                title="[bold red]Проект не открыт[/bold red]",
                border_style="red",
            )
        )
        return 2

    project_id = client.current_project_id() or ""
    resolver = RefResolver(
        client,
        log,
        RefCache(cfg.runs_log().parent / ".flowbatch_refcache.json"),
        project_id,
        on_upload=lambda p: console.print(f"  [dim]заливаю в библиотеку: {p.name}[/dim]"),
    )

    # Статусы в книгу пишем только в боевом прогоне: dry-run не должен
    # трогать STATUS, иначе строка перестанет быть TODO и потеряется.
    on_status = None
    if sheet is not None and not args.dry_run:
        st_map = {"ok": ST_DONE, "failed": ST_ERROR, "IN_PROGRESS": ST_IN_PROGRESS}

        def on_status(job: Any, status: str, result_path: str | None = None, error: str | None = None) -> None:
            row = sheet_by_id.get(job.id)
            if row is None:
                return
            sheet.write_result(
                row,
                st_map.get(status, status),
                result_path=result_path,
                bump_attempts=(status == "failed"),
                note=error,
            )

    runner = Runner(
        cfg, client, notifier, log, console,
        dry_run=args.dry_run,
        resolver=resolver,
        on_status=on_status,
        project_id=project_id,
    )
    try:
        outcome = runner.run(jobs)
    finally:
        client.close()

    if resolver.uploads or resolver.reused:
        console.print(
            f"[dim]референсы: залито {resolver.uploads}, "
            f"переиспользовано без загрузки {resolver.reused}[/dim]"
        )

    # --- финальный отчёт ---
    console.print()
    mins = outcome.elapsed_sec / 60
    summary = (
        f"Сделано {outcome.done} из {outcome.total}"
        f"{f', провалено {outcome.failed}' if outcome.failed else ''}"
        f"{f', пропущено {outcome.skipped}' if outcome.skipped else ''}"
        f" за {mins:.1f} мин"
    )
    style = "green" if outcome.failed == 0 and not outcome.stopped_reason else "yellow"
    body = summary
    if outcome.failures:
        body += "\n\nПровалы:\n" + "\n".join(f"  • {i}: {e}" for i, e in outcome.failures)
    if outcome.stopped_reason:
        body += f"\n\nОстановка: {outcome.stopped_reason}"
        style = "red"
    console.print(Panel(escape(body), title="Итог", border_style=style))

    if not args.dry_run:
        notifier.send(
            f"{'✅' if style == 'green' else '⚠️'} flowbatch: {summary}"
            + (f"\n{outcome.stopped_reason}" if outcome.stopped_reason else "")
        )

    if outcome.stopped_reason:
        return 3
    return 1 if outcome.failed else 0


# -------------------------------------------------------------------- library


def cmd_library(args: argparse.Namespace) -> int:
    """Показать элементы библиотеки Flow по имени — чтобы писать @lib не вслепую."""
    cfg = Config.load(args.config)
    client = FlowClient(cfg)
    try:
        client.connect()
    except FlowClientError as exc:
        console.print(Panel(escape(str(exc)), border_style="red", title="Что делать"))
        return 2

    client.focus()
    lib_project = args.library_project
    try:
        if client.current_project_id() is None:
            # На списке проектов пикера нет физически. С --project честно
            # открываем этот проект; без него — просим открыть хоть какой-то.
            if not lib_project:
                console.print("[red]Открыт список проектов — открой проект или укажи --project[/red]")
                return 2
            client.ensure_project(lib_project)
            lib_project = None  # теперь это текущий проект

        where = lib_project or client.project_name()
        console.print(f"Библиотека проекта [bold]{where}[/bold]"
                      + (f", поиск {args.query!r}" if args.query else "") + ":")
        items = client.list_library(
            args.query or "",
            project=lib_project,
            only_images=not args.all_types,
        )
    except (FlowError, FlowClientError) as exc:
        console.print(Panel(escape(str(exc)), title="[bold red]Библиотека[/bold red]", border_style="red"))
        return 1
    finally:
        try:
            client.close_add_dialog()
        except Exception:  # noqa: BLE001 — диалога могло и не быть
            pass
        client.close()

    if not items:
        console.print("  [yellow]ничего не найдено[/yellow]")
        return 1

    table = Table(header_style="bold")
    table.add_column("Имя")
    table.add_column("Тип")
    table.add_column("uuid", style="dim")
    for it in sorted(items, key=lambda x: x["name"].lower()):
        table.add_row(escape(it["name"]), it["type"], it["uuid"][:8])
    console.print(table)
    console.print(f"[dim]всего: {len(items)}. В промпте: @lib <имя>"
                  f"{'  или  @lib ' + where + ' :: <имя>' if args.library_project else ''}[/dim]")
    return 0


# ------------------------------------------------------------------- products


def cmd_products(args: argparse.Namespace) -> int:
    """Показать базу продуктов (папка products/). Без браузера."""
    from .promptfile import PRODUCT_IMAGE_EXTS

    cfg = Config.load(args.config)
    base = cfg.products_dir()
    if not base.is_dir():
        console.print(
            f"[yellow]Базы продуктов ещё нет.[/yellow] Создай папку {base}\\<продукт>\\ "
            "и положи туда фото — в промпте это будет @product <продукт>."
        )
        return 1

    table = Table(header_style="bold")
    table.add_column("Продукт")
    table.add_column("Фото", justify="right")
    table.add_column("Файлы", overflow="fold", style="dim")
    total = 0
    for d in sorted(base.iterdir(), key=lambda p: p.name.lower()):
        if not d.is_dir():
            continue
        files = sorted(
            f.name for f in d.iterdir()
            if f.is_file() and f.suffix.lower() in PRODUCT_IMAGE_EXTS
        )
        total += 1
        table.add_row(d.name, str(len(files)), ", ".join(files) or "[red]пусто[/red]")
    if not total:
        console.print(f"[yellow]{base} пуста — создай подпапку с фото продукта.[/yellow]")
        return 1
    console.print(table)
    console.print("[dim]в промпте: @product <продукт>  или  @product <продукт>/<файл>[/dim]")
    return 0


# ------------------------------------------------------------------------- ui


def cmd_ui(args: argparse.Namespace) -> int:
    """Поднять локальную веб-панель."""
    from .web import serve

    cfg = Config.load(args.config)
    serve(cfg, args.source, port=args.port, open_browser=not args.no_browser)
    return 0


# --------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="flowbatch",
        description="Пакетная генерация в Google Flow поверх живой сессии браузера.",
    )
    p.add_argument("--config", default="config.yaml", help="путь к config.yaml")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("doctor", help="проверить подключение, селекторы, настройки, Telegram")
    d.set_defaults(func=cmd_doctor)

    pr = sub.add_parser("products", help="база продуктов на диске (для @product)")
    pr.set_defaults(func=cmd_products)

    lb = sub.add_parser("library", help="список элементов библиотеки Flow (для @lib)")
    lb.add_argument("query", nargs="?", default="", help="подстрока имени")
    lb.add_argument("--project", dest="library_project", help="смотреть библиотеку другого проекта")
    lb.add_argument("--all-types", action="store_true", help="не только картинки")
    lb.set_defaults(func=cmd_library)

    w = sub.add_parser("ui", help="локальная веб-панель управления очередью")
    w.add_argument("--source", default="jobs.yaml", help="очередь по умолчанию: .xlsx или jobs.yaml")
    w.add_argument("--port", type=int, default=8765, help="порт панели (только 127.0.0.1)")
    w.add_argument("--no-browser", action="store_true", help="не открывать браузер автоматически")
    w.set_defaults(func=cmd_ui)

    n = sub.add_parser("newproject", help="создать новый пустой проект Flow и остаться в нём")
    n.add_argument("--name", help="имя проекта; без него Flow ставит дату и время")
    n.set_defaults(func=cmd_newproject)

    r = sub.add_parser("run", help="прогнать очередь задач")
    r.add_argument("--jobs", default="jobs.yaml", help="путь к jobs.yaml")
    r.add_argument("--sheet", help="взять очередь из .xlsx (листы IMG_QUEUE/VID_QUEUE) вместо jobs.yaml")
    r.add_argument("--prompts", help="взять очередь из .txt с @-директивами (@ref/@use/@duration/@out)")
    r.add_argument("--batch", help="только строки с этим значением 04_BATCH (для --sheet)")
    r.add_argument("--kind", choices=["image", "video"], help="только задачи этого типа")
    r.add_argument("--dry-run", action="store_true", help="всё кроме клика «Создать»")
    r.add_argument("--only", metavar="ID", help="выполнить только эту задачу")
    r.add_argument("--from", dest="from_id", metavar="ID", help="начать с этой задачи")
    r.add_argument("--limit", type=int, metavar="N", help="взять не больше N задач")
    r.add_argument("--no-resume", action="store_true", help="не пропускать уже выполненные")
    r.add_argument(
        "--new-project",
        action="store_true",
        help="создать новый пустой проект и прогнать очередь в нём (резюм отключается)",
    )
    r.add_argument("--project-name", help="имя нового проекта для --new-project")
    r.add_argument(
        "--project",
        help="открыть (или создать) проект Flow с этим именем перед прогоном; "
        "перебивает @project из файла промптов и PROJECT_NAME из xlsx",
    )
    r.set_defaults(func=cmd_run)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except FlowClientError as exc:
        console.print(Panel(escape(str(exc)), title="[bold red]Ошибка[/bold red]", border_style="red"))
        return 2
    except KeyboardInterrupt:
        console.print("\n[yellow]Прервано пользователем[/yellow]")
        return 130


if __name__ == "__main__":
    sys.exit(main())
