"""Запуск браузера с отладочным портом.

Важно, что именно тут происходит: запускается ТВОЙ Edge на ТВОЁМ профиле —
ровно та команда, что в README, просто её больше не надо набирать руками.
Это не «свой браузер» в смысле Playwright: никакого headless, никакого
чистого профиля и никакой автоматизации логина Google. Сессия берётся из
папки профиля, в которой ты залогинился один раз сам.

Две ловушки, из-за которых наивный запуск не работает:

1. Chromium 136+ игнорирует --remote-debugging-port на профиле по умолчанию.
   Поэтому --user-data-dir обязателен и обязан быть отдельным.
2. Если этот профиль уже открыт БЕЗ порта, повторный запуск не поднимет новый
   процесс: он передаст ссылку уже работающему окну, откроет вкладку — и порт
   так и не появится. Такое надо распознать и сказать прямо, а не ждать
   таймаута непонятно чего.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

from .config import Config

# Где искать браузер, если путь не задан в конфиге. Edge первым: проект
# затачивался под него, и сессия Flow живёт именно там.
CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

DEFAULT_PROFILE = r"C:\edge-flow-profile"
DEFAULT_URL = "https://labs.google/fx/ru/tools/flow"


class BrowserError(RuntimeError):
    """Не удалось запустить браузер или дождаться порта."""


def find_browser(configured: str = "") -> Path:
    """Путь к браузеру: из конфига, иначе первый найденный из известных."""
    if configured:
        p = Path(configured)
        if not p.exists():
            raise BrowserError(f"cdp.browser_path указывает в никуда: {p}")
        return p
    for c in CANDIDATES:
        if Path(c).exists():
            return Path(c)
    raise BrowserError(
        "Не нашёл ни Edge, ни Chrome в стандартных местах. "
        "Пропиши путь в config.yaml -> cdp.browser_path."
    )


def port_of(endpoint: str) -> int:
    """Порт из endpoint вида http://localhost:9222."""
    tail = endpoint.rstrip("/").rsplit(":", 1)[-1]
    try:
        return int(tail)
    except ValueError as exc:
        raise BrowserError(f"не разобрал порт в cdp.endpoint={endpoint!r}") from exc


def version(endpoint: str, timeout: float = 2.0) -> str | None:
    """Имя браузера, если порт отвечает. Иначе None."""
    try:
        r = httpx.get(endpoint.rstrip("/") + "/json/version", timeout=timeout)
        return str(r.json().get("Browser") or "браузер")
    except Exception:  # noqa: BLE001 — «не отвечает» это штатный ответ, не сбой
        return None


def profile_holder(user_data_dir: str | Path) -> int | None:
    """PID процесса, который уже держит этот профиль. None — свободен.

    Без этой проверки повторный запуск молча уходит в существующее окно, и
    остаётся гадать, почему порт не поднялся.
    """
    if os.name != "nt":
        return None
    want = str(Path(user_data_dir)).lower()
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' or Name='chrome.exe'\" | "
        "ForEach-Object { \"$($_.ProcessId)|$($_.CommandLine)\" }"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=20,
        ).stdout
    except Exception:  # noqa: BLE001 — проверка необязательная
        return None
    for line in out.splitlines():
        pid, _, cmd = line.partition("|")
        if want in cmd.lower() and pid.strip().isdigit():
            return int(pid)
    return None


def launch(
    cfg: Config,
    tabs: int = 1,
    wait_sec: int = 40,
    on_step: Any = None,
) -> dict[str, Any]:
    """Поднять браузер с отладочным портом и дождаться, пока он ответит.

    tabs — сколько вкладок Flow открыть сразу (для параллельного режима).
    Возвращает {'started': bool, 'browser': str, 'profile': str, 'tabs': int}.
    """
    say = on_step or (lambda _s: None)
    endpoint = str(cfg.get("cdp.endpoint", "http://localhost:9222"))
    port = port_of(endpoint)
    profile = str(cfg.get("cdp.user_data_dir", "") or DEFAULT_PROFILE)
    url = str(cfg.get("cdp.start_url", "") or DEFAULT_URL)
    exe = find_browser(str(cfg.get("cdp.browser_path", "") or ""))
    # Потолок 7 — под режим «до 7 проектов параллельно, по вкладке на каждый».
    tabs = max(1, min(int(tabs), 7))

    already = version(endpoint)
    if already:
        # tabs — это «пусть будет столько», а не «открой ещё столько»: иначе
        # повторный запуск команды каждый раз плодил бы новые вкладки.
        have = _count_flow_tabs(endpoint)
        say(f"порт {port} уже отвечает: {already}, вкладок Flow {have}")
        opened = _open_tabs(endpoint, url, tabs - have, say) if tabs > have else 0
        return {"started": False, "browser": already, "profile": profile,
                "tabs": max(have, tabs) if opened or have else 1, "exe": str(exe)}

    holder = profile_holder(profile)
    if holder:
        raise BrowserError(
            f"Профиль {profile} уже открыт процессом {holder}, но БЕЗ отладочного порта.\n"
            "Повторный запуск не поднимет порт — он просто откроет вкладку в том окне.\n"
            f"Закрой это окно браузера (или заверши процесс {holder}) и запусти снова."
        )

    Path(profile).mkdir(parents=True, exist_ok=True)
    args = [
        str(exe),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        url,
    ]
    say(f"запускаю {exe.name} на профиле {profile}")
    flags = 0
    if os.name == "nt":
        # Браузер должен пережить выход из CLI, а не умереть вместе с ним.
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(args, creationflags=flags, close_fds=True)
    except Exception as exc:  # noqa: BLE001
        raise BrowserError(f"не смог запустить {exe}: {type(exc).__name__}: {exc}") from exc

    deadline = time.time() + wait_sec
    while time.time() < deadline:
        got = version(endpoint)
        if got:
            say(f"порт {port} ответил: {got}")
            if tabs > 1:
                # Пауза не косметическая: первой вкладке нужно доехать до
                # приложения и получить сессию, иначе соседние встретят
                # незавершённый OAuth.
                time.sleep(4.0)
            opened = _open_tabs(endpoint, url, tabs - 1, say) if tabs > 1 else 0
            return {"started": True, "browser": got, "profile": profile,
                    "tabs": opened + 1, "exe": str(exe)}
        time.sleep(1.0)

    raise BrowserError(
        f"Браузер запустился, но порт {port} не ответил за {wait_sec}с.\n"
        "Чаще всего это значит, что профиль оказался дефолтным: на нём Chromium 136+ "
        "отладочный порт не поднимает. Проверь cdp.user_data_dir в config.yaml."
    )


def open_more_tabs(endpoint: str, count: int, url: str | None = None, say: Any = None) -> int:
    """Открыть ещё count вкладок Flow в уже работающем браузере."""
    return _open_tabs(endpoint, url or DEFAULT_URL, count, say or (lambda _s: None))


def _count_flow_tabs(endpoint: str) -> int:
    """Сколько вкладок Flow уже открыто."""
    try:
        data = httpx.get(endpoint.rstrip("/") + "/json/list", timeout=4).json()
    except Exception:  # noqa: BLE001
        return 0
    return sum(
        1 for t in data
        if t.get("type") == "page" and "labs.google/fx" in (t.get("url") or "")
    )


def _open_tabs(endpoint: str, url: str, count: int, say: Any) -> int:
    """Доокрыть вкладки в уже работающем окне. Возвращает сколько открыто.

    Через отладочный порт, а не повторным запуском exe. Запуск нескольких
    процессов подряд сразу после старта ловит гонку в OAuth: вкладки просят
    сессию одновременно, callback не сходится, и одна из них приезжает на
    страницу входа с error=OAuthCallback при живой сессии в соседней.
    """
    opened = 0
    for _ in range(max(0, count)):
        try:
            r = httpx.put(endpoint.rstrip("/") + "/json/new?" + url, timeout=10)
            if r.status_code >= 400:  # старые сборки принимают только GET
                r = httpx.get(endpoint.rstrip("/") + "/json/new?" + url, timeout=10)
            if r.status_code >= 400:
                break
            opened += 1
            # Вкладки поднимаются по очереди, а не наперегонки за одну сессию.
            time.sleep(2.0)
        except Exception:  # noqa: BLE001 — вкладку всегда можно открыть руками
            break
    if opened:
        say(f"открыл ещё вкладок: {opened}")
    return opened
