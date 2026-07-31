"""Работа с живой страницей Google Flow через CDP.

Принципиально: свой браузер НЕ поднимаем, headless не используем, логин Google
не автоматизируем. Подключаемся к уже запущенному вручную Chromium-браузеру
(Edge или Chrome) и работаем в уже открытой залогиненной вкладке.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal

from playwright.sync_api import Browser, Page, Playwright, expect, sync_playwright

from .config import Config
from .scrub import scrub

Kind = Literal["image", "video"]

# Текст триггера настроек:
#   изображение: '🍌 Nano Banana 2\ncrop_9_16\nx1'
#   видео:       'Видео · 8s\ncrop_9_16\nx1'   (имени модели там нет)
_ASPECT_RE = re.compile(r"crop_[a-z0-9_]+")
_BATCH_RE = re.compile(r"\bx(\d+)\b")
_DURATION_RE = re.compile(r"\b(\d+)s\b")
_MEDIA_NAME_RE = re.compile(r"[?&]name=([^&]+)")

# Коды ошибок Flow, приходящие в теле ответов tRPC.
ERR_MODERATION = "moderation"
ERR_QUOTA = "quota"
ERR_UNUSUAL = "unusual_activity"
ERR_THROTTLE = "throttle"
ERR_SERVER = "server"
ERR_UNKNOWN = "unknown"

_CODE_MAP = {
    "PUBLIC_ERROR_UNSAFE_GENERATION": ERR_MODERATION,
    "USER_QUOTA_REACHED": ERR_QUOTA,
    "PUBLIC_ERROR_UNUSUAL_ACTIVITY": ERR_UNUSUAL,
}

_EXT_BY_CTYPE = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
}


class FlowClientError(RuntimeError):
    """Ошибка подключения/поиска вкладки — не ошибка генерации."""


class FlowError(RuntimeError):
    """Ошибка со стороны Flow. kind определяет реакцию очереди.

    Текст и детали чистятся от секретов прямо в конструкторе — так ни один путь
    (консоль, runs.jsonl, Telegram) не может утечь токеном по недосмотру.
    """

    def __init__(self, kind: str, message: str, detail: str = "") -> None:
        message = scrub(message)
        super().__init__(message)
        self.kind = kind
        self.detail = scrub(detail)


def classify_trpc_error(status: int, body: str) -> str:
    """Определить тип ошибки Flow по статусу и телу ответа tRPC."""
    for code, kind in _CODE_MAP.items():
        if code in body:
            return kind
    if status == 429:
        return ERR_THROTTLE
    if status >= 500:
        return ERR_SERVER
    return ERR_UNKNOWN


@dataclass
class SelectorCheck:
    """Результат проверки одного селектора."""

    key: str
    label: str
    selector: str
    count: int
    required: bool = True

    @property
    def found(self) -> bool:
        return self.count > 0


@dataclass
class GenSettings:
    """Настройки генерации, прочитанные из текста триггера настроек."""

    raw: str
    label: str | None = None
    aspect: str | None = None
    batch: int | None = None
    duration: int | None = None

    @classmethod
    def parse(cls, raw: str) -> "GenSettings":
        flat = " ".join(raw.split())
        aspect_m = _ASPECT_RE.search(flat)
        batch_m = _BATCH_RE.search(flat)
        dur_m = _DURATION_RE.search(flat)
        label = flat
        for m in (aspect_m, batch_m, dur_m):
            if m:
                label = label.replace(m.group(0), "")
        return cls(
            raw=flat,
            label=" ".join(label.replace("·", " ").split()) or None,
            aspect=aspect_m.group(0) if aspect_m else None,
            batch=int(batch_m.group(1)) if batch_m else None,
            duration=int(dur_m.group(1)) if dur_m else None,
        )


@dataclass
class SessionInfo:
    """Диагностика сессии. Сам access_token наружу НИКОГДА не отдаём."""

    next_data_found: bool = False
    has_session: bool = False
    has_access_token: bool = False
    email_masked: str | None = None
    expires: str | None = None


@dataclass
class MediaItem:
    """Элемент медиа-библиотеки, найденный в DOM."""

    name: str
    url: str
    tag: str  # img | video


class FlowClient:
    """Обёртка над вкладкой Flow. Используется как контекстный менеджер."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._page: Page | None = None
        self.trpc_errors: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ setup

    def __enter__(self) -> "FlowClient":
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def connect(self) -> Page:
        """Подключиться к CDP и найти открытую вкладку Flow."""
        endpoint = self.cfg.get("cdp.endpoint", "http://localhost:9222")
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.connect_over_cdp(endpoint)
        except Exception as exc:  # noqa: BLE001
            self.close()
            raise FlowClientError(
                f"Не удалось подключиться к CDP на {endpoint}.\n"
                "Запусти браузер вручную с отладочным портом и НЕдефолтным профилем:\n"
                '  msedge.exe --remote-debugging-port=9222 --user-data-dir="C:\\edge-flow-profile"\n'
                f"Исходная ошибка: {type(exc).__name__}: {exc}"
            ) from exc

        self._page = self._pick_page()
        self._arm_network_listener(self._page)
        return self._page

    def _pick_page(self) -> Page:
        """Выбрать вкладку Flow среди уже открытых. Новых вкладок не создаём."""
        assert self._browser is not None
        match = self.cfg.get("cdp.page_url_match", "labs.google/fx")
        all_pages: list[Page] = []
        for ctx in self._browser.contexts:
            all_pages.extend(ctx.pages)

        if not all_pages:
            raise FlowClientError("В подключённом браузере нет ни одной вкладки.")

        candidates = [p for p in all_pages if match in (p.url or "")]
        if not candidates:
            opened = "\n".join(f"  - {p.url}" for p in all_pages[:20])
            raise FlowClientError(
                f"Среди открытых вкладок нет ни одной с '{match}'.\n"
                f"Открой проект Flow в этом же браузере. Сейчас открыто:\n{opened}"
            )

        for p in candidates:
            if "/flow/project/" in (p.url or ""):
                return p
        return candidates[0]

    @property
    def page(self) -> Page:
        if self._page is None:
            raise FlowClientError("Нет подключения — сначала вызови connect().")
        return self._page

    @property
    def all_flow_pages(self) -> list[str]:
        if self._browser is None:
            return []
        match = self.cfg.get("cdp.page_url_match", "labs.google/fx")
        urls: list[str] = []
        for ctx in self._browser.contexts:
            urls.extend(p.url for p in ctx.pages if match in (p.url or ""))
        return urls

    def close(self) -> None:
        """Отключиться от CDP. Браузер пользователя НЕ закрываем."""
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._browser = None
            self._page = None
            if self._pw is not None:
                try:
                    self._pw.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._pw = None

    def focus(self) -> None:
        """Вывести вкладку на передний план: фоновые вкладки троттлятся."""
        try:
            self.page.bring_to_front()
        except Exception:  # noqa: BLE001
            pass

    # ---------------------------------------------------------------- проекты

    def current_project_id(self) -> str | None:
        """id открытого проекта из URL, либо None если мы на списке проектов."""
        m = re.search(r"/flow/project/([0-9a-fA-F-]{8,})", self.page.url or "")
        return m.group(1) if m else None

    def project_name(self) -> str | None:
        """Имя проекта из шапки."""
        loc = self.page.locator(self.cfg.selectors["project_title"])
        if loc.count() == 0:
            return None
        return loc.first.input_value()

    def goto_projects_list(self, timeout_ms: int = 30_000) -> None:
        """Уйти с проекта на список проектов кнопкой «Назад».

        Навигация — единственное место, где скрипт её делает, и она включается
        только явной командой newproject / флагом --new-project.
        """
        create_label = self.cfg.locale.get("create_project", "Создать проект")
        if self.current_project_id() is not None:
            back_label = self.cfg.locale.get("back_button", "Назад")
            back = self.page.locator("button").filter(has_text=back_label)
            if back.count() == 0:
                raise FlowError(ERR_UNKNOWN, f"Кнопка «{back_label}» не найдена")
            back.first.click()

        # Список рендерится на клиенте: сразу после перехода кнопок ещё нет.
        self.page.locator("button").filter(has_text=create_label).first.wait_for(
            state="visible", timeout=timeout_ms
        )

    def create_project(self, name: str | None = None, timeout_ms: int = 60_000) -> str:
        """Создать новый проект и вернуть его id.

        Диалога нет: Flow создаёт проект сразу и переходит в него. Имя по
        умолчанию — дата и время создания, поэтому переименование опционально.
        """
        create_label = self.cfg.locale.get("create_project", "Создать проект")
        self.goto_projects_list()
        was = self.current_project_id()

        btn = self.page.locator("button").filter(has_text=create_label)
        if btn.count() == 0:
            raise FlowError(ERR_UNKNOWN, f"Кнопка «{create_label}» не найдена на списке проектов")
        started = time.time()
        btn.first.click()

        # Ждём перехода в НОВЫЙ проект.
        while time.time() - started < timeout_ms / 1000:
            self.page.wait_for_timeout(500)
            pid = self.current_project_id()
            if pid and pid != was:
                break
            self.raise_for_errors(started)
        else:
            raise FlowError(ERR_UNKNOWN, f"Проект не создался за {timeout_ms // 1000}с")

        # Дожидаемся, пока отрисуется рабочая область проекта.
        self.page.wait_for_selector(self.cfg.selectors["prompt_editor"], timeout=timeout_ms)
        self.page.wait_for_timeout(1000)

        if name:
            self.rename_project(name)
        return self.current_project_id() or ""

    def rename_project(self, name: str) -> str:
        """Переименовать текущий проект и проверить, что имя применилось."""
        loc = self.page.locator(self.cfg.selectors["project_title"])
        if loc.count() == 0:
            raise FlowError(ERR_UNKNOWN, "Поле имени проекта не найдено в шапке")
        field = loc.first
        field.click()
        self.page.keyboard.press("Control+A")
        self.page.keyboard.press("Delete")
        field.type(name, delay=20)
        self.page.keyboard.press("Enter")
        self.page.wait_for_timeout(1200)

        actual = field.input_value()
        if actual.strip() != name.strip():
            raise FlowError(
                ERR_UNKNOWN,
                "Имя проекта не применилось",
                detail=f"ожидалось {name!r}, в поле {actual!r}",
            )
        return actual

    # ---------------------------------------------------------------- network

    def _arm_network_listener(self, page: Page) -> None:
        """Логировать все non-2xx ответы tRPC вместе с телом."""

        def on_response(resp: Any) -> None:
            url = resp.url or ""
            if "/fx/api/trpc/" not in url:
                return
            # Только 4xx/5xx. 3xx здесь штатны: media.getMediaUrlRedirect по
            # своей природе отвечает 307 на КАЖДОЕ успешное обращение к медиа,
            # и трактовка «всё, что не 2xx — ошибка» валит любую удачную задачу.
            if resp.status < 400:
                return
            try:
                body = scrub(resp.text())[:4000]
            except Exception:  # noqa: BLE001 — тело могло быть уже отброшено
                body = "<тело недоступно>"
            self.trpc_errors.append(
                {
                    "url": url,
                    "status": resp.status,
                    "body": body,
                    "kind": classify_trpc_error(resp.status, body),
                    "ts": time.time(),
                }
            )

        page.on("response", on_response)

    def errors_since(self, ts: float) -> list[dict[str, Any]]:
        """Ошибки tRPC, пришедшие после указанного момента."""
        return [e for e in self.trpc_errors if e["ts"] >= ts]

    def raise_for_errors(self, since: float) -> None:
        """Превратить перехваченную ошибку Flow в типизированное исключение."""
        errs = self.errors_since(since)
        if not errs:
            return
        # Приоритет: то, что останавливает очередь, важнее того, что её не останавливает.
        priority = [ERR_QUOTA, ERR_UNUSUAL, ERR_MODERATION, ERR_THROTTLE, ERR_SERVER, ERR_UNKNOWN]
        errs.sort(key=lambda e: priority.index(e["kind"]) if e["kind"] in priority else 99)
        top = errs[0]
        raise FlowError(
            top["kind"],
            f"Flow вернул HTTP {top['status']} ({top['kind']})",
            detail=top["body"][:1000],
        )

    # ----------------------------------------------------------------- checks

    def check_selectors(self) -> list[SelectorCheck]:
        """Посчитать, сколько узлов находит каждый селектор."""
        sel = self.cfg.selectors
        create_name = self.cfg.locale.get("create_button", "Создать")
        page = self.page
        checks: list[SelectorCheck] = []

        def add(key: str, label: str, selector: str, required: bool = True) -> None:
            try:
                count = page.locator(selector).count()
            except Exception:  # noqa: BLE001 — битый селектор считаем как 0
                count = 0
            checks.append(SelectorCheck(key, label, selector, count, required))

        add("prompt_editor", "Поле промпта", sel["prompt_editor"])

        try:
            btn_count = page.get_by_role("button", name=create_name).count()
        except Exception:  # noqa: BLE001
            btn_count = 0
        checks.append(
            SelectorCheck(
                "create_button",
                "Кнопка «Создать»",
                f'get_by_role("button", name="{create_name}")',
                btn_count,
            )
        )

        add("file_input", "Загрузка в библиотеку", sel["file_input"])
        add("settings_trigger", "Триггер настроек", sel["settings_trigger"])
        add("add_dialog_trigger", "Кнопка «+» (пикер медиа)", sel["add_dialog_trigger"])
        add("search_input", "Поиск по библиотеке", sel["search_input"])
        add("virtuoso_scroller", "Скроллер библиотеки", sel["virtuoso_scroller"])
        add("virtuoso_item_list", "Список элементов библиотеки", sel["virtuoso_item_list"])
        add("result_video", "Готовые видео", sel["result_video"], required=False)
        add("result_image", "Готовые изображения", sel["result_image"], required=False)

        # virtuoso-item-list рендерится только когда в проекте есть что
        # виртуализировать. В пустом проекте его нет — и это не поломка вёрстки,
        # а нормальное состояние. Требуем его лишь при наличии медиа.
        by_key = {c.key: c for c in checks}
        has_media = by_key["result_video"].count + by_key["result_image"].count > 0
        by_key["virtuoso_item_list"].required = has_media
        return checks

    def session_info(self) -> SessionInfo:
        """Диагностика сессии из __NEXT_DATA__.

        access_token не читается в Python: из JS возвращается только булев флаг
        его наличия. Значение токена нигде не логируется и не сохраняется.
        """
        js = r"""
        () => {
          const el = document.getElementById('__NEXT_DATA__');
          if (!el) return { next_data_found: false };
          let d;
          try { d = JSON.parse(el.textContent || '{}'); } catch (e) { return { next_data_found: true }; }
          const s = d?.props?.pageProps?.session;
          if (!s) return { next_data_found: true, has_session: false };
          const email = s?.user?.email || null;
          const mask = email ? email.slice(0, 2) + '***@' + (email.split('@')[1] || '?') : null;
          return {
            next_data_found: true,
            has_session: true,
            has_access_token: Boolean(s.access_token),
            email_masked: mask,
            expires: s.expires || null,
          };
        }
        """
        try:
            data = self.page.evaluate(js) or {}
        except Exception:  # noqa: BLE001
            return SessionInfo()
        return SessionInfo(
            next_data_found=bool(data.get("next_data_found")),
            has_session=bool(data.get("has_session")),
            has_access_token=bool(data.get("has_access_token")),
            email_masked=data.get("email_masked"),
            expires=data.get("expires"),
        )

    # -------------------------------------------------------------- настройки

    def read_gen_settings(self) -> GenSettings | None:
        """Прочитать формат/количество/длительность из текста триггера."""
        loc = self.page.locator(self.cfg.selectors["settings_trigger"])
        if loc.count() == 0:
            return None
        return GenSettings.parse((loc.first.inner_text() or "").strip())

    def assert_aspect(self) -> tuple[bool, str]:
        """Обязательная проверка формата перед запуском генерации."""
        required = self.cfg.get("generation.required_aspect", "crop_9_16")
        gs = self.read_gen_settings()
        if gs is None:
            return False, "триггер настроек не найден — формат прочитать нечем"
        if gs.aspect is None:
            return False, f"в тексте триггера нет формата: {gs.raw!r}"
        if gs.aspect != required:
            return False, f"формат {gs.aspect}, а нужен {required}"
        return True, gs.raw

    def _menu_open(self) -> bool:
        return self.page.locator(self.cfg.selectors["settings_menu"]).count() > 0

    def open_settings(self) -> None:
        """Открыть меню настроек, если оно закрыто."""
        if self._menu_open():
            return
        self.page.locator(self.cfg.selectors["settings_trigger"]).first.click()
        self.page.wait_for_selector(self.cfg.selectors["settings_menu"], timeout=10_000)
        self.page.wait_for_timeout(300)

    def close_settings(self) -> None:
        """Закрыть меню настроек."""
        if not self._menu_open():
            return
        self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(400)

    def _tab(self, token: str):  # noqa: ANN202 — Locator
        menu = self.cfg.selectors["settings_menu"]
        tmpl = self.cfg.selectors["tab_template"].format(token=token)
        return self.page.locator(f"{menu} {tmpl}")

    def _select_tab(self, token: str, what: str) -> None:
        """Кликнуть таб, если он ещё не выбран. Лишних кликов не делаем."""
        tab = self._tab(token)
        if tab.count() == 0:
            raise FlowError(
                ERR_UNKNOWN,
                f"В меню настроек нет таба {what} (-content-{token})",
                detail="Разметка меню могла поменяться — перезапусти doctor.",
            )
        if tab.first.get_attribute("aria-selected") == "true":
            return
        tab.first.click()
        self.page.wait_for_timeout(600)
        if tab.first.get_attribute("aria-selected") != "true":
            raise FlowError(ERR_UNKNOWN, f"Не удалось выбрать таб {what} (-content-{token})")

    def ensure_settings(self, kind: Kind, duration: int | None = None) -> GenSettings:
        """Выставить тип/формат/количество (и длительность для видео) и проверить.

        Возвращает фактически прочитанные настройки. Бросает FlowError, если
        выставить не удалось.
        """
        gen = self.cfg.get("generation", {})
        kind_token = gen["kind_tabs"][kind]
        aspect_tab = gen["aspect_tab"]
        batch = int(gen.get("batch", 1))

        self.open_settings()
        # Тип задаём первым: от него зависит состав остальных табов.
        self._select_tab(kind_token, f"тип={kind}")

        if kind == "video":
            self._select_tab(gen["video_mode"], f"подрежим={gen['video_mode']}")
            dur = int(duration or gen.get("video_duration_sec", 8))
            allowed = [int(x) for x in gen.get("allowed_durations", [4, 6, 8, 10])]
            if dur not in allowed:
                raise FlowError(
                    ERR_UNKNOWN,
                    f"Длительность {dur}s не входит в допустимые {allowed}",
                )
            self._select_tab(str(dur), f"длительность={dur}s")

        self._select_tab(aspect_tab, f"формат={aspect_tab}")
        self._select_tab(str(batch), f"количество=x{batch}")
        self.close_settings()

        gs = self.read_gen_settings()
        if gs is None:
            raise FlowError(ERR_UNKNOWN, "После настройки не читается триггер настроек")

        required = gen.get("required_aspect", "crop_9_16")
        if gs.aspect != required:
            raise FlowError(
                ERR_UNKNOWN,
                f"Формат так и не выставился: в триггере {gs.aspect}, нужен {required}",
                detail=gs.raw,
            )
        if gs.batch != batch:
            raise FlowError(
                ERR_UNKNOWN,
                f"Количество так и не выставилось: x{gs.batch}, нужен x{batch}",
                detail=gs.raw,
            )
        return gs

    # ------------------------------------------------------------------ ввод

    def set_prompt(self, prompt: str) -> str:
        """Ввести промпт в Slate-редактор и обязательно верифицировать результат.

        Обычный fill() меняет DOM, но не внутреннее состояние Slate, и в
        генерацию уходит пустой промпт — поэтому только insert_text + проверка.
        """
        box = self.page.locator(self.cfg.selectors["prompt_editor"]).first
        box.click()
        self.page.wait_for_timeout(150)
        self._clear_editor()
        self.page.keyboard.insert_text(prompt)
        self.page.wait_for_timeout(400)

        actual = (box.inner_text() or "").strip()
        if _same_text(actual, prompt):
            return actual

        # Фоллбэк: посимвольный ввод — медленнее, но Slate его точно услышит.
        box.click()
        self._clear_editor()
        box.type(prompt, delay=15)
        self.page.wait_for_timeout(400)
        actual = (box.inner_text() or "").strip()
        if not _same_text(actual, prompt):
            raise FlowError(
                ERR_UNKNOWN,
                "Промпт не попал в Slate-редактор",
                detail=f"ожидалось {prompt[:120]!r}, в редакторе {actual[:120]!r}",
            )
        return actual

    def _clear_editor(self) -> None:
        self.page.keyboard.press("Control+A")
        self.page.keyboard.press("Delete")
        self.page.wait_for_timeout(100)

    # ------------------------------------------------------------- референсы

    def attached_refs(self) -> list[str]:
        """uuid референсов, прикреплённых к запросу прямо сейчас."""
        js = r"""
        (sel) => Array.from(document.querySelectorAll(sel))
          .flatMap(c => Array.from(c.querySelectorAll('img')).map(i => i.src || ''))
          .filter(Boolean)
        """
        srcs = self.page.evaluate(js, self.cfg.selectors["ref_chip"]) or []
        out: list[str] = []
        for s in srcs:
            m = _MEDIA_NAME_RE.search(s)
            if m:
                out.append(m.group(1))
        return out

    def clear_request(self) -> bool:
        """Снять промпт и все прикреплённые референсы одной кнопкой.

        Кнопка «Очистить запрос» существует только когда есть что чистить —
        её отсутствие означает, что панель уже пустая.
        """
        label = self.cfg.locale.get("clear_request", "Очистить запрос")
        btn = self.page.locator("button").filter(has_text=label)
        if btn.count() == 0:
            return False
        btn.first.click()
        self.page.wait_for_timeout(1000)
        return True

    def open_add_dialog(self) -> None:
        """Открыть пикер медиа (кнопка «+»)."""
        sel = self.cfg.selectors
        if self.page.locator(sel["add_dialog"]).count() > 0:
            return
        trigger = self.page.locator(sel["add_dialog_trigger"])
        if trigger.count() == 0:
            raise FlowError(ERR_UNKNOWN, "Кнопка «+» (aria-haspopup=dialog) не найдена")
        trigger.first.click()
        self.page.wait_for_selector(sel["add_dialog"], timeout=10_000)
        self.page.wait_for_timeout(800)

    def close_add_dialog(self) -> None:
        if self.page.locator(self.cfg.selectors["add_dialog"]).count() == 0:
            return
        self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(500)

    def _dialog_rows_uuids(self) -> list[str]:
        """uuid видимых строк пикера, в порядке отображения."""
        js = r"""
        (sel) => Array.from(document.querySelectorAll(sel)).map(r => {
          const img = r.querySelector('img');
          return img ? (img.src || '') : '';
        })
        """
        srcs = self.page.evaluate(js, self.cfg.selectors["add_row"]) or []
        out = []
        for s in srcs:
            m = _MEDIA_NAME_RE.search(s)
            out.append(m.group(1) if m else "")
        return out

    def _select_dialog_row(self, index: int) -> None:
        """Кликнуть строку пикера, только если она ещё не выбрана.

        Повторный клик по выбранной строке снимает выбор, и кнопка
        «Добавить в запрос» исчезает — поэтому проверяем aria-selected.
        """
        row = self.page.locator(self.cfg.selectors["add_row"]).nth(index)
        marker = row.locator("[aria-selected]").first
        if marker.count() and marker.get_attribute("aria-selected") == "true":
            return
        row.click()
        self.page.wait_for_timeout(700)

    def _confirm_add(self) -> None:
        label = self.cfg.locale.get("add_to_request", "Добавить в запрос")
        btn = self.page.locator("button").filter(has_text=label)
        if btn.count() == 0:
            raise FlowError(
                ERR_UNKNOWN,
                f"Кнопка «{label}» не появилась — элемент не выбран в пикере",
            )
        btn.first.click()
        self.page.wait_for_timeout(1500)

    def attach_ref_by_uuid(self, uuid: str, max_scrolls: int = 25) -> None:
        """Прикрепить элемент библиотеки по его media-uuid. Без повторной загрузки."""
        self.open_add_dialog()
        scroller = self.cfg.selectors["add_scroller"]
        for _ in range(max_scrolls):
            uuids = self._dialog_rows_uuids()
            if uuid in uuids:
                self._select_dialog_row(uuids.index(uuid))
                self._confirm_add()
                return
            at_end = self.page.evaluate(
                "(sel) => { const s = document.querySelector(sel);"
                " if (!s) return true;"
                " const end = s.scrollTop + s.clientHeight >= s.scrollHeight - 4;"
                " s.scrollTop += s.clientHeight * 0.8; return end; }",
                scroller,
            )
            self.page.wait_for_timeout(500)
            if at_end:
                break
        self.close_add_dialog()
        raise FlowError(ERR_UNKNOWN, f"В пикере не найден элемент с uuid {uuid}")

    def upload_to_library(self, path: Path, timeout_sec: int = 120) -> str:
        """Загрузить локальный файл в библиотеку проекта и вернуть его uuid.

        input[type=file] грузит именно в библиотеку («Загрузки»), а не в запрос.
        Ждём, пока файл появится в библиотеке, и возвращаем его uuid.
        """
        # Детект нового элемента идёт по списку библиотеки — пикер, если он
        # открыт, перекрывает её и путает снапшоты. Закрываем.
        self.close_add_dialog()
        before = set(self.media_snapshot().keys())
        inp = self.page.locator(self.cfg.selectors["file_input"]).first
        inp.set_input_files(str(path.resolve()))

        started = time.time()
        while time.time() - started < timeout_sec:
            self.page.wait_for_timeout(2000)
            self.scroll_library_to_fresh()
            new = [n for n in self.media_snapshot() if n not in before]
            if new:
                return new[0]
            self.raise_for_errors(started)
        raise FlowError(
            ERR_UNKNOWN,
            f"Файл {path.name} не появился в библиотеке за {timeout_sec}с",
            detail="Google мог отклонить файл при обработке — проверь формат и размер.",
        )

    def attach_refs(self, refs: Iterable[str | Path], resolver: Any) -> list[str]:
        """Прикрепить референсы к запросу. Возвращает список прикреплённых uuid.

        resolver отвечает за путь -> uuid: он решает, можно ли переиспользовать
        уже существующий элемент библиотеки, или файл надо залить (см. refs.py).

        Сначала разрешаем ВСЕ uuid, и только потом открываем пикер: разрешение
        может включать загрузку файла в библиотеку, а загрузка при открытом
        пикере — это гонка между его списком и списком библиотеки.
        """
        paths = list(refs)
        attached: list[str] = []
        for p in paths:
            try:
                attached.append(resolver.resolve(p))
            except (FileNotFoundError, ValueError) as exc:
                raise FlowError(ERR_UNKNOWN, str(exc)) from exc
        for uuid in attached:
            self.attach_ref_by_uuid(uuid)

        self.close_add_dialog()

        # Верификация: сколько чипов реально висит в панели.
        got = self.attached_refs()
        if len(got) < len(paths):
            raise FlowError(
                ERR_UNKNOWN,
                f"Прикрепилось {len(got)} референсов из {len(paths)}",
                detail=f"ожидались {attached}, в панели {got}",
            )
        return attached

    # ---------------------------------------------------------------- медиа

    def media_snapshot(self) -> dict[str, MediaItem]:
        """Снять текущее окно медиа библиотеки из DOM (она виртуализирована).

        Считаем ТОЛЬКО списки библиотеки. Прикреплённые референсы в панели ввода
        и строки пикера используют те же getMediaUrlRedirect-ссылки: если их не
        отсечь, свежеприкреплённый референс засчитается как новый результат.
        """
        js = r"""
        () => {
          const lists = Array.from(document.querySelectorAll('[data-testid="virtuoso-item-list"]'))
            .filter(l => !l.closest('[role=dialog]'));
          const out = [];
          for (const l of lists) {
            l.querySelectorAll('img[src*="getMediaUrlRedirect"], video[src*="getMediaUrlRedirect"]')
              .forEach(e => out.push({ tag: e.tagName.toLowerCase(), src: e.src || e.getAttribute('src') || '' }));
          }
          return out.filter(x => x.src);
        }
        """
        rows = self.page.evaluate(js) or []
        out: dict[str, MediaItem] = {}
        for r in rows:
            m = _MEDIA_NAME_RE.search(r["src"])
            if not m:
                continue
            name = m.group(1)
            # Видео-тег информативнее превьюшки: он выигрывает при совпадении uuid.
            if name in out and out[name].tag == "video":
                continue
            out[name] = MediaItem(name=name, url=r["src"], tag=r["tag"])
        return out

    def collect_all_media(self, max_rounds: int = 60) -> dict[str, MediaItem]:
        """Полный список медиа проекта: скроллим виртуализированный список.

        Наивный «собрать всё» вернёт окно из ~16 штук. Копим uuid в set, пока
        их количество не перестанет расти.
        """
        scroller = self.cfg.selectors["virtuoso_scroller"]
        acc: dict[str, MediaItem] = {}
        acc.update(self.media_snapshot())
        stable = 0
        for _ in range(max_rounds):
            before = len(acc)
            self.page.evaluate(
                "(sel) => { const s = document.querySelector(sel); if (s) s.scrollTop += s.clientHeight * 0.8; }",
                scroller,
            )
            self.page.wait_for_timeout(500)
            acc.update(self.media_snapshot())
            at_end = self.page.evaluate(
                "(sel) => { const s = document.querySelector(sel);"
                " return s ? s.scrollTop + s.clientHeight >= s.scrollHeight - 4 : true; }",
                scroller,
            )
            stable = stable + 1 if len(acc) == before else 0
            if at_end and stable >= 2:
                break
        return acc

    def scroll_library_to_fresh(self) -> None:
        """Прокрутить библиотеку к свежим результатам.

        Сортировка по умолчанию — «Недавние», новые элементы встают в НАЧАЛО
        списка (проверено на загрузке файла: он появился первой плиткой).
        """
        self.page.evaluate(
            "(sel) => { const s = document.querySelectorAll(sel);"
            " s.forEach(x => { if (!x.closest('[role=dialog]')) x.scrollTop = 0; }); }",
            self.cfg.selectors["virtuoso_scroller"],
        )
        self.page.wait_for_timeout(400)

    # ------------------------------------------------------------- генерация

    def create_button(self):  # noqa: ANN201 — Locator
        """Кнопка «Создать». Их на странице несколько — рабочая последняя."""
        name = self.cfg.locale.get("create_button", "Создать")
        return self.page.get_by_role("button", name=name).last

    def wait_ready(self, timeout_ms: int | None = None) -> None:
        """Дождаться aria-disabled="false".

        Кнопка блокируется через aria-disabled, а не через нативный disabled:
        клик по «неактивной» кнопке формально проходит и молча ничего не делает.
        """
        ms = timeout_ms or int(self.cfg.get("generation.ready_timeout_sec", 60)) * 1000
        expect(self.create_button()).to_have_attribute("aria-disabled", "false", timeout=ms)

    def click_create(self) -> None:
        """Запустить генерацию."""
        self.wait_ready()
        self.create_button().click()

    def wait_for_new_media(
        self,
        before: set[str],
        kind: Kind,
        timeout_sec: int | None = None,
        on_tick: Any = None,
    ) -> MediaItem:
        """Ждать появления нового медиа-URL, которого не было до запуска."""
        gen = self.cfg.get("generation", {})
        if timeout_sec is None:
            timeout_sec = int(
                gen.get("video_timeout_sec", 900) if kind == "video" else gen.get("image_timeout_sec", 180)
            )
        poll = int(gen.get("poll_interval_sec", 3))
        started = time.time()
        tick = 0

        while time.time() - started < timeout_sec:
            self.page.wait_for_timeout(poll * 1000)
            tick += 1
            # Раз в ~5 опросов подматываем список к свежим элементам.
            if tick % 5 == 0:
                self.scroll_library_to_fresh()

            snap = self.media_snapshot()
            new = [item for name, item in snap.items() if name not in before]
            if new:
                # Для видео предпочитаем <video>, но и превьюшка сгодится:
                # скачиваем всё равно по uuid, а тип определим по content-type.
                new.sort(key=lambda i: 0 if (kind == "video" and i.tag == "video") else 1)
                return new[0]

            if on_tick:
                on_tick(int(time.time() - started))

            self.raise_for_errors(started)

        raise FlowError(
            ERR_UNKNOWN,
            f"Результат не появился за {timeout_sec}с ({kind})",
        )

    def is_generating(self) -> bool:
        """Идёт ли генерация прямо сейчас (по тому же aria-disabled)."""
        try:
            return self.create_button().get_attribute("aria-disabled") == "true"
        except Exception:  # noqa: BLE001
            return False

    # -------------------------------------------------------------- скачивание

    def download(self, item: MediaItem, dest_stem: Path) -> Path:
        """Скачать медиа по прямой ссылке. Куки сессии подхватываются сами.

        Кнопки Download в UI нет и диалоги скачивания не используются.
        """
        resp = self.page.request.get(item.url)
        if not resp.ok:
            raise FlowError(
                classify_trpc_error(resp.status, ""),
                f"Скачивание вернуло HTTP {resp.status}",
                detail=item.url,
            )
        body = resp.body()
        if not body:
            raise FlowError(ERR_UNKNOWN, "Скачался пустой файл", detail=item.url)
        ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        ext = _EXT_BY_CTYPE.get(ctype) or (".mp4" if item.tag == "video" else ".png")
        dest = dest_stem.with_suffix(ext)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        return dest

    # ------------------------------------------------- диагностика разметки

    def screenshot(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(p))
        return p

    def dump_markup(self, key: str) -> str:
        """Показать актуальную разметку места, где селектор не нашёлся.

        Селекторы «по аналогии» не выдумываем — отдаём сырые факты, чтобы
        решение принимал человек.
        """
        dumpers = {
            "prompt_editor": self._dump_editables,
            "create_button": self._dump_buttons,
            "settings_trigger": self._dump_buttons,
            "add_dialog_trigger": self._dump_buttons,
            "file_input": self._dump_file_inputs,
            "search_input": self._dump_testids,
            "virtuoso_scroller": self._dump_testids,
            "virtuoso_item_list": self._dump_testids,
            "result_video": self._dump_media,
            "result_image": self._dump_media,
        }
        fn = dumpers.get(key)
        if fn is None:
            return "(нет дампера для этого ключа)"
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            return f"(дамп не удался: {type(exc).__name__}: {exc})"

    def _dump_buttons(self) -> str:
        js = r"""
        () => Array.from(document.querySelectorAll('button, [role=button]'))
          .map(b => ({
            text: (b.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 70),
            aria_label: b.getAttribute('aria-label'),
            aria_disabled: b.getAttribute('aria-disabled'),
            haspopup: b.getAttribute('aria-haspopup'),
            testid: b.getAttribute('data-testid'),
          }))
          .filter(b => b.text || b.aria_label || b.testid)
          .slice(0, 60)
        """
        rows = self.page.evaluate(js) or []
        if not rows:
            return "На странице не найдено ни одной кнопки."
        out = [f"Кнопок на странице: {len(rows)} (первые 60)"]
        for r in rows:
            bits = [f"text={r['text']!r}"]
            for k, lbl in (("aria_label", "aria-label"), ("testid", "data-testid"),
                           ("haspopup", "aria-haspopup"), ("aria_disabled", "aria-disabled")):
                if r.get(k) is not None:
                    bits.append(f"{lbl}={r[k]!r}")
            out.append("  " + "  ".join(bits))
        return "\n".join(out)

    def _dump_editables(self) -> str:
        js = r"""
        () => Array.from(document.querySelectorAll('[contenteditable], textarea, input[type=text]'))
          .map(e => ({
            tag: e.tagName.toLowerCase(),
            attrs: e.getAttributeNames()
              .filter(n => n !== 'class' && n !== 'style')
              .map(n => n + '=' + JSON.stringify(e.getAttribute(n)))
              .join(' '),
          }))
          .slice(0, 30)
        """
        rows = self.page.evaluate(js) or []
        if not rows:
            return "Ни contenteditable, ни textarea, ни input[type=text] на странице нет."
        return "\n".join([f"Полей ввода: {len(rows)}"] + [f"  <{r['tag']}> {r['attrs']}" for r in rows])

    def _dump_file_inputs(self) -> str:
        js = r"""
        () => Array.from(document.querySelectorAll('input[type=file]'))
          .map(i => ({ accept: i.getAttribute('accept'), multiple: i.multiple,
                       testid: i.getAttribute('data-testid') }))
        """
        rows = self.page.evaluate(js) or []
        if not rows:
            return "input[type=file] на странице нет вообще."
        return "\n".join(
            [f"Файловых инпутов: {len(rows)}"]
            + [f"  accept={r['accept']!r} multiple={r['multiple']} data-testid={r['testid']!r}" for r in rows]
        )

    def _dump_testids(self) -> str:
        js = r"""
        () => [...new Set(Array.from(document.querySelectorAll('[data-testid]'))
          .map(e => e.getAttribute('data-testid')))].slice(0, 120)
        """
        ids = self.page.evaluate(js) or []
        if not ids:
            return "Атрибутов data-testid на странице нет вообще."
        return f"Все data-testid на странице ({len(ids)}):\n  " + "\n  ".join(ids)

    def _dump_media(self) -> str:
        js = r"""
        () => {
          const items = Array.from(document.querySelectorAll('img, video'))
            .map(e => ({ tag: e.tagName.toLowerCase(), src: (e.src || e.getAttribute('src') || '').slice(0, 120) }))
            .filter(x => x.src);
          return { total: items.length, sample: items.slice(0, 25) };
        }
        """
        d = self.page.evaluate(js) or {}
        items = d.get("sample", [])
        if not items:
            return "На странице нет ни одного <img>/<video> с src."
        return "\n".join(
            [f"Всего <img>/<video> с src: {d.get('total')} (первые 25)"]
            + [f"  <{it['tag']}> {it['src']}" for it in items]
        )


def _same_text(actual: str, expected: str) -> str | bool:
    """Сравнение с нормализацией пробелов: Slate вставляет свои пробельные узлы."""
    norm = lambda s: " ".join(s.replace("\u00a0", " ").split())  # noqa: E731
    return norm(actual) == norm(expected)
