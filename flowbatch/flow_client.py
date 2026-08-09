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

import httpx
from playwright.sync_api import Browser, Page, Playwright, expect, sync_playwright

from .config import Config
from .scrub import scrub

Kind = Literal["image", "video"]

_PROJECT_ID_RE = re.compile(r"/flow/project/([0-9a-fA-F-]{8,})")

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
# Список «+»-пикера отстал от библиотеки: нужного медиа в нём нет, хотя в
# проекте оно есть. Лечится перезагрузкой вкладки — см. reload_page().
ERR_STALE_PICKER = "stale_picker"
# Прогон прерван пользователем прямо посреди ожидания результата.
# Не ошибка задачи: ни ретраев, ни смягчения, ни пометки FAILED.
ERR_ABORTED = "aborted"
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


def list_flow_tabs(endpoint: str, match: str = "labs.google/fx") -> list[dict[str, str]]:
    """Перечислить открытые вкладки Flow по HTTP-эндпоинту отладки.

    Намеренно без Playwright: панель дёргает список часто, а поднимать ради
    этого драйвер — секунды на каждый опрос. /json/list отдаёт targetId,
    и именно по нему потом привязывается воркер: URL для этого не годится,
    у двух вкладок одного проекта он одинаковый.
    """
    url = endpoint.rstrip("/") + "/json/list"
    data = httpx.get(url, timeout=4).json()
    tabs: list[dict[str, str]] = []
    for t in data:
        if t.get("type") != "page":
            continue
        page_url = t.get("url") or ""
        if match not in page_url:
            continue
        m = _PROJECT_ID_RE.search(page_url)
        tabs.append(
            {
                "id": t.get("id") or "",
                "url": page_url,
                "title": (t.get("title") or "").strip(),
                "project_id": m.group(1) if m else "",
            }
        )
    return tabs


class FlowClient:
    """Обёртка над вкладкой Flow. Используется как контекстный менеджер."""

    def __init__(self, cfg: Config, target_id: str | None = None) -> None:
        self.cfg = cfg
        # Привязка к конкретной вкладке. None — взять первую подходящую
        # (обычный однопоточный режим). Задаётся в параллельном режиме,
        # где каждый воркер обязан держаться своей вкладки.
        self.target_id = target_id
        # Выносить вкладку на передний план имеет смысл, только когда она одна:
        # три воркера, дерущихся за фокус, переключали бы вкладки без остановки.
        self.bring_to_front = True
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
        if not self.bring_to_front:
            self._keep_page_awake(self._page)
        return self._page

    @staticmethod
    def page_target_id(page: Page) -> str:
        """targetId вкладки — единственный её стабильный идентификатор.

        Playwright такого свойства не даёт, но CDP-сессию к странице открыть
        можно, и Target.getTargetInfo отвечает ровно тем же id, что и
        /json/list. Так список вкладок в панели сходится с воркерами.
        """
        try:
            sess = page.context.new_cdp_session(page)
            info = sess.send("Target.getTargetInfo") or {}
            sess.detach()
            return str(info.get("targetInfo", {}).get("targetId") or "")
        except Exception:  # noqa: BLE001 — идентификация не должна ронять подключение
            return ""

    def _keep_page_awake(self, page: Page) -> None:
        """Не дать Chromium придушить фоновую вкладку.

        Параллельный режим держит 2–3 вкладки, и на переднем плане может быть
        только одна. Chromium режет фоновым вкладкам таймеры и снимает фокус,
        а Flow опрашивает статус генерации именно таймерами. Обе команды —
        штатные CDP: эмуляция фокуса и запрет ухода вкладки в «замороженное»
        состояние. Если браузер их не поддержит, просто работаем как есть.
        """
        try:
            sess = page.context.new_cdp_session(page)
            sess.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
            sess.send("Page.setWebLifecycleState", {"state": "active"})
        except Exception:  # noqa: BLE001 — необязательная оптимизация
            pass

    def _pick_page(self) -> Page:
        """Выбрать вкладку Flow среди уже открытых. Новых вкладок не создаём."""
        assert self._browser is not None
        match = self.cfg.get("cdp.page_url_match", "labs.google/fx")
        all_pages: list[Page] = []
        for ctx in self._browser.contexts:
            all_pages.extend(ctx.pages)

        if not all_pages:
            raise FlowClientError("В подключённом браузере нет ни одной вкладки.")

        if self.target_id:
            for p in all_pages:
                if self.page_target_id(p) == self.target_id:
                    return p
            raise FlowClientError(
                f"Вкладка {self.target_id[:12]}… не найдена — её закрыли или "
                "браузер перезапустили. Обнови список вкладок в панели."
            )

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

    def reload_page(self, timeout_ms: int = 90_000) -> None:
        """Перезагрузить вкладку и дождаться готовности редактора.

        Единственный известный способ обновить список «+»-пикера. Он берётся
        один раз вместе со страницей: в проекте PODMENA пикер показывал 20
        элементов (первая серия плюс загрузки), тогда как в библиотеке их было
        34 — всё, сгенерированное в текущей сессии, в пикер не попадало.
        После перезагрузки — 34 из 34, искомый элемент на месте.

        Промпт и прикреплённые референсы перезагрузка стирает, поэтому
        вызывать её нужно ДО ввода промпта — задача пойдёт с начала.
        """
        self.page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
        self.page.wait_for_selector(self.cfg.selectors["prompt_editor"], timeout=timeout_ms)
        # Библиотека и пикер дозагружаются уже после domcontentloaded.
        self.page.wait_for_timeout(2500)

    def focus(self) -> None:
        """Вывести вкладку на передний план: фоновые вкладки троттлятся.

        В параллельном режиме отключено (bring_to_front=False): вместо драки за
        передний план вкладку удерживает «живой» _keep_page_awake.
        """
        if not self.bring_to_front:
            return
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

    def ensure_workspace(self, timeout_ms: int = 60_000) -> None:
        """Вернуть вкладку проекта в рабочий вид с редактором промпта.

        Вкладку могли оставить в галерее медиа (полноэкранная библиотека
        с сайдбаром) или в просмотрщике — там редактор и навигация скрыты,
        и любой клик по ним висит до таймаута. Сначала пробуем Escape
        (закрывает оверлеи), не помогло — перезагрузка вкладки: она всегда
        открывает проект в виде по умолчанию.
        """
        ed = self.page.locator(self.cfg.selectors["prompt_editor"])
        back_label = self.cfg.locale.get("back_button", "Назад")

        def visible() -> bool:
            # Редактора мало: у полноэкранной галереи медиа есть СВОЯ строка
            # промпта, и по одному редактору она неотличима от рабочего вида.
            # Кнопка «Назад» в галерее скрыта — по ней и отличаем.
            try:
                if not ed.first.is_visible():
                    return False
                back = self.page.locator("button").filter(has_text=back_label)
                return back.count() > 0 and back.first.is_visible()
            except Exception:  # noqa: BLE001 — нет элемента = не видим
                return False

        if visible():
            return
        for _ in range(2):
            try:
                self.page.keyboard.press("Escape")
                self.page.wait_for_timeout(700)
            except Exception:  # noqa: BLE001
                break
            if visible():
                return
        self.reload_page(timeout_ms)
        if not visible():
            raise FlowError(
                ERR_UNKNOWN,
                "Редактор промпта не появился даже после перезагрузки вкладки",
            )

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
            try:
                back.first.click(timeout=8_000)
            except Exception:  # noqa: BLE001 — таймаут клика, а не логики
                # Кнопка в DOM, но «not visible»: вкладка стоит в галерее
                # медиа или просмотрщике, где навигация скрыта. Возвращаем
                # рабочий вид (Escape, при нужде перезагрузка) и повторяем.
                self.ensure_workspace()
                back = self.page.locator("button").filter(has_text=back_label)
                back.first.click(timeout=timeout_ms)

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

    def _find_project_href(self, name: str) -> str | None:
        """href карточки проекта с точным именем (на странице списка).

        Карточка устроена так: <a href="/fx/.../project/<id>"> с превью, а имя —
        в соседнем <span>. Кликабельна ссылка, не имя, поэтому ищем href.
        """
        js = r"""
        (name) => {
          const spans = Array.from(document.querySelectorAll('span'))
            .filter(e => Array.from(e.childNodes)
              .some(n => n.nodeType === 3 && n.textContent.trim() === name));
          for (const s of spans) {
            let e = s;
            for (let i = 0; e && i < 8; i++) {
              const a = e.querySelector && e.querySelector('a[href*="/flow/project/"]');
              if (a) return a.getAttribute('href');
              e = e.parentElement;
            }
          }
          return null;
        }
        """
        return self.page.evaluate(js, name)

    def _scan_projects_for(self, name: str, max_scrolls: int) -> str | None:
        """Пролистать список проектов в поисках имени.

        Замерено вживую: сразу после перехода на список карточек в DOM НОЛЬ —
        они дорисовываются лениво и лежат ниже баннера, а листается ОКНО,
        не virtuoso-контейнер. Поэтому: сначала ждём появления хотя бы одной
        карточки, потом крутим и окно, и контейнер (на всякий случай).
        Без этого старые проекты «не находились», и ensure_project плодил
        дубли с тем же именем.
        """
        try:
            self.page.wait_for_selector(
                'a[href*="/flow/project/"]', state="attached", timeout=15_000
            )
        except Exception:  # noqa: BLE001 — у пустого аккаунта карточек нет вовсе
            pass
        href = self._find_project_href(name)
        if href:
            return href
        stable = 0
        for _ in range(max_scrolls):
            self.page.evaluate(
                "(sel) => { const s = document.querySelector(sel);"
                " if (s) s.scrollTop += s.clientHeight * 0.8;"
                " window.scrollBy(0, Math.round(window.innerHeight * 0.8)); }",
                self.cfg.selectors["virtuoso_scroller"],
            )
            self.page.wait_for_timeout(450)
            href = self._find_project_href(name)
            if href:
                return href
            at_end = self.page.evaluate(
                "() => (window.innerHeight + window.scrollY)"
                " >= document.documentElement.scrollHeight - 4"
            )
            # Конец подтверждаем дважды: хвост списка дорисовывается с лагом.
            stable = stable + 1 if at_end else 0
            if stable >= 2:
                break
        return self._find_project_href(name)

    def ensure_project(self, name: str, max_scrolls: int = 30) -> str:
        """Открыть проект Flow с этим именем; нет такого — создать.

        Возвращает 'current' | 'opened' | 'created'. Список проектов
        виртуализирован, поэтому при поиске скроллим его.
        """
        name = name.strip()
        if not name:
            raise FlowError(ERR_UNKNOWN, "Пустое имя проекта")
        if self.current_project_id() and (self.project_name() or "").strip() == name:
            return "current"

        self.goto_projects_list()
        href = self._scan_projects_for(name, max_scrolls)

        if href is None:
            # «Не нашёл» — это либо проекта правда нет, либо список протух
            # (Flow дорисовывает его лениво и не всегда честно). Прежде чем
            # СОЗДАВАТЬ, обновляем страницу и ищем ещё раз: создание по
            # протухшему списку плодит проекты-дубли с одним именем, и
            # дальше все @use/@lib смотрят то в один, то в другой.
            create_label = self.cfg.locale.get("create_project", "Создать проект")
            self.page.reload(wait_until="domcontentloaded", timeout=90_000)
            try:
                self.page.wait_for_selector(
                    f'button:has-text("{create_label}")', timeout=60_000
                )
            except Exception:  # noqa: BLE001 — список без кнопки бывает при пустом аккаунте
                pass
            self.page.wait_for_timeout(1_500)
            href = self._scan_projects_for(name, max_scrolls)

        if href is None:
            self.create_project(name)
            return "created"

        self.page.locator(f'a[href="{href}"]').first.click()
        self.page.wait_for_selector(self.cfg.selectors["prompt_editor"], timeout=60_000)
        self.page.wait_for_timeout(800)
        actual = (self.project_name() or "").strip()
        if actual != name:
            raise FlowError(
                ERR_UNKNOWN,
                f"Открылся не тот проект: ожидался {name!r}, в шапке {actual!r}",
            )
        return "opened"

    def rename_media(self, uuid: str, new_name: str, timeout_ms: int = 15_000) -> str:
        """Переименовать элемент библиотеки по его uuid. Возвращает новое имя.

        Путь проверен вживую: навести на плитку -> у неё появляется
        единственная кнопка с aria-haspopup=menu -> пункт «Переименовать»
        -> поле ввода -> Enter. Кнопка появляется ТОЛЬКО при наведении,
        поэтому hover обязателен.
        """
        rename_label = self.cfg.locale.get("rename_media", "Переименовать")
        list_sel = self.cfg.selectors["virtuoso_item_list"]

        # Плитку ищем ОТ медиа с нужным uuid, а не перебором детей списка:
        # прямых детей у него всего два, а сами превью лежат на пятнадцатом
        # уровне вложенности. Перебор находил ровно две плитки из семнадцати,
        # остальные падали с «не найдена».
        # Всё взаимодействие — мышью по координатам, а элемент ищется заново
        # на каждом шаге. Причина: Virtuoso перемонтирует плитку при любой
        # перерисовке. Локатор Playwright после этого ждёт «стабильности» до
        # таймаута, а собственный data-атрибут просто исчезает вместе с узлом.
        # На один uuid в плитке приходится ДВА элемента: постер <img> и сам
        # <video>. Один из них скрыт и отдаёт нулевой прямоугольник, поэтому
        # querySelector наугад брал невидимый. Берём самый крупный.
        find_media = r"""
        (uuid) => {
          const all = Array.from(document.querySelectorAll(
            `[data-testid="virtuoso-item-list"] img[src*="${uuid}"],`
            + ` [data-testid="virtuoso-item-list"] video[src*="${uuid}"]`));
          if (!all.length) return null;
          all[0].scrollIntoView({block: 'center'});
          let best = null;
          for (const e of all) {
            const r = e.getBoundingClientRect();
            if (!best || r.width * r.height > best.w * best.h) {
              best = {x: r.x + r.width / 2, y: r.y + r.height / 2, w: r.width, h: r.height};
            }
          }
          return best;
        }
        """
        # Размер до прокрутки не проверяем: за пределами окна virtuoso держит
        # плитку свёрнутой в нулевую высоту, и осмысленный прямоугольник
        # появляется только после scrollIntoView.
        if self.page.evaluate(find_media, uuid) is None:
            raise FlowError(ERR_STALE_PICKER, f"Плитка с uuid {uuid} не найдена в библиотеке")
        self.page.wait_for_timeout(500)
        box = self.page.evaluate(find_media, uuid)
        if not box or box["w"] < 2 or box["h"] < 2:
            raise FlowError(
                ERR_STALE_PICKER,
                f"Плитка {uuid[:8]} не раскрылась после прокрутки",
                detail=f"прямоугольник: {box}",
            )

        self.page.mouse.move(box["x"], box["y"])
        self.page.wait_for_timeout(500)

        # Кнопка «Ещё» появляется только под курсором и живёт выше по дереву:
        # поднимаемся от медиа до ближайшего предка, где она есть.
        btn = self.page.evaluate(
            r"""
            (uuid) => {
              const m = document.querySelector(
                `[data-testid="virtuoso-item-list"] img[src*="${uuid}"],`
                + ` [data-testid="virtuoso-item-list"] video[src*="${uuid}"]`);
              if (!m) return null;
              let n = m;
              while (n && n !== document.body) {
                const bs = n.querySelectorAll('button[aria-haspopup="menu"]');
                if (bs.length) {
                  const r = bs[bs.length - 1].getBoundingClientRect();
                  if (r.width < 2 || r.height < 2) return null;
                  return {x: r.x + r.width / 2, y: r.y + r.height / 2};
                }
                n = n.parentElement;
              }
              return null;
            }
            """,
            uuid,
        )
        if not btn:
            raise FlowError(ERR_UNKNOWN, f"У плитки {uuid[:8]} не появилось меню «Ещё»")
        self.page.mouse.click(btn["x"], btn["y"])
        self.page.wait_for_timeout(600)
        item = self.page.get_by_role("menuitem", name=rename_label)
        if item.count() == 0:
            self.page.keyboard.press("Escape")
            raise FlowError(ERR_UNKNOWN, f"В меню плитки нет пункта «{rename_label}»")
        item.first.click(timeout=timeout_ms)
        self.page.wait_for_timeout(600)
        # Поле переименования — единственное активное поле ввода.
        field = self.page.locator(
            'input:focus, [contenteditable="true"]:focus, [role=dialog] input'
        ).first
        if field.count() == 0:
            self.page.keyboard.press("Escape")
            raise FlowError(ERR_UNKNOWN, "Поле переименования не появилось")
        # Поле предзаполнено текущим именем Flow — читаем его, чтобы
        # шаблон вида "{n}_{name}" сохранил исходное название.
        try:
            current = (field.input_value() or "").strip()
        except Exception:  # noqa: BLE001 — contenteditable вместо input
            current = (field.inner_text() or "").strip()
        new_name = new_name.replace("{name}", current) if "{name}" in new_name else new_name
        if current == new_name:
            # Уже названо как надо — незачем перепечатывать. Делает
            # массовое переименование идемпотентным: повторный проход
            # по проекту ничего не трогает и идёт втрое быстрее.
            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(200)
            return new_name
        field.click()
        self.page.keyboard.press("Control+A")
        self.page.keyboard.press("Delete")
        field.type(new_name, delay=15)
        self.page.keyboard.press("Enter")
        self.page.wait_for_timeout(900)
        return new_name

    def open_project_by_id(self, project_id: str, timeout_ms: int = 60_000) -> str:
        """Открыть проект по его id и вернуть имя.

        Надёжнее, чем ensure_project по имени: имена в Flow не уникальны
        (легко завести второй «SAHARNY_DOM_P01» и попасть в пустышку), а
        список проектов перерисовывается прямо под курсором, и клик по
        ссылке ловит «element is not stable». id из журнала однозначен.
        """
        base = re.sub(r"/flow/project/[0-9a-fA-F-]+.*$", "/flow", self.page.url or "")
        if "/flow" not in base:
            base = "https://labs.google/fx/ru/tools/flow"
        self.page.goto(f"{base}/project/{project_id}", timeout=timeout_ms)
        self.page.wait_for_timeout(3500)
        got = self.current_project_id() or ""
        if got.lower() != project_id.lower():
            raise FlowError(
                ERR_UNKNOWN,
                f"Не удалось открыть проект {project_id[:8]}",
                detail=f"после перехода в адресе {got[:8] or '—'}",
            )
        return self.project_name() or ""

    def rename_many(self, wanted: dict[str, str], on_step: Any = None,
                    max_rounds: int = 60) -> dict[str, str]:
        """Переименовать пачку плиток за один проход по библиотеке.

        wanted: {uuid: новое имя}. Возвращает {uuid: что получилось}.

        Список виртуализирован — в DOM живёт лишь окно из полутора десятков
        плиток, поэтому идём сверху вниз: переименовываем всё, что видно,
        прокручиваем, повторяем. Плитки, которых так и не встретили,
        в ответ не попадут — их видно по разнице ключей.
        """
        done: dict[str, str] = {}
        pending = dict(wanted)
        scroller = self.cfg.selectors["virtuoso_scroller"]
        to_top = (
            "(sel) => { document.querySelectorAll(sel).forEach("
            "s => { if (!s.closest('[role=dialog]')) s.scrollTop = 0; }); }"
        )
        scroll_down = (
            "(sel) => { const s = Array.from(document.querySelectorAll(sel))"
            ".find(x => !x.closest('[role=dialog]'));"
            " if (!s) return true;"
            " s.scrollTop += s.clientHeight * 0.7;"
            " return s.scrollTop + s.clientHeight >= s.scrollHeight - 4; }"
        )
        self.page.evaluate(to_top, scroller)
        self.page.wait_for_timeout(700)

        # Ровно одно переименование на один свежий снимок DOM. Пакетом нельзя:
        # диалог переименования перемонтирует виртуализированный список, и все
        # плитки, найденные до него, разом выпадают из DOM — второе и далее
        # переименования падали с «плитка не найдена».
        passes = 0
        for _ in range(max_rounds):
            if not pending:
                break
            visible = set(self.media_snapshot().keys())
            hit = next((u for u in pending if u in visible), None)
            if hit is not None:
                target = pending.pop(hit)
                try:
                    done[hit] = self.rename_media(hit, target)
                except Exception as exc:  # noqa: BLE001 — одна плитка не рушит проход
                    done[hit] = f"ОШИБКА: {scrub(str(exc))[:90]}"
                if on_step:
                    on_step(hit, done[hit], len(pending))
                self.page.wait_for_timeout(400)
                continue

            at_end = self.page.evaluate(scroll_down, scroller)
            self.page.wait_for_timeout(650)
            if at_end:
                # Дошли до дна, а что-то ещё не встретили: список мог
                # перемотаться после диалога — начинаем сверху. Два пустых
                # прохода подряд означают, что этих плиток тут просто нет.
                passes += 1
                if passes >= 2:
                    break
                self.page.evaluate(to_top, scroller)
                self.page.wait_for_timeout(700)
        return done

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

    # ------------------------------------------------------------- модерация

    def moderation_state(self) -> dict[str, Any]:
        """Сколько раз на странице встречаются фразы модерации + кусок текста.

        Flow сообщает об отказе не только сетевым кодом, но и надписью прямо
        на плитке («видео может нарушать наши правила»), при этом HTTP-ответ
        бывает успешным. Скан по innerText не зависит от разметки — фразы
        задаются в config.yaml и дополняются по мере встречи новых.
        """
        phrases = [
            str(p)
            for p in ((self.cfg.get("moderation.phrases", []) or [])
                      + (self.cfg.get("moderation.phrases_third_party", []) or [])
                      + (self.cfg.get("moderation.phrases_unusual", []) or []))
            if str(p).strip()
        ]
        if not phrases:
            return {"count": 0, "snippet": None}
        js = r"""
        (phrases) => {
          const text = (document.body.innerText || '').toLowerCase();
          let count = 0, snippet = null;
          for (const p of phrases) {
            const pl = p.toLowerCase();
            let idx = 0;
            for (;;) {
              idx = text.indexOf(pl, idx);
              if (idx === -1) break;
              count++;
              if (snippet === null) {
                snippet = text.slice(Math.max(0, idx - 70), idx + pl.length + 70)
                  .replace(/\s+/g, ' ').trim();
              }
              idx += pl.length;
            }
          }
          return { count, snippet };
        }
        """
        try:
            return self.page.evaluate(js, phrases) or {"count": 0, "snippet": None}
        except Exception:  # noqa: BLE001 — скан не должен ронять ожидание
            return {"count": 0, "snippet": None}

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

    # ------------------------------------------- пикер: проект, поиск, имена

    def _picker_project_button(self):  # noqa: ANN202 — Locator
        """Кнопка выбора проекта в шапке пикера (первый haspopup=menu в диалоге)."""
        return self.page.locator(
            f'{self.cfg.selectors["add_dialog"]} button[aria-haspopup="menu"]'
        ).first

    def set_picker_project(self, name: str) -> None:
        """Переключить пикер на библиотеку другого проекта. No-op, если уже там."""
        name = name.strip()
        btn = self._picker_project_button()
        if btn.count() == 0:
            raise FlowError(ERR_UNKNOWN, "В пикере не найдена кнопка выбора проекта")
        current = (btn.inner_text() or "").split("\n")[0].strip()
        if current == name:
            return
        btn.click()
        self.page.wait_for_timeout(800)
        # Точное совпадение имени в приоритете: подстрочный поиск не смог бы
        # выбрать проект, чьё имя — префикс другого («Дыхание» и «Дыхание(старое)»).
        idx = self.page.evaluate(
            r"""
            (name) => {
              const items = Array.from(document.querySelectorAll(
                '[role=menu] [role=menuitem], [role=menu] [role=menuitemradio]'));
              const texts = items.map(i => (i.innerText || '').replace(/\s+/g, ' ').trim());
              let hit = texts.findIndex(t => t === name);
              if (hit === -1) {
                const subs = texts.map((t, i) => t.includes(name) ? i : -1).filter(i => i >= 0);
                hit = subs.length === 1 ? subs[0] : -1;
              }
              return hit;
            }
            """,
            name,
        )
        if idx < 0:
            self.page.keyboard.press("Escape")
            # STALE, а не UNKNOWN: выпадашка проектов грузится вместе со
            # страницей и новые проекты не подтягивает — раннер перезагрузит
            # вкладку и повторит задачу, после чего список свежий.
            raise FlowError(
                ERR_STALE_PICKER,
                f"В меню пикера нет проекта с точным именем {name!r} (или подстрока неоднозначна)",
                detail="Если проект точно есть — список выпадашки протух, лечится перезагрузкой вкладки.",
            )
        self.page.locator(
            '[role=menu] [role=menuitem], [role=menu] [role=menuitemradio]'
        ).nth(idx).click()
        self.page.wait_for_timeout(1500)
        now = (self._picker_project_button().inner_text() or "").split("\n")[0].strip()
        if now != name:
            raise FlowError(ERR_UNKNOWN, f"Пикер не переключился: ожидался {name!r}, показан {now!r}")

    def _picker_tab(self, kind: str) -> None:
        """Включить таб типа в пикере: kind = 'all' | 'images'.

        Подписи берутся из locale-блока конфига, как и остальные надписи UI.
        Промах не фатален (останется активный таб), но это сузит только
        видимую выборку, а не сломает выбор по uuid/имени.
        """
        label = self.cfg.locale.get(
            "picker_tab_images" if kind == "images" else "picker_tab_all",
            "Изображения" if kind == "images" else "Все",
        )
        tab = self.page.locator(f'{self.cfg.selectors["add_dialog"]} [role=tab]').filter(
            has_text=label
        )
        if tab.count() == 0:
            return
        if tab.first.get_attribute("aria-selected") == "true":
            return
        tab.first.click()
        self.page.wait_for_timeout(800)

    def _locate_row_scrolling(self, uuid: str, max_scrolls: int = 25) -> bool:
        """Доскроллить пикер до строки с uuid. True, если строка на экране.

        Конец списка подтверждаем дважды: виртуализированный список может
        дорисовывать хвост после того, как scrollTop уже упёрся в низ.
        """
        scroller = self.cfg.selectors["add_scroller"]
        end_hits = 0
        for _ in range(max_scrolls):
            if uuid in self._dialog_rows_uuids():
                return True
            at_end = self.page.evaluate(
                "(sel) => { const s = document.querySelector(sel);"
                " if (!s) return true;"
                " const end = s.scrollTop + s.clientHeight >= s.scrollHeight - 4;"
                " s.scrollTop += s.clientHeight * 0.8; return end; }",
                scroller,
            )
            self.page.wait_for_timeout(500)
            end_hits = end_hits + 1 if at_end else 0
            if end_hits >= 2:
                break
        return uuid in self._dialog_rows_uuids()

    def picker_search(self, query: str) -> None:
        """Ввести запрос в поиск пикера (контролируемый React-инпут — посимвольно)."""
        inp = self.page.locator(self.cfg.selectors["add_search"]).first
        inp.click()
        self.page.keyboard.press("Control+A")
        self.page.keyboard.press("Delete")
        if query:
            inp.type(query, delay=30)
        self.page.wait_for_timeout(1200)

    def picker_rows(self) -> list[dict[str, str]]:
        """Видимые строки пикера: имя, тип, uuid."""
        js = r"""
        (sel) => Array.from(document.querySelectorAll(sel)).map(r => {
          const img = r.querySelector('img');
          const lines = (r.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
          return { name: lines[0] || '', type: lines[1] || '', src: img ? (img.src || '') : '' };
        })
        """
        rows = self.page.evaluate(js, self.cfg.selectors["add_row"]) or []
        out = []
        for r in rows:
            m = _MEDIA_NAME_RE.search(r["src"])
            out.append({"name": r["name"], "type": r["type"], "uuid": m.group(1) if m else ""})
        return out

    def list_library(self, query: str = "", project: str | None = None,
                     only_images: bool = True, max_scrolls: int = 20) -> list[dict[str, str]]:
        """Список элементов библиотеки (текущего или другого проекта) по имени."""
        self.open_add_dialog()
        if project:
            self.set_picker_project(project)
        else:
            current = (self.project_name() or "").strip()
            if current:
                self.set_picker_project(current)
        self._picker_tab("images" if only_images else "all")
        self.picker_search(query)

        seen: dict[str, dict[str, str]] = {}
        scroller = self.cfg.selectors["add_scroller"]
        end_hits = 0
        for _ in range(max_scrolls):
            before = len(seen)
            for r in self.picker_rows():
                if r["uuid"]:
                    seen[r["uuid"]] = r
            at_end = self.page.evaluate(
                "(sel) => { const s = document.querySelector(sel); if (!s) return true;"
                " const end = s.scrollTop + s.clientHeight >= s.scrollHeight - 4;"
                " s.scrollTop += s.clientHeight * 0.8; return end; }",
                scroller,
            )
            self.page.wait_for_timeout(400)
            # Конец подтверждаем дважды И без прироста элементов: хвост
            # виртуализированного списка может дорисоваться с опозданием.
            end_hits = end_hits + 1 if (at_end and len(seen) == before) else 0
            if end_hits >= 2:
                break
        return list(seen.values())

    def attach_from_library(self, query: str, project: str | None = None) -> str:
        """Найти картинку в библиотеке по имени и прикрепить к запросу.

        Правило выбора: единственное вхождение подстроки, либо точное совпадение
        имени, если вхождений несколько. Иначе — ошибка со списком кандидатов.
        Возвращает uuid прикреплённого элемента.
        """
        items = self.list_library(query, project=project, only_images=True)
        matches = [i for i in items if query.lower() in i["name"].lower()]
        exact = [i for i in matches if i["name"] == query]
        where = f"в проекте {project!r}" if project else "в текущем проекте"

        if not matches:
            self.close_add_dialog()
            # STALE: пикер не дорисовывает свежезалитое — перезагрузка вкладки
            # обновляет список, раннер сделает её сам и повторит задачу.
            raise FlowError(
                ERR_STALE_PICKER,
                f"@lib: {where} нет картинки с именем содержащим {query!r}",
                detail=(
                    "Имена видны в «+»-диалоге Flow или через команду library. "
                    "ВАЖНО: у сгенерированных результатов имя в библиотеке НЕ "
                    "совпадает с id задачи — их прикрепляют через @use: "
                    "'@use id_задачи' (тот же проект) или "
                    "'@use Имя проекта :: id_задачи' (другой проект)."
                ),
            )
        if len(exact) == 1:
            target = exact[0]
        elif len(matches) == 1:
            target = matches[0]
        else:
            names = ", ".join(repr(i["name"]) for i in matches[:6])
            self.close_add_dialog()
            raise FlowError(
                ERR_UNKNOWN,
                f"@lib: {where} по запросу {query!r} найдено {len(matches)} картинок — уточни имя",
                detail=f"кандидаты: {names}",
            )

        # После сбора список мог уехать скроллом — возвращаем поиск и
        # доскролливаем до нужной строки (точное совпадение может лежать
        # глубже первого экрана).
        if target["uuid"] not in self._dialog_rows_uuids():
            self.picker_search(query)
        if not self._locate_row_scrolling(target["uuid"]):
            self.close_add_dialog()
            raise FlowError(ERR_UNKNOWN, f"@lib: строка {target['name']!r} пропала из пикера")
        uuids = self._dialog_rows_uuids()
        self._select_dialog_row(uuids.index(target["uuid"]))
        self._confirm_add()
        return target["uuid"]

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

    def attach_ref_by_uuid(self, uuid: str, project: str | None = None,
                           max_scrolls: int = 25) -> None:
        """Прикрепить элемент библиотеки по его media-uuid. Без повторной загрузки.

        Пикер запоминает последний выбранный проект, поэтому явно возвращаем его
        на нужный: иначе после @lib из чужого проекта uuid текущего проекта
        в списке не найдётся.
        """
        self.open_add_dialog()
        want = (project or self.project_name() or "").strip()
        if want:
            self.set_picker_project(want)
        self._picker_tab("all")
        self.picker_search("")
        if self._locate_row_scrolling(uuid, max_scrolls=max_scrolls):
            uuids = self._dialog_rows_uuids()
            self._select_dialog_row(uuids.index(uuid))
            self._confirm_add()
            return
        self.close_add_dialog()
        raise FlowError(
            ERR_STALE_PICKER,
            f"В пикере не найден элемент с uuid {uuid}",
            detail=(
                "Список пикера Flow загружается один раз вместе со страницей и "
                "не видит медиа, сгенерированные в этой же сессии. Перезагрузка "
                "вкладки его обновляет."
            ),
        )

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

        resolver превращает каждую спеку (путь или lib:имя) в RefHandle: либо
        готовый uuid (файл залит/переиспользован), либо задание «найти в
        библиотеке по имени» (см. refs.py).

        Сначала разрешаем ВСЁ, и только потом открываем пикер: разрешение
        может включать загрузку файла в библиотеку, а загрузка при открытом
        пикере — это гонка между его списком и списком библиотеки.
        """
        specs = list(refs)
        try:
            handles = [resolver.resolve(s) for s in specs]
        except (FileNotFoundError, ValueError) as exc:
            raise FlowError(ERR_UNKNOWN, str(exc)) from exc

        attached: list[str] = []
        for h in handles:
            if h.uuid:
                self.attach_ref_by_uuid(h.uuid, project=h.picker_project)
                attached.append(h.uuid)
            else:
                attached.append(self.attach_from_library(h.search or "", project=h.picker_project))

        self.close_add_dialog()

        # Верификация: и количество чипов, и их состав. Сверка по uuid ловит
        # случай «прикрепилось что-то не то», который счётчик пропустил бы.
        got = self.attached_refs()
        missing = [u for u in attached if u not in set(got)]
        if len(got) < len(specs) or missing:
            raise FlowError(
                ERR_UNKNOWN,
                f"Прикрепилось {len(got)} референсов из {len(specs)}"
                + (f", не хватает {[m[:8] for m in missing]}" if missing else ""),
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
        moderation_baseline: int = 0,
        arbiter: Any = None,
        should_abort: Any = None,
    ) -> MediaItem:
        """Ждать появления нового медиа-URL, которого не было до запуска.

        moderation_baseline — сколько фраз модерации было на странице ДО клика:
        старые упавшие плитки из прошлых сессий не должны давать ложных
        срабатываний, поэтому реагируем только на прирост.

        arbiter — общий на все вкладки реестр «этот результат уже занят».
        Нужен только в параллельном режиме: три вкладки смотрят в один проект,
        и без реестра две задачи могли бы забрать один и тот же файл. Пока
        arbiter не передан, ветка полностью выключена и поведение то же, что
        и было в однопоточном прогоне.

        should_abort — проверяется на каждом опросе. Даёт «Стоп сейчас»:
        бросить ожидание в течение секунд, а не досиживать до конца
        генерации видео. Сама генерация во Flow при этом продолжится, её
        результат просто останется в библиотеке нескачанным.
        """
        gen = self.cfg.get("generation", {})
        if timeout_sec is None:
            timeout_sec = int(
                gen.get("video_timeout_sec", 900) if kind == "video" else gen.get("image_timeout_sec", 180)
            )
        poll = int(gen.get("poll_interval_sec", 3))
        started = time.time()
        tick = 0
        settle = 0

        while time.time() - started < timeout_sec:
            if should_abort is not None and should_abort():
                raise FlowError(ERR_ABORTED, "ожидание результата прервано по «Стоп сейчас»")
            self.page.wait_for_timeout(poll * 1000)
            tick += 1
            # Раз в ~5 опросов подматываем список к свежим элементам.
            if tick % 5 == 0:
                self.scroll_library_to_fresh()

            snap = self.media_snapshot()
            new = [item for name, item in snap.items() if name not in before]
            if new and arbiter is not None:
                # Чужое не трогаем — его уже забрал другой воркер.
                new = [i for i in new if arbiter.is_free(i.name)]
                # Кнопка «Создать» в МОЕЙ вкладке остаётся заблокированной,
                # пока идёт МОЯ генерация. Значит, всплывший результат при
                # заблокированной кнопке — с соседней вкладки. Ждём три опроса
                # и всё-таки берём: если Flow когда-нибудь перестанет
                # блокировать кнопку, задача не должна зависнуть навсегда.
                if new and settle < 3 and self.is_generating():
                    settle += 1
                    new = []
            if new:
                # Для видео предпочитаем <video>, но и превьюшка сгодится:
                # скачиваем всё равно по uuid, а тип определим по content-type.
                new.sort(key=lambda i: 0 if (kind == "video" and i.tag == "video") else 1)
                if arbiter is not None and not arbiter.claim(new[0].name):
                    continue  # успели перехватить между проверкой и захватом
                return new[0]

            if on_tick:
                on_tick(int(time.time() - started))

            # Слой 1: сетевые коды tRPC (PUBLIC_ERROR_UNSAFE_GENERATION и пр.)
            self.raise_for_errors(started)

            # Слой 2: надпись модерации на странице (HTTP при этом может быть 200)
            mod = self.moderation_state()
            if mod["count"] > moderation_baseline:
                raise FlowError(
                    ERR_MODERATION,
                    "Flow показал сообщение модерации на странице",
                    detail=f"текст на странице: «{mod['snippet']}»",
                )

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
