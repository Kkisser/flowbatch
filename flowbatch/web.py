"""Локальная веб-панель: несколько независимых прогонов в одном окне.

Главная единица — «прогон» (слот): своя очередь (.xlsx / .yaml / .txt /
вставленный текст), свои вкладки браузера, свой проект Flow, свой Старт/Стоп
и свой статус. Прогонов до 7, и они работают одновременно: пока в одном
проекте генерится видео, соседний проект спокойно гонит свою очередь.

Что при этом общее на все прогоны (и почему):
  - ритм пауз (Pacer): антибан считается на аккаунт, а не на проект. Семь
    прогонов дают ту же частоту запросов, что и один, — быстрее становится
    только за счёт ожидания результатов внахлёст;
  - журнал runs.jsonl и кэш референсов: оба под замками;
  - замок создания проектов: два прогона не создадут одноимённые проекты
    наперегонки.

Чего общего НЕТ: два прогона не могут работать в одном проекте Flow.
Готовность определяется диффом списка медиа проекта, и второй прогон в том
же проекте перепутал бы чужие результаты со своими. Панель это проверяет
и останавливает второй прогон с объяснением.

По умолчанию сервер слушает только 127.0.0.1 и отдаёт файлы только из out/
и screenshots/. Панель управляет твоим браузером и запускает генерации, так
что любой, кто до неё дотянулся, распоряжается аккаунтом Flow как ты сам.
Отсюда правило: адрес, отличный от 127.0.0.1 (например IP в Tailscale),
включает обязательный токен — см. serve().
"""

from __future__ import annotations

import html
import base64
import binascii
import json
import mimetypes
import os
import platform
import secrets
import subprocess
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import httpx
from rich.console import Console

from .browser import BrowserError, launch, open_more_tabs, version as browser_version
from .config import Config
from .flow_client import FlowClient, FlowClientError, list_flow_tabs
from .notify import Notifier
from .parallel import MAX_TABS, Pacer, ParallelRunner
from .promptfile import SYNTAX_HELP
from .promptfile import parse as parse_prompts
from .queue import RunLog, load_jobs
from .refs import RefCache, RefResolver, parse_lib_spec
from .runner import STOP_QUEUE
from .sheet import ST_DONE, ST_ERROR, SheetQueue
from .soften import backend_status, build_softener, ensure_ollama

# Потолки. MAX_TABS (3) — вкладок на ОДИН проект; здесь — сколько всего
# прогонов и сколько всего вкладок суммарно на аккаунт.
MAX_SLOTS = 7
TOTAL_TAB_CAP = 7

UI_FILE = ".flowbatch_ui.json"

# Адреса, при которых панель доступна только с этой машины и токен не нужен.
LOOPBACK = {"127.0.0.1", "::1", "localhost", ""}

STATUS_DISPLAY = {"ok": "DONE", "failed": "ERROR", "dry_run": "DRY", "IN_PROGRESS": "IN_PROGRESS"}


def tailscale_ip() -> str | None:
    """IPv4 этой машины в Tailscale, если он поднят.

    Нужен, чтобы слушать РОВНО интерфейс Tailscale, а не 0.0.0.0: на 0.0.0.0
    панель торчала бы ещё и в любой Wi-Fi, к которому подключён ноутбук.
    """
    exe = Path(r"C:\Program Files\Tailscale\tailscale.exe")
    cmd = [str(exe) if exe.exists() else "tailscale", "ip", "-4"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in (res.stdout or "").splitlines():
        ip = line.strip()
        if ip.startswith("100."):
            return ip
    return None

HTML = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>flowbatch</title>
<style>
:root{
--bg:#07090e;--panel:#0e1219;--panel2:#151b26;--line:#1d2533;--line2:#2b3648;
--fg:#e9eef7;--dim:#94a2ba;--dim2:#5b6880;--ok:#3fb950;--err:#f85149;--run:#e3a008;
--todo:#7d8ba0;--dry:#a371f7;--accent:#4f9cff;
--p1:#38bdf8;--p2:#c084fc;--p3:#4ade80;--p4:#fbbf24;--p5:#f472b6;--p6:#2dd4bf;--p7:#fb923c;
--r:14px;--rs:9px;font-synthesis:none}
*{box-sizing:border-box}
::selection{background:#26456e}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:#232c3c;border-radius:6px;border:2px solid var(--bg)}
::-webkit-scrollbar-thumb:hover{background:#313d52}
::-webkit-scrollbar-track{background:transparent}
body{margin:0;background:radial-gradient(1200px 500px at 50% -180px,#101a2c 0%,var(--bg) 60%) fixed,var(--bg);
color:var(--fg);font:13.5px/1.55 -apple-system,"Segoe UI Variable Text","Segoe UI",Roboto,sans-serif;
-webkit-font-smoothing:antialiased;font-variant-numeric:tabular-nums}

/* ------------------------------------------------------------------ шапка */
header{padding:0 16px;height:54px;border-bottom:1px solid var(--line);display:flex;gap:10px;
align-items:center;position:sticky;top:0;z-index:6;
background:#0b0f16d9;backdrop-filter:blur(10px)}
h1{font-size:16px;margin:0 2px 0 0;font-weight:700;letter-spacing:-.01em;flex:none}
h1 b{background:linear-gradient(90deg,#5b9dff,#a78bfa);-webkit-background-clip:text;
background-clip:text;color:transparent}
.sep{width:1px;height:22px;background:var(--line);flex:none}
.grow{flex:1}

.pill{display:inline-flex;gap:7px;align-items:center;background:#141a26;
border:1px solid var(--line);border-radius:99px;padding:3px 11px;font-size:12px;
color:var(--dim);white-space:nowrap}
.pill b{color:var(--fg);font-weight:600}
.dot{width:7px;height:7px;border-radius:50%;flex:none;box-shadow:0 0 0 3px #ffffff09}
.dot.live{animation:pulse 1.2s infinite alternate}
@keyframes pulse{from{opacity:.4}to{opacity:1}}

main{display:grid;grid-template-columns:minmax(0,1fr) 396px;height:calc(100vh - 54px)}
@media(max-width:1200px){main{display:block;height:auto}}
section{overflow:auto;padding:16px 16px 60px;min-width:0}
aside{border-left:1px solid var(--line);overflow:auto;padding:14px;background:#0b0e15}
@media(max-width:1200px){aside{border-left:none;border-top:1px solid var(--line)}}

/* -------------------------------------------------------------- примитивы */
input,select,textarea{font:inherit;background:#0d121b;color:var(--fg);
border:1px solid var(--line2);border-radius:var(--rs);padding:7px 11px;outline:none;
transition:border-color .12s,box-shadow .12s}
input:focus,select:focus,textarea:focus{border-color:var(--accent);box-shadow:0 0 0 3px #4f9cff22}
input::placeholder{color:var(--dim2)}
input[type=text]{flex:1;min-width:180px}
input[type=number]{width:88px}
input[type=checkbox]{accent-color:var(--accent);width:15px;height:15px;cursor:pointer;margin:0}
textarea{width:100%;min-height:170px;resize:vertical;
font:12px/1.55 ui-monospace,Consolas,monospace;white-space:pre;tab-size:2}
select{cursor:pointer}

.btn{font:inherit;font-weight:500;cursor:pointer;border-radius:var(--rs);padding:7px 14px;
background:#1a2130;color:var(--fg);border:1px solid var(--line2);white-space:nowrap;
transition:background .12s,border-color .12s,opacity .12s;display:inline-flex;
align-items:center;gap:6px;line-height:1.3}
.btn:hover:not(:disabled){background:#222b3d;border-color:#3c4a61}
.btn:disabled{opacity:.35;cursor:not-allowed}
.btn.primary{background:linear-gradient(180deg,#549aff,#3272d9);border-color:#3d7ad6;
color:#fff;font-weight:600;box-shadow:0 1px 12px #3576dd3d}
.btn.primary:hover:not(:disabled){filter:brightness(1.08)}
.btn.danger{background:#2a1417;border-color:#5c2b2b;color:#ff9d96}
.btn.danger:hover:not(:disabled){background:#38191d;border-color:#7a3838}
.btn.ghost{background:transparent;border-color:transparent;color:var(--dim)}
.btn.ghost:hover:not(:disabled){background:#1a2130;color:var(--fg)}
.btn.sm{padding:3px 10px;font-size:12px;border-radius:7px}
.btn.icon{padding:7px 10px}
label.chk{display:inline-flex;gap:7px;align-items:center;color:var(--dim);cursor:pointer;
user-select:none;white-space:nowrap}
label.chk:hover{color:var(--fg)}
.note{font-size:12px;color:var(--dim);line-height:1.55}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.row+.row{margin-top:8px}

/* ------------------------------------------------------------------ слоты */
#gstats{display:flex;gap:7px;flex-wrap:wrap;margin:0 0 14px}
.slot{position:relative;background:linear-gradient(180deg,var(--panel),#0b0f16);
border:1px solid var(--line);border-radius:var(--r);padding:13px 15px 12px 18px;
margin-bottom:14px;box-shadow:0 8px 28px #00000052;transition:border-color .15s}
.slot:hover{border-color:#26314a}
.slot::before{content:"";position:absolute;left:0;top:14px;bottom:14px;width:3px;
border-radius:0 3px 3px 0;background:var(--sc)}
.shead{display:flex;gap:9px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
.sbadge{font-weight:800;font-size:12px;padding:2px 9px;border-radius:7px;flex:none;
color:var(--sc);background:color-mix(in srgb,var(--sc) 14%,transparent);
border:1px solid color-mix(in srgb,var(--sc) 35%,transparent)}
.sname{font-weight:650;font-size:14px;overflow:hidden;text-overflow:ellipsis;
white-space:nowrap;max-width:34%}
.sel-t{font-size:12px;color:var(--dim)}
.badge{display:inline-flex;gap:5px;align-items:center;padding:2px 9px;border-radius:99px;
font-size:11px;font-weight:600;white-space:nowrap}
.badge::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor}
.s-TODO{background:#1a202b;color:var(--todo)}
.s-IN_PROGRESS{background:#33270d;color:var(--run)}
.s-IN_PROGRESS::before{animation:pulse 1.1s infinite alternate}
.s-DONE{background:#10281a;color:var(--ok)}
.s-ERROR{background:#2e161a;color:var(--err)}
.s-DRY{background:#221936;color:var(--dry)}
.s-SKIP{background:#1a202b;color:var(--todo)}

.tchips{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:9px}
.tchips .lbl{font-size:11.5px;color:var(--dim2);text-transform:uppercase;letter-spacing:.06em}
.tchip{font:12px/1.3 inherit;border-radius:8px;padding:4px 10px;cursor:pointer;
border:1px solid var(--line2);background:#0e141f;color:var(--dim);transition:all .12s}
.tchip:hover:not(.busy):not(:disabled){border-color:var(--sc);color:var(--fg)}
.tchip.mine{border-color:var(--sc);color:var(--sc);
background:color-mix(in srgb,var(--sc) 11%,transparent);font-weight:600}
.tchip.busy{opacity:.4;cursor:not-allowed}
.tchip:disabled{cursor:not-allowed;opacity:.5}

.pbwrap{margin-top:10px}
.pb{height:7px;border-radius:5px;background:#141b28;overflow:hidden;display:flex}
.pb span{height:100%;transition:width .4s}
.pb .ok{background:linear-gradient(90deg,#2ea043,#3fb950)}
.pb .er{background:#c93c37}
.pb .dr{background:#8957e5}
.worker{display:flex;gap:9px;align-items:center;padding:6px 10px;border-radius:8px;
background:#0d1119;border:1px solid var(--line);margin-top:7px;font-size:12px}
.worker .wl{font-weight:700;font-size:11px;flex:none}
.worker .jid{font-family:ui-monospace,Consolas,monospace;flex:1;min-width:0;
overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.worker .el{color:var(--dim);flex:none}
.snote{margin-top:9px;font-size:12.5px}
.snote .okc{color:#7ee29a}.snote .erc{color:#ff9d96}
.stail{margin-top:7px;font:11px/1.5 ui-monospace,Consolas,monospace;color:var(--dim2);
white-space:pre-wrap;word-break:break-word}

.qwrap{margin-top:10px;border:1px solid var(--line);border-radius:10px;
max-height:44vh;overflow:auto;background:#0b0f16}
table{width:100%;border-collapse:collapse}
thead th{position:sticky;top:0;background:#10151f;z-index:2;text-align:left;
font-size:10.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--dim2);
font-weight:700;padding:8px;border-bottom:1px solid var(--line);white-space:nowrap}
thead th.sortable{cursor:pointer;user-select:none}
thead th.sortable:hover{color:var(--fg)}
thead th .arr{color:var(--accent);margin-left:3px}
td{padding:8px;border-bottom:1px solid #141a25;vertical-align:top}
tbody tr:hover td{background:#101724}
tr.active td{background:#131c2c;box-shadow:inset 2px 0 0 var(--run)}
.id{font-family:ui-monospace,Consolas,monospace;font-size:12px;white-space:nowrap}
.prompt{color:var(--dim);font-size:12px;max-width:520px;overflow:hidden;cursor:pointer;
display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;line-height:1.5}
.prompt.full{-webkit-line-clamp:unset}
.err{color:var(--err);font-size:11px;margin-top:4px;max-width:520px;line-height:1.45}
.del{opacity:0;transition:opacity .12s;color:var(--dim2);background:none;border:none;
cursor:pointer;font-size:14px;padding:0 4px;line-height:1}
tbody tr:hover .del{opacity:1}
.del:hover{color:var(--err)}
.empty{color:var(--dim2);padding:20px 8px;text-align:center}
#addslot{width:100%;padding:13px;border:1px dashed var(--line2);background:transparent;
border-radius:var(--r);color:var(--dim);font:inherit;cursor:pointer;transition:all .15s}
#addslot:hover{border-color:var(--accent);color:var(--accent);background:#4f9cff0d}

/* ------------------------------------------------------------------ aside */
.apanel{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);
padding:12px 13px;margin-bottom:12px}
h2{font-size:11px;text-transform:uppercase;letter-spacing:.09em;color:var(--dim2);
margin:0 0 9px;font-weight:700;display:flex;align-items:center;gap:8px}
.atab{display:flex;gap:8px;align-items:center;padding:7px 9px;border-radius:8px;
background:#0d1119;border:1px solid var(--line);margin-bottom:6px;font-size:12px}
.atab .nm{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.atab .own{font-weight:700;font-size:11px;flex:none}
pre#log{background:#080b11;border:1px solid var(--line);border-radius:9px;padding:10px;
font:11.5px/1.6 ui-monospace,Consolas,monospace;white-space:pre-wrap;word-break:break-word;
max-height:32vh;min-height:110px;overflow:auto;margin:0;color:#c4cddc}
pre#log .lt{font-weight:700}
.gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(90px,1fr));gap:8px}
.gallery a{display:block;border:1px solid var(--line);border-radius:9px;overflow:hidden;
background:#000;transition:border-color .12s}
.gallery a:hover{border-color:var(--accent)}
.gallery img,.gallery video{width:100%;height:136px;object-fit:cover;display:block}
.gallery span{display:block;font-size:10px;color:var(--dim);padding:4px 6px;
overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

/* ----------------------------------------------------------------- диалог */
dialog{background:var(--panel);color:var(--fg);border:1px solid var(--line2);
border-radius:16px;padding:0;width:min(680px,92vw);max-height:84vh}
dialog::backdrop{background:#000000a8;backdrop-filter:blur(3px)}
.dhead{display:flex;align-items:center;gap:10px;padding:14px 18px;border-bottom:1px solid var(--line);
font-weight:650;font-size:14px}
.dtabs{display:flex;gap:4px;padding:10px 14px 0}
.dtabs button{font:inherit;font-size:12.5px;background:none;border:none;color:var(--dim);
padding:7px 12px;cursor:pointer;border-radius:8px 8px 0 0;border-bottom:2px solid transparent}
.dtabs button.on{color:var(--fg);border-bottom-color:var(--accent)}
.dbody{padding:14px 18px 18px;overflow:auto;max-height:60vh}
.dsec{display:none}.dsec.on{display:block}
.helpgrid{display:grid;grid-template-columns:auto 1fr;gap:8px 14px;font-size:12.5px;
color:var(--dim);margin:6px 0}
.helpgrid b{color:var(--fg);font-weight:600;white-space:nowrap}
.hint{font:11.5px/1.65 ui-monospace,Consolas,monospace;color:var(--dim);
white-space:pre-wrap;background:#0a0d13;border:1px solid var(--line);
border-radius:9px;padding:10px 12px;margin:8px 0 0}
.bkrow{font-size:12.5px;color:var(--dim);padding:3px 0}
.bkrow b{color:var(--fg)}

#toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);
border-radius:10px;padding:11px 18px;font-size:13px;max-width:640px;white-space:pre-wrap;
display:none;z-index:99;box-shadow:0 10px 34px #000c}
#toast.err{background:#2d1518;border:1px solid #5c2b2b;color:#ff9d96}
#toast.ok{background:#122a1c;border:1px solid #2b5c3b;color:#7ee29a}
.mut{font-size:12px;color:var(--dim2)}
</style></head><body>
<header>
  <h1>flow<b>batch</b></h1>
  <span class="sep"></span>
  <span class="pill" id="bstat"><span class="dot" style="background:var(--todo)"></span>…</span>
  <button class="btn sm" id="bbtn">Запустить браузер</button>
  <select id="bcount" class="sm" title="сколько вкладок Flow должно быть открыто" style="padding:4px 8px">
    <option>1</option><option>2</option><option selected>3</option><option>5</option><option>7</option>
  </select>
  <span class="grow"></span>
  <span class="pill" title="кто переписывает промпт, если Flow завернул его по модерации">
    <span class="dot" id="softdot" style="background:var(--todo)"></span>
    смягчение
    <select id="soft" style="border:none;background:transparent;padding:2px 4px;color:var(--fg)"></select>
  </span>
  <button class="btn danger sm" id="stopall" style="display:none"
          title="доработают текущие задачи и встанут">■ Стоп всё</button>
  <button class="btn danger sm" id="killall" style="display:none"
          title="бросить текущие задачи прямо сейчас">⏹! Стоп сейчас</button>
  <button class="btn icon" id="gear" title="настройки и справка">⚙</button>
</header>
<main>
<section>
  <div id="gstats"></div>
  <div id="slots"></div>
  <button id="addslot">＋ Новый прогон — свой проект, своя очередь, своя вкладка</button>
</section>
<aside>
  <div class="apanel">
    <h2>Вкладки браузера <span class="pill" id="tabcount">0</span>
        <span class="grow"></span>
        <button class="btn ghost sm" id="rescan" title="перечитать список вкладок">↻</button></h2>
    <div id="atabs"></div>
    <div class="row" style="margin-top:9px">
      <button class="btn sm" id="aaddtab">＋ вкладка Flow</button>
      <span class="mut">каждому прогону — своя</span>
    </div>
  </div>
  <div class="apanel">
    <h2>Лог <span class="grow"></span>
      <select id="logf" style="padding:3px 8px;font-size:12px"><option value="">все</option></select></h2>
    <pre id="log">—</pre>
  </div>
  <div class="apanel">
    <h2>Результаты <span class="pill" id="rescount" style="display:none"></span></h2>
    <div class="gallery" id="gal"></div>
  </div>
</aside>
</main>

<dialog id="dlg">
  <div class="dhead">Настройки <span class="grow"></span>
    <button class="btn ghost sm" id="dclose">✕</button></div>
  <div class="dtabs">
    <button data-t="conn" class="on">Подключение</button>
    <button data-t="soft">Смягчение</button>
    <button data-t="prod">Продукты</button>
    <button data-t="help">Справка</button>
  </div>
  <div class="dbody">
    <div class="dsec on" id="d-conn">
      <div class="row">
        <input type="text" id="endpoint" placeholder="http://localhost:9222">
        <button class="btn" id="saveep">Сохранить и проверить</button>
      </div>
      <div class="note" id="epstatus" style="margin-top:8px"></div>
      <div class="helpgrid" style="margin-top:12px">
        <b>браузер</b><span>кнопка «Запустить браузер» в шапке поднимает твой Edge на
        отдельном профиле с отладочным портом. Логин не автоматизируется: сессия
        берётся из профиля, где ты один раз вошёл сам.</span>
        <b>чужой доступ</b><span>токены не нужны: авторизация — это залогиненный браузер.
        Другой человек запускает свой браузер со своим профилем и указывает здесь порт.</span>
      </div>
    </div>
    <div class="dsec" id="d-soft">
      <div id="softlist"></div>
      <div class="helpgrid" style="margin-top:12px">
        <b>зачем</b><span>если Flow завернул промпт («может нарушать наши правила»),
        программа перепишет его безобиднее и перезапустит задачу. Реплики, имена
        персонажей и бренд не трогаются.</span>
        <b>выбор</b><span>переключатель в шапке. Ollama — локально и бесплатно, сервер
        поднимется сам при выборе. Gemini — по API-ключу из .env. «Авто» берёт первое
        доступное: Ollama → Gemini → Claude → правила.</span>
      </div>
    </div>
    <div class="dsec" id="d-prod">
      <div class="row" id="prodlist" style="gap:6px"></div>
      <div class="helpgrid" style="margin-top:12px">
        <b>как это работает</b><span>подпапка = продукт: <code id="proddir"></code>\rl410\фото.jpg.
        В промпте — <b>@product rl410</b> (все фото) или <b>@product rl410/front.jpg</b> (одно).
        Фото заливаются в проект автоматически, один раз, дальше из кэша.</span>
      </div>
    </div>
    <div class="dsec" id="d-help">
      <div class="helpgrid">
        <b>прогоны</b><span>каждый прогон — отдельный проект Flow в отдельной вкладке браузера.
        До 7 одновременно. Два прогона в одном проекте не разрешаются — результаты
        перепутались бы.</span>
        <b>галочки</b><span>отмеченные строки идут в прогон ровно как отмечены — даже DONE
        (перегенерация). Ничего не отмечено — идут все TODO.</span>
        <b>dry</b><span>репетиция: всё, кроме нажатия «Создать». Генерации не тратятся.</span>
        <b>BATCH</b><span>фильтр по колонке 04_BATCH Excel-очереди.</span>
        <b>лимит</b><span>не больше N строк после фильтров.</span>
        <b>✕ у строки</b><span>убирает строку из очереди панели, файл не меняется.</span>
        <b>паузы</b><span>общие на все прогоны: запросов в минуту столько же, сколько в один
        поток. Ускорение — за счёт ожидания генераций внахлёст.</span>
      </div>
      <div class="hint" id="syntax"></div>
    </div>
  </div>
</dialog>
<div id="toast"></div>
<!-- Подсказка со списком файлов из папки промптов: браузер сам покажет
     их выпадашкой при клике по полю очереди. -->
<datalist id="queuefiles"></datalist>

<script>
const $ = s => document.querySelector(s);
const S = { sel:{}, sort:{}, q:{}, logSlots:'' };
let LAST = null;

const PAL = 7;
const pcol = sid => `var(--p${((sid-1)%PAL)+1})`;

function esc(s){ return (s??'').toString().replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function base(p){ return (p||'').split(/[\\/]/).pop(); }
function mmss(sec){ const m=Math.floor(sec/60),s=sec%60; return m+':'+String(s).padStart(2,'0'); }
function toast(msg, kind){
  const t=$('#toast'); t.textContent=msg; t.className=kind||'err'; t.style.display='block';
  clearTimeout(t._h); t._h=setTimeout(()=>t.style.display='none', kind==='ok'?3500:8000);
}
async function api(path, body){
  const opt = body ? {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)} : {};
  const r = await fetch(path, opt); const j = await r.json();
  if (j.error) throw new Error(j.error);
  return j;
}
async function tick(fresh){
  try { LAST = await api('/api/state'+(fresh?'?fresh=1':'')); render(LAST); } catch(e){}
}

/* ------------------------------------------------------------------ шапка */
function renderHeader(st){
  const ok = st.browser_ok, n = (st.tabs||[]).length;
  $('#bstat').innerHTML = `<span class="dot${st.slots.some(s=>s.running)?' live':''}" style="background:${ok?'var(--ok)':'var(--err)'}"></span>`
    + (ok ? `${esc(st.browser_name||'браузер')} · <b>${n}</b>&nbsp;вкладок` : 'браузер не запущен');
  $('#bbtn').textContent = ok ? '＋ вкладки' : '▶ Запустить браузер';
  const anyRun = st.slots.some(s=>s.running);
  $('#stopall').style.display = anyRun ? '' : 'none';
  $('#killall').style.display = anyRun ? '' : 'none';

  const sb = st.soften||{};
  const sel = $('#soft');
  if (sel.dataset.filled !== '1' && (sb.backends||[]).length){
    sel.innerHTML = '<option value="auto">авто</option>' + sb.backends.map(b=>
      `<option value="${b.id}">${esc(b.title)}${b.available?'':' ✗'}</option>`).join('');
    sel.value = sb.backend||'auto'; sel.dataset.filled='1';
  }
  $('#softdot').style.background = sb.active ? 'var(--ok)' : 'var(--todo)';
  $('#softdot').title = sb.active ? ('сейчас: '+sb.active) : '';
  $('#softlist').innerHTML = (sb.backends||[]).map(b=>
    `<div class="bkrow"><span style="color:${b.available?'var(--ok)':'var(--dim2)'}">${b.available?'✓':'○'}</span>
     <b>${esc(b.title)}</b> — ${esc(b.detail)}</div>`).join('')
    + (sb.note ? `<div class="bkrow" style="margin-top:6px;color:var(--run)">${esc(sb.note)}</div>` : '');
}

/* ----------------------------------------------------------------- сводка */
function renderStats(st){
  const sl = st.slots||[];
  const agg = {};
  sl.forEach(s => Object.entries(s.counts||{}).forEach(([k,v])=>agg[k]=(agg[k]||0)+v));
  const used = sl.reduce((a,s)=>a+(s.tabs||[]).length,0);
  const run = sl.filter(s=>s.running).length;
  const chip = (t,v,c) => `<span class="pill">${t}: <b${c?` style="color:${c}"`:''}>${v}</b></span>`;
  $('#gstats').innerHTML =
    chip('прогонов', sl.length+' / '+st.max_slots)
    + (run ? chip('активно', run, 'var(--run)') : '')
    + chip('вкладок занято', used+' / '+st.total_tab_cap)
    + (agg.DONE ? chip('DONE', agg.DONE, 'var(--ok)') : '')
    + (agg.ERROR ? chip('ERROR', agg.ERROR, 'var(--err)') : '')
    + (agg.TODO ? chip('TODO', agg.TODO) : '');
}

/* ------------------------------------------------------------------ слоты */
function slotTemplate(s){
  const n = s.id;
  return `
  <div class="shead">
    <span class="sbadge">${esc(s.label)}</span>
    <span class="sname" id="s${n}-name"></span>
    <span class="badge s-TODO" id="s${n}-state">ожидает</span>
    <span class="sel-t" id="s${n}-elapsed"></span>
    <span class="grow"></span>
    <label class="chk" title="репетиция без траты генераций"><input type="checkbox" id="s${n}-dry"> dry</label>
    <button class="btn primary sm" id="s${n}-start">▶ Старт</button>
    <button class="btn danger sm" id="s${n}-stop" disabled
            title="мягкая остановка: доработает текущую задачу и встанет">■</button>
    <button class="btn danger sm" id="s${n}-kill" disabled
            title="жёсткая остановка: бросить текущую задачу прямо сейчас">⏹!</button>
    <button class="btn ghost sm" id="s${n}-del" title="убрать прогон из панели">✕</button>
  </div>
  <div class="row">
    <button class="btn sm" id="s${n}-pick" title="выбрать файл где угодно на диске">📂 Файл с диска…</button>
    <input type="file" id="s${n}-file" hidden
           accept=".txt,.text,.prompts,.yaml,.yml,.xlsx,.xlsm">
    <input type="text" id="s${n}-src" list="queuefiles"
           placeholder="или имя файла из папки промптов">
    <button class="btn sm" id="s${n}-load" title="прочитать файл и показать задачи">Разобрать</button>
    <button class="btn ghost sm" id="s${n}-pastebtn">текстом…</button>
  </div>
  <div id="s${n}-paste" hidden>
    <textarea id="s${n}-ptext" spellcheck="false" placeholder="@project Название проекта&#10;&#10;=== IMG K1&#10;Текст промпта...&#10;&#10;=== VID K1_anim&#10;@use K1&#10;@duration 8&#10;Animate this image..."></textarea>
    <div class="row"><button class="btn sm" id="s${n}-parse">Разобрать и загрузить</button>
      <button class="btn ghost sm" id="s${n}-phelp">формат?</button></div>
  </div>
  <div class="tchips" id="s${n}-tabs"></div>
  <div class="row" style="margin-top:9px">
    <select id="s${n}-kind" style="flex:0"><option value="">все типы</option>
      <option value="image">image</option><option value="video">video</option></select>
    <input type="text" id="s${n}-batch" placeholder="BATCH" style="min-width:86px;flex:0">
    <input type="number" id="s${n}-limit" placeholder="лимит" style="width:80px">
    <span class="grow"></span>
    <span class="mut" id="s${n}-refs"></span>
    <button class="btn ghost sm" id="s${n}-delsel" style="display:none">✕ отмеченные</button>
    <button class="btn ghost sm" id="s${n}-tgl">очередь ▾</button>
  </div>
  <div class="pbwrap" id="s${n}-pbw" style="display:none"><div class="pb">
    <span class="ok" id="s${n}-pbok"></span><span class="er" id="s${n}-pber"></span>
    <span class="dr" id="s${n}-pbdr"></span></div></div>
  <div id="s${n}-workers"></div>
  <div class="snote" id="s${n}-note"></div>
  <div class="stail" id="s${n}-tail"></div>
  <div class="qwrap" id="s${n}-qwrap">
    <table><thead><tr>
      <th style="width:24px"><input type="checkbox" class="selall" data-sid="${n}"></th>
      <th class="sortable" data-sid="${n}" data-k="id">ID</th>
      <th class="sortable" data-sid="${n}" data-k="kind">тип</th>
      <th class="sortable" data-sid="${n}" data-k="batch">batch</th>
      <th class="sortable" data-sid="${n}" data-k="duration">сек</th>
      <th class="sortable" data-sid="${n}" data-k="refs">реф</th>
      <th>промпт</th>
      <th class="sortable" data-sid="${n}" data-k="status">статус</th>
      <th style="width:24px"></th>
    </tr></thead><tbody id="s${n}-rows"></tbody></table>
  </div>`;
}

function buildSlot(s, isOnly){
  const el = document.createElement('div');
  el.className='slot'; el.id='slot-'+s.id; el.dataset.sid=s.id;
  el.style.setProperty('--sc', pcol(s.id));
  el.innerHTML = slotTemplate(s);
  S.sel[s.id] = S.sel[s.id] || new Set();
  if (!(s.id in S.q)) S.q[s.id] = isOnly;
  const g = id => el.querySelector('#s'+s.id+'-'+id);
  g('start').onclick = () => slotStart(s.id);
  g('stop').onclick = async () => { try{ await api('/api/slot/stop',{id:s.id}); }catch(e){toast(e.message);} tick(); };
  g('kill').onclick = async () => {
    if (!confirm('Бросить текущую задачу прогона '+s.label+' прямо сейчас?\n\n'
      +'Уже запущенная во Flow генерация доработает, но результат не скачается '
      +'и задача останется в очереди.')) return;
    try{ await api('/api/slot/stop',{id:s.id, force:true}); toast('Останавливаю немедленно','ok'); }
    catch(e){toast(e.message);} tick();
  };
  g('del').onclick = async () => {
    if (!confirm('Убрать прогон '+s.label+' из панели? Файлы очереди не трогаются.')) return;
    try{ await api('/api/slot/remove',{id:s.id}); delete S.sel[s.id]; delete S.q[s.id]; }
    catch(e){toast(e.message);} tick();
  };
  g('load').onclick = async () => {
    try{ S.sel[s.id].clear(); await api('/api/slot/load',{id:s.id, source:g('src').value.trim()});
         toast('Очередь загружена','ok'); }catch(e){toast(e.message);} tick();
  };
  /* Проводник: браузер отдаёт только СОДЕРЖИМОЕ файла, не путь. Поэтому
     копируем файл в папку промптов на стороне сервера и сразу разбираем —
     заодно это работает и с телефона через Tailscale. */
  g('pick').onclick = () => g('file').click();
  g('file').onchange = async (e) => {
    const f = e.target.files && e.target.files[0];
    if (!f) return;
    if (f.size > 8*1024*1024) { toast('Файл больше 8 МБ — это точно очередь?'); return; }
    try{
      const buf = await f.arrayBuffer();
      let bin = ''; const bytes = new Uint8Array(buf);
      for (let i=0;i<bytes.length;i++) bin += String.fromCharCode(bytes[i]);
      S.sel[s.id].clear();
      const r = await api('/api/slot/upload',{id:s.id, name:f.name, data:btoa(bin)});
      g('src').value = r.saved || '';
      toast('Загружен и разобран: '+f.name,'ok');
    }catch(err){ toast(err.message); }
    e.target.value = '';   // тот же файл можно выбрать повторно
    tick();
  };
  g('pastebtn').onclick = () => { g('paste').hidden = !g('paste').hidden; };
  g('parse').onclick = async () => {
    try{ S.sel[s.id].clear(); await api('/api/slot/parse',{id:s.id, text:g('ptext').value});
         g('paste').hidden=true; toast('Разобрано и загружено','ok'); }catch(e){toast(e.message);} tick();
  };
  g('phelp').onclick = () => openDlg('help');
  g('tgl').onclick = () => { S.q[s.id]=!S.q[s.id]; if(LAST) renderSlots(LAST); };
  g('delsel').onclick = async () => {
    try{ await api('/api/slot/rows',{id:s.id, ids:[...S.sel[s.id]]}); S.sel[s.id].clear(); }
    catch(e){toast(e.message);} tick();
  };
  return el;
}

async function slotStart(sid){
  const g = id => document.getElementById('s'+sid+'-'+id);
  const b = g('start'); b.disabled = true;
  try{
    await api('/api/slot/start', {id:sid, source:g('src').value.trim(), kind:g('kind').value,
      batch:g('batch').value.trim(), limit:g('limit').value, dry_run:g('dry').checked,
      selected:[...(S.sel[sid]||[])]});
    toast('Прогон запущен','ok');
  }catch(e){ toast(e.message); b.disabled=false; }
  tick();
}

function sortRows(rows, sk){
  if (!sk || !sk.k) return rows;
  const num = v => typeof v === 'number';
  return rows.slice().sort((a,b)=>{
    let x=a[sk.k], y=b[sk.k];
    if (x==null&&y==null) return 0; if (x==null) return 1; if (y==null) return -1;
    if (num(x)&&num(y)) return (x-y)*sk.d;
    return String(x).localeCompare(String(y),'ru')*sk.d;
  });
}

function updateSlot(el, s, st){
  const g = id => el.querySelector('#s'+s.id+'-'+id);
  g('name').textContent = s.queue_project || base(s.source) || 'очередь не загружена';
  g('name').title = s.source || '';

  const stEl = g('state');
  let cls='s-TODO', txt='ожидает';
  if (s.running){ cls='s-IN_PROGRESS'; txt = s.dry_running ? 'репетиция' : 'идёт'; }
  else if (s.error){ cls='s-ERROR'; txt='ошибка'; }
  else if (s.outcome){ cls='s-DONE'; txt='готово'; }
  stEl.className = 'badge '+cls; stEl.textContent = txt;
  g('elapsed').textContent = s.running ? mmss(s.elapsed) : '';

  g('start').disabled = s.running;
  g('stop').disabled = !s.running;
  g('kill').disabled = !s.running;
  g('del').disabled = s.running;
  g('load').disabled = s.running; g('parse').disabled = s.running;
  const src = g('src');
  if (document.activeElement !== src && !src.value && s.source) src.value = s.source;
  src.disabled = s.running;

  // вкладки-чипы
  const tabs = st.tabs||[];
  g('tabs').innerHTML = '<span class="lbl">вкладки</span>' + (tabs.length ? tabs.map((t,i)=>{
    const mine = (s.tabs||[]).includes(t.id);
    const busy = t.owner && t.owner !== s.id;
    const name = 'вкладка '+(i+1)+(t.project_id ? ' · '+t.project_id.slice(0,8) : ' · без проекта');
    return `<button class="tchip ${mine?'mine':''} ${busy?'busy':''}" data-sid="${s.id}"
      data-tab="${esc(t.id)}" ${s.running||busy?'disabled':''}
      title="${busy?('занята прогоном '+esc(t.owner_label||'')):(mine?'клик — открепить':'клик — выбрать для этого прогона')}">${esc(name)}${busy?' · '+esc(t.owner_label||''):''}</button>`;
  }).join('') : '<span class="mut">нет открытых вкладок — запусти браузер (кнопка в шапке)</span>');

  // прогресс
  const c = s.counts||{}, total = (s.rows||[]).length;
  const done=(c.DONE||0), err=(c.ERROR||0), dr=(c.DRY||0);
  g('pbw').style.display = total ? '' : 'none';
  if (total){
    g('pbok').style.width=(100*done/total)+'%';
    g('pber').style.width=(100*err/total)+'%';
    g('pbdr').style.width=(100*dr/total)+'%';
  }
  g('refs').textContent = (s.refs && (s.refs.uploads||s.refs.reused))
    ? `реф: залито ${s.refs.uploads} · повторно ${s.refs.reused}` : '';

  // воркеры
  g('workers').innerHTML = s.running ? (s.workers||[]).map((w,i)=>`
    <div class="worker"><span class="wl" style="color:${pcol(s.id)}">${esc(w.label)}</span>
      <span class="jid">${w.job_id?esc(w.job_id):'<span class="mut">'+esc(w.status)+'</span>'}</span>
      ${w.elapsed?`<span class="el">${mmss(w.elapsed)}</span>`:''}
      <span class="el" style="color:var(--ok)">${w.done}</span>${w.failed?`<span class="el" style="color:var(--err)">/${w.failed}</span>`:''}
    </div>`).join('') : '';

  // итог / ошибка / скрытые строки
  let note = '';
  if (s.error) note += `<span class="erc">${esc(s.error)}</span> `;
  else if (s.outcome && !s.running) note += `<span class="okc">${esc(s.outcome)}</span> `;
  if (s.removed) note += `<span class="mut">скрыто строк: ${s.removed}
    <a href="#" data-restore="${s.id}" style="color:var(--accent)">вернуть</a></span>`;
  g('note').innerHTML = note;
  g('tail').textContent = s.running ? (s.log_tail||[]).slice(-2).join('\n') : '';

  // таблица
  const open = !!S.q[s.id];
  g('tgl').textContent = open ? 'очередь ▴' : `очередь ▾ (${total})`;
  g('qwrap').style.display = open ? '' : 'none';
  g('delsel').style.display = S.sel[s.id] && S.sel[s.id].size ? '' : 'none';
  if (open){
    const rows = sortRows(s.rows||[], S.sort[s.id]);
    el.querySelectorAll('th.sortable').forEach(th=>{
      const b = th.textContent.replace(/[▲▼]/g,'').trim();
      const sk = S.sort[s.id];
      th.innerHTML = esc(b) + (sk && th.dataset.k===sk.k ? `<span class="arr">${sk.d>0?'▲':'▼'}</span>` : '');
    });
    g('rows').innerHTML = rows.map(r=>`
      <tr class="${r.status==='IN_PROGRESS'?'active':''}">
        <td><input type="checkbox" data-rowsel data-sid="${s.id}" data-id="${esc(r.id)}"
             ${S.sel[s.id].has(r.id)?'checked':''}></td>
        <td class="id">${esc(r.id)}</td>
        <td title="${r.kind}">${r.kind==='video'?'🎬':'🖼'}</td>
        <td class="id" style="color:var(--dim)">${esc(r.batch||'')}</td>
        <td style="color:var(--dim)">${r.duration||''}</td>
        <td title="${esc((r.refs_list||[]).join('\n'))}">${r.refs||''}</td>
        <td><div class="prompt">${esc(r.prompt)}</div>
            ${r.error?`<div class="err">${esc(r.error)}</div>`:''}</td>
        <td><span class="badge s-${r.status}">${r.status}</span></td>
        <td>${s.running?'':`<button class="del" data-sid="${s.id}" data-del="${esc(r.id)}" title="убрать строку">✕</button>`}</td>
      </tr>`).join('') || '<tr><td colspan="9" class="empty">очередь пуста — укажи файл или вставь промпты текстом</td></tr>';
    const all = el.querySelector('.selall');
    const ids = rows.map(r=>r.id);
    all.checked = ids.length>0 && ids.every(id=>S.sel[s.id].has(id));
  }
}

function renderSlots(st){
  const wrap = $('#slots');
  const ids = (st.slots||[]).map(s=>s.id);
  [...wrap.children].forEach(el=>{
    if (!ids.includes(+el.dataset.sid)) el.remove();
  });
  (st.slots||[]).forEach(s=>{
    let el = document.getElementById('slot-'+s.id);
    if (!el){ el = buildSlot(s, (st.slots||[]).length===1); wrap.appendChild(el); }
    updateSlot(el, s, st);
  });
  $('#addslot').style.display = ids.length >= st.max_slots ? 'none' : '';
}

/* ------------------------------------------------------------------ aside */
function renderAside(st){
  const tabs = st.tabs||[];
  $('#tabcount').textContent = tabs.length;
  $('#atabs').innerHTML = tabs.length ? tabs.map((t,i)=>{
    const own = t.owner ? `<span class="own" style="color:${pcol(t.owner)}">${esc(t.owner_label)}</span>` : '<span class="mut">свободна</span>';
    return `<div class="atab"><span class="nm">вкладка ${i+1} · ${t.project_id?esc(t.project_id.slice(0,8)):'без проекта'}</span>${own}</div>`;
  }).join('') : `<div class="note">${st.browser_ok?'нет вкладок Flow':'браузер не запущен — кнопка в шапке'}</div>`;

  // лог-фильтр по прогонам
  const key = (st.slots||[]).map(s=>s.id).join(',');
  const lf = $('#logf');
  if (S.logSlots !== key){
    S.logSlots = key;
    const cur = lf.value;
    lf.innerHTML = '<option value="">все</option>' + (st.slots||[]).map(s=>
      `<option value="${esc(s.label)}">${esc(s.label)}</option>`).join('');
    lf.value = cur;
  }
  const filt = lf.value;
  const lines = (st.log||[]).filter(l => !filt || l.startsWith('['+filt+']'));
  const log = $('#log');
  const stick = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  log.innerHTML = lines.map(l=>{
    const m = l.match(/^\[П(\d+)\]/);
    if (!m) return esc(l);
    return `<span class="lt" style="color:${pcol(+m[1])}">[П${m[1]}]</span>` + esc(l.slice(m[0].length));
  }).join('\n') || '—';
  if (stick) log.scrollTop = log.scrollHeight;

  const res = st.results||[];
  const rc = $('#rescount');
  rc.style.display = res.length?'':'none'; rc.textContent = res.length;
  $('#gal').innerHTML = res.map(f=>{
    const u = '/api/file?path='+encodeURIComponent(f.path);
    return `<a href="${u}" target="_blank">${f.video
      ? `<video src="${u}" muted loop onmouseover="this.play()" onmouseout="this.pause()"></video>`
      : `<img src="${u}" loading="lazy">`}<span title="${esc(f.name)}">${esc(f.name)}</span></a>`;
  }).join('');
}

/* ----------------------------------------------------------------- диалог */
function openDlg(tab){
  $('#dlg').showModal();
  if (tab) switchDlg(tab);
}
function switchDlg(t){
  document.querySelectorAll('.dtabs button').forEach(b=>b.classList.toggle('on', b.dataset.t===t));
  document.querySelectorAll('.dsec').forEach(d=>d.classList.toggle('on', d.id==='d-'+t));
}

function render(st){
  renderHeader(st); renderStats(st); renderSlots(st); renderAside(st);
  if (document.activeElement !== $('#endpoint') && !$('#endpoint').value)
    $('#endpoint').value = st.endpoint || '';
  $('#syntax').textContent = st.syntax_help || '';
  const qf = (st.queue_files || []).map(f=>`<option value="${esc(f)}">`).join('');
  if ($('#queuefiles').innerHTML !== qf) $('#queuefiles').innerHTML = qf;
  $('#proddir').textContent = st.products_dir || 'products';
  $('#prodlist').innerHTML = (st.products||[]).length
    ? st.products.map(p=>`<span class="pill" title="@product ${esc(p.name)}">📦 <b>${esc(p.name)}</b>&nbsp;· ${p.files} фото</span>`).join('')
    : '<span class="pill">пока пусто — создай подпапку с фото продукта</span>';
}

/* ---------------------------------------------------------------- события */
document.body.addEventListener('click', async e=>{
  const p = e.target.closest('.prompt'); if (p){ p.classList.toggle('full'); return; }
  const th = e.target.closest('th.sortable');
  if (th){
    const sid = +th.dataset.sid, k = th.dataset.k, cur = S.sort[sid];
    if (cur && cur.k===k){ if (cur.d===1) cur.d=-1; else delete S.sort[sid]; }
    else S.sort[sid] = {k, d:1};
    if (LAST) renderSlots(LAST); return;
  }
  const chip = e.target.closest('.tchip');
  if (chip && !chip.disabled && !chip.classList.contains('busy')){
    const sid = +chip.dataset.sid, tid = chip.dataset.tab;
    const slot = (LAST.slots||[]).find(s=>s.id===sid); if(!slot) return;
    const cur = new Set(slot.tabs||[]);
    cur.has(tid) ? cur.delete(tid) : cur.add(tid);
    try{ await api('/api/slot/tabs',{id:sid, ids:[...cur]}); }catch(err){ toast(err.message); }
    tick(); return;
  }
  const del = e.target.closest('.del');
  if (del){
    try{ S.sel[+del.dataset.sid]?.delete(del.dataset.del);
         await api('/api/slot/rows',{id:+del.dataset.sid, ids:[del.dataset.del]}); }
    catch(err){ toast(err.message); }
    tick(); return;
  }
  const rst = e.target.closest('[data-restore]');
  if (rst){ e.preventDefault();
    try{ await api('/api/slot/rows',{id:+rst.dataset.restore, reset:true}); }catch(err){ toast(err.message); }
    tick(); return;
  }
  const dt = e.target.closest('.dtabs button');
  if (dt){ switchDlg(dt.dataset.t); return; }
});

document.body.addEventListener('change', e=>{
  if (e.target.matches('input[data-rowsel]')){
    const set = S.sel[+e.target.dataset.sid];
    e.target.checked ? set.add(e.target.dataset.id) : set.delete(e.target.dataset.id);
    if (LAST) renderSlots(LAST);
    return;
  }
  if (e.target.matches('.selall')){
    const sid = +e.target.dataset.sid;
    const slot = (LAST.slots||[]).find(s=>s.id===sid); if(!slot) return;
    const set = S.sel[sid];
    sortRows(slot.rows||[], S.sort[sid]).forEach(r => e.target.checked ? set.add(r.id) : set.delete(r.id));
    renderSlots(LAST);
    return;
  }
  if (e.target.id === 'logf' && LAST) renderAside(LAST);
});

$('#addslot').onclick = async ()=>{ try{ await api('/api/slot/add',{}); }catch(e){ toast(e.message); } tick(); };
$('#stopall').onclick = async ()=>{ try{ await api('/api/stopall',{}); }catch(e){ toast(e.message); } tick(); };
$('#killall').onclick = async ()=>{
  if (!confirm('Бросить текущие задачи ВСЕХ прогонов прямо сейчас?

'
    +'Запущенные во Flow генерации доработают, но результаты не скачаются '
    +'и задачи останутся в очередях.')) return;
  try{ await api('/api/stopall',{force:true}); toast('Останавливаю всё немедленно','ok'); }
  catch(e){ toast(e.message); } tick();
};
$('#rescan').onclick = ()=>{ tick(true); };
$('#aaddtab').onclick = async ()=>{
  try{ await api('/api/browser',{add:1}); toast('Открываю вкладку…','ok'); }catch(e){ toast(e.message); }
  setTimeout(()=>tick(true), 2500);
};
$('#bbtn').onclick = async ()=>{
  const b = $('#bbtn'); b.disabled = true; const old=b.textContent; b.textContent='запускаю…';
  try{
    const r = await api('/api/browser',{tabs:+$('#bcount').value});
    toast('Браузер: '+(r.browser||'ок')+', вкладок Flow '+(r.tabs??'?'),'ok');
  }catch(e){ toast(e.message); }
  b.disabled=false; b.textContent=old;
  setTimeout(()=>tick(true), 1500);
};
$('#soft').onchange = async e=>{
  try{ const r = await api('/api/soften',{backend:e.target.value});
       toast('Смягчение: '+r.active+(r.note?'\n'+r.note:''),'ok'); }
  catch(err){ toast(err.message); }
  tick(true);
};
$('#gear').onclick = ()=>openDlg();
$('#dclose').onclick = ()=>$('#dlg').close();
$('#saveep').onclick = async ()=>{
  const stel = $('#epstatus'); stel.textContent='проверяю…';
  try{
    const r = await api('/api/settings',{endpoint:$('#endpoint').value.trim()});
    stel.textContent = '✓ '+r.browser;
    toast('Подключение сохранено: '+r.browser,'ok');
  }catch(e){ stel.textContent='✗ '+e.message; toast(e.message); }
  tick(true);
};

tick(true);
setInterval(tick, 1500);
</script></body></html>
"""


class LogSink:
    """Приёмник вывода rich.Console: копит строки, умеет пересылать их дальше.

    forward — колбэк на каждую готовую строку; так лог прогона попадает и в
    свою карточку, и в общий журнал панели (уже с меткой [Пn]).
    """

    def __init__(self, maxlen: int = 600, forward: Callable[[str], None] | None = None) -> None:
        self.lines: deque[str] = deque(maxlen=maxlen)
        self._buf = ""
        self._lock = threading.Lock()
        self._forward = forward

    def push(self, line: str) -> None:
        with self._lock:
            self.lines.append(line)

    def write(self, text: str) -> int:
        done: list[str] = []
        with self._lock:
            self._buf += text
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                line = line.rstrip()
                if line:
                    self.lines.append(line)
                    done.append(line)
        # Пересылку делаем вне замка: у глобального приёмника замок свой.
        if self._forward:
            for line in done:
                self._forward(line)
        return len(text)

    def flush(self) -> None:
        return None

    def snapshot(self) -> list[str]:
        with self._lock:
            return list(self.lines)


class RunSlot:
    """Один прогон: очередь, вкладки, проект, поток. Живёт внутри AppState."""

    def __init__(self, sid: int, app: "AppState") -> None:
        self.id = sid
        self.label = f"П{sid}"
        self.app = app
        self.cfg = app.cfg
        self.sink = LogSink(maxlen=300, forward=lambda line: app.sink.push(f"[{self.label}] {line}"))
        self.console = Console(file=self.sink, force_terminal=False, width=100, highlight=False)

        self.source = ""
        self.rows: list[dict[str, Any]] = []
        self.jobs_by_id: dict[str, Any] = {}
        self.statuses: dict[str, str] = {}
        self.errors: dict[str, str] = {}
        self.removed: set[str] = set()
        self.queue_project: str | None = None
        self.sheet: SheetQueue | None = None
        self.sheet_by_id: dict[str, Any] = {}

        self.tabs_selected: list[str] = []
        self.thread: threading.Thread | None = None
        self.parallel: ParallelRunner | None = None
        self.project: str | None = None      # имя проекта после старта
        self.project_id = ""                 # id проекта во время прогона
        self.refs_stat = {"uploads": 0, "reused": 0}
        self.outcome_text = ""
        self.last_error = ""
        self.dry_running = False
        self.started_at = 0.0
        self._skip_completed = False

    # ------------------------------------------------------------- состояние

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def _require_idle(self) -> None:
        if self.running:
            raise RuntimeError(f"{self.label}: прогон идёт — сначала останови его")

    # --------------------------------------------------------------- очередь

    def load_queue(self, source: str) -> None:
        """Прочитать очередь из .xlsx / .yaml / .txt."""
        self._require_idle()
        # Имя без пути ищется в папке промптов — см. Config.resolve_queue.
        p = self.cfg.resolve_queue(source)
        # Храним разрешённый путь: иначе после переезда файлов в prompts/
        # сохранённая настройка прогона осталась бы указывать в пустоту.
        self.source = str(p)
        self.errors = {}
        self.removed = set()
        self.queue_project = None
        self.outcome_text = ""
        self.last_error = ""
        suffix = p.suffix.lower()

        if suffix in (".xlsx", ".xlsm"):
            self.sheet = SheetQueue(p)
            sheet_rows = self.sheet.load(statuses=("TODO", "IN_PROGRESS", "DONE", "ERROR", "SKIP"))
            self.sheet_by_id = {r.job.id: r for r in sheet_rows}
            self.jobs_by_id = {r.job.id: r.job for r in sheet_rows}
            self.rows = [self._row_dict(r.job, batch=r.batch) for r in sheet_rows]
            self.statuses = {r.job.id: r.status or "TODO" for r in sheet_rows}
            self.queue_project = self.sheet.project_name
        else:
            self.sheet = None
            self.sheet_by_id = {}
            if suffix in (".txt", ".text", ".prompts"):
                jobs, errors, meta = parse_prompts(
                    p.read_text(encoding="utf-8-sig"),
                    self.cfg.out_dir(), self.cfg.products_dir(),
                )
                if errors:
                    raise ValueError("ошибки разбора промптов:\n" + "\n".join(f"• {e}" for e in errors))
                self.queue_project = meta.get("project")
            else:
                jobs = load_jobs(p)
            self.jobs_by_id = {j.id: j for j in jobs}
            self.rows = [self._row_dict(j) for j in jobs]
            if self.queue_project:
                # Что реально сделано в проекте, выяснит прогон при старте.
                self.statuses = {j.id: "TODO" for j in jobs}
            else:
                done = self.app.log.completed_ids()
                self.statuses = {j.id: (ST_DONE if j.id in done else "TODO") for j in jobs}

        self.console.print(
            f"очередь загружена: {len(self.rows)} строк из {p.name}"
            + (f" (проект: {self.queue_project})" if self.queue_project else "")
        )
        self.app.save_ui()

    # Что принимаем из проводника. Всё прочее — отказ: панель может быть
    # открыта из сети, и запись произвольных файлов на диск недопустима.
    UPLOAD_EXTS = {".txt", ".text", ".prompts", ".yaml", ".yml", ".xlsx", ".xlsm"}
    UPLOAD_MAX = 8 * 1024 * 1024

    def upload_queue(self, name: str, data_b64: str) -> str:
        """Сохранить выбранный в проводнике файл в папку промптов и разобрать.

        Браузер отдаёт содержимое, а не путь, — поэтому файл именно копируется
        к себе. Заодно он появляется в списке папки и переживает перезапуск.
        """
        self._require_idle()
        # Только имя файла: '..', слэши и абсолютные пути из name вырезаются,
        # иначе через него можно было бы писать куда угодно.
        safe = Path(str(name or "")).name.strip()
        if not safe:
            raise ValueError("файл без имени")
        if Path(safe).suffix.lower() not in self.UPLOAD_EXTS:
            raise ValueError(
                f"формат {Path(safe).suffix!r} не поддерживается; "
                f"нужен один из: {', '.join(sorted(self.UPLOAD_EXTS))}"
            )
        try:
            blob = base64.b64decode(data_b64 or "", validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("файл не удалось прочитать") from exc
        if not blob:
            raise ValueError("файл пустой")
        if len(blob) > self.UPLOAD_MAX:
            raise ValueError(f"файл больше {self.UPLOAD_MAX // 1024 // 1024} МБ")

        folder = self.cfg.prompts_dir()
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / safe
        # Одноимённый файл не затираем молча: старая очередь может быть нужна.
        if dest.exists() and dest.read_bytes() != blob:
            stem, suf = dest.stem, dest.suffix
            n = 2
            while (folder / f"{stem}_{n}{suf}").exists():
                n += 1
            dest = folder / f"{stem}_{n}{suf}"
        dest.write_bytes(blob)
        self.console.print(f"файл принят: {dest}")
        self.load_queue(str(dest))
        return str(dest)

    def parse_text(self, text: str) -> None:
        """Разобрать вставленный текст. Файл свой на каждый прогон."""
        self._require_idle()
        if not (text or "").strip():
            raise ValueError("пустой текст — нечего разбирать")
        _, errors, _ = parse_prompts(text, self.cfg.out_dir(), self.cfg.products_dir())
        if errors:
            raise ValueError("ошибки разбора:\n" + "\n".join(f"• {e}" for e in errors))
        folder = self.cfg.prompts_dir()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"pasted_{self.id}.flow.txt"
        path.write_text(text, encoding="utf-8")
        self.load_queue(str(path))

    def remove_rows(self, ids: list[str] | None, reset: bool = False) -> None:
        self._require_idle()
        if reset:
            self.removed = set()
            return
        for rid in ids or []:
            if rid in self.jobs_by_id:
                self.removed.add(rid)

    @staticmethod
    def _row_dict(job: Any, batch: str = "") -> dict[str, Any]:
        def pretty(ref: str) -> str:
            if ref.startswith("lib:"):
                name, project = parse_lib_spec(ref)
                return f"библиотека: {name}" + (f" (проект {project})" if project else "")
            if ref.startswith("use:"):
                return f"результат задачи {ref[4:]}"
            return ref

        return {
            "id": job.id,
            "kind": job.kind,
            "batch": batch,
            "duration": job.duration,
            "refs": len(job.refs),
            "refs_list": [pretty(str(r)) for r in job.refs],
            "out": job.out_stem if (job.output_name or "") else "",
            "prompt": job.prompt[:300],
        }

    # --------------------------------------------------------------- вкладки

    def set_tabs(self, ids: list[str]) -> None:
        self._require_idle()
        ids = [str(i) for i in (ids or []) if str(i).strip()]
        if len(ids) > MAX_TABS:
            raise ValueError(f"на один проект — не больше {MAX_TABS} вкладок")
        used = self.app.tabs_used_elsewhere(self.id)
        clash = [i for i in ids if i in used]
        if clash:
            raise ValueError("эта вкладка уже выбрана другим прогоном")
        total = sum(len(s.tabs_selected) for s in self.app.slots if s.id != self.id) + len(ids)
        if total > TOTAL_TAB_CAP:
            raise ValueError(f"суммарно больше {TOTAL_TAB_CAP} вкладок нельзя")
        self.tabs_selected = ids

    # ---------------------------------------------------------------- прогон

    def start(self, opts: dict[str, Any]) -> None:
        self._require_idle()
        src = (opts.get("source") or self.source or "").strip()
        if src != self.source or not self.rows:
            self.load_queue(src)

        selected = {s for s in (opts.get("selected") or []) if s}
        kind = (opts.get("kind") or "").strip()
        batch = (opts.get("batch") or "").strip()
        limit = int(opts["limit"]) if str(opts.get("limit") or "").strip() else None

        chosen: list[str] = []
        for row in self.rows:
            if row["id"] in self.removed:
                continue
            if kind and row["kind"] != kind:
                continue
            if batch and (row["batch"] or "").upper() != batch.upper():
                continue
            if selected:
                if row["id"] in selected:
                    chosen.append(row["id"])
            elif self.statuses.get(row["id"]) == "TODO":
                chosen.append(row["id"])
        if limit is not None:
            if limit < 1:
                raise ValueError("лимит должен быть >= 1")
            chosen = chosen[:limit]
        if not chosen:
            raise ValueError(
                "нечего запускать: отметь строки галочками или проверь фильтры "
                "(без галочек идут только TODO)"
            )

        # Вкладки: выбранные, а если пусто — первая свободная.
        live = {t["id"] for t in self.app.tabs()}
        if not live:
            raise RuntimeError(
                "браузер не запущен или в нём нет вкладок Flow — "
                "кнопка «Запустить браузер» в шапке"
            )
        used = self.app.tabs_used_elsewhere(self.id)
        tab_ids = [t for t in self.tabs_selected if t]
        if tab_ids:
            dead = [t for t in tab_ids if t not in live]
            if dead:
                raise RuntimeError("выбранная вкладка закрыта — обнови выбор (значки вкладок)")
            clash = [t for t in tab_ids if t in used]
            if clash:
                raise RuntimeError("выбранная вкладка занята другим прогоном")
        else:
            free = [t["id"] for t in self.app.tabs() if t["id"] not in used]
            if not free:
                raise RuntimeError("нет свободной вкладки Flow — «＋ вкладка Flow» справа")
            tab_ids = [free[0]]
            self.tabs_selected = tab_ids
            self.console.print("вкладка не выбрана — беру первую свободную")

        jobs = [self.jobs_by_id[cid] for cid in chosen]
        for cid in chosen:
            self.statuses[cid] = "TODO"
            self.errors.pop(cid, None)
        self.refs_stat = {"uploads": 0, "reused": 0}
        self._skip_completed = not selected and self.sheet is None

        dry = bool(opts.get("dry_run"))
        self.dry_running = dry
        self.outcome_text = ""
        self.last_error = ""
        self.started_at = time.time()
        self.thread = threading.Thread(
            target=self._run, args=(jobs, dry, tab_ids), daemon=True,
            name=f"flowbatch-slot-{self.id}",
        )
        self.thread.start()

    def stop(self, force: bool = False) -> None:
        if self.parallel is not None:
            self.parallel.stop(force=force)
            self.console.print(
                "[bold red]СТОП СЕЙЧАС: бросаю текущие задачи[/bold red]" if force
                else "[yellow]запрошена остановка — доработаю текущую задачу[/yellow]"
            )

    def _apply_resume(self, jobs: list[Any], project_id: str, dry: bool) -> list[Any]:
        if not (self._skip_completed and not dry):
            return jobs
        done = self.app.log.completed_ids(project=project_id)
        skipped = [j for j in jobs if j.id in done]
        rest = [j for j in jobs if j.id not in done]
        for j in skipped:
            self.statuses[j.id] = ST_DONE
        if skipped:
            self.console.print(f"резюм: в этом проекте уже сделано {len(skipped)}")
        return rest

    def _run(self, jobs: list[Any], dry: bool, tab_ids: list[str]) -> None:
        try:
            self.app.prepare_soften(self.console)

            # Разведка одним подключением: проект, конфликт, резюм.
            probe = FlowClient(self.cfg, target_id=tab_ids[0])
            probe.bring_to_front = False
            try:
                probe.connect()
            except FlowClientError as exc:
                self.last_error = "нет связи с браузером"
                self.console.print(f"[red]{exc}[/red]")
                return
            try:
                if self.queue_project:
                    with self.app.project_lock:
                        how = probe.ensure_project(self.queue_project)
                    verb = {"current": "уже открыт", "opened": "открыт", "created": "создан"}[how]
                    self.console.print(f"проект {self.queue_project!r} {verb}")
                self.project = probe.project_name() or probe.current_project_id()
                pid = probe.current_project_id() or ""
                if not pid:
                    self.last_error = "во вкладке открыт список проектов, а не проект"
                    self.console.print(f"[red]{self.last_error}[/red]")
                    return
                other = self.app.project_conflict(self.id, pid)
                if other:
                    self.last_error = f"этот проект уже гоняет {other} — два прогона в одном проекте нельзя"
                    self.console.print(f"[red]{self.last_error}[/red]")
                    return
                self.project_id = pid
                jobs = self._apply_resume(jobs, pid, dry)
            finally:
                probe.close()

            if not jobs:
                self.outcome_text = "все выбранные задачи уже выполнены в этом проекте"
                self.console.print(f"[yellow]{self.outcome_text}[/yellow]")
                return

            refs_lock = threading.Lock()
            resolvers: list[RefResolver] = []

            def resolver_factory(client: Any, pid2: str) -> RefResolver:
                r = RefResolver(
                    client, self.app.log, self.app.refcache, pid2,
                    on_upload=lambda p: self.console.print(f"  заливаю в библиотеку: {p.name}"),
                    lock=refs_lock,
                )
                resolvers.append(r)
                return r

            def on_status(job: Any, status: str, result_path: str | None = None,
                          error: str | None = None) -> None:
                self.statuses[job.id] = STATUS_DISPLAY.get(status, status)
                if error:
                    self.errors[job.id] = error
                self.refs_stat = {
                    "uploads": sum(r.uploads for r in resolvers),
                    "reused": sum(r.reused for r in resolvers),
                }
                if self.sheet is not None and status in ("ok", "failed"):
                    row = self.sheet_by_id.get(job.id)
                    if row is not None:
                        self.sheet.write_result(
                            row,
                            ST_DONE if status == "ok" else ST_ERROR,
                            result_path=result_path,
                            bump_attempts=(status == "failed"),
                            note=error,
                        )

            softener_factory = lambda: build_softener(  # noqa: E731
                self.cfg, log=lambda s: self.console.print(f"[dim]{s}[/dim]")
            )
            sof = softener_factory()
            if sof is not None:
                self.console.print(f"[dim]смягчение при модерации: {sof.name}[/dim]")

            self.parallel = ParallelRunner(
                self.cfg, tab_ids, self.app.notifier, self.app.log, self.console,
                dry_run=dry, on_status=on_status,
                softener_factory=softener_factory, resolver_factory=resolver_factory,
                queue_project=self.queue_project,
                pacer=self.app.pacer, project_lock=self.app.project_lock,
                bring_front=False,
            )
            outcome = self.parallel.run(jobs)
            self.outcome_text = (
                f"сделано {outcome.done} из {outcome.total}"
                + (f", провалено {outcome.failed}" if outcome.failed else "")
                + f" · {outcome.elapsed_sec / 60:.1f} мин"
            )
            self.console.print(f"[bold]Итог:[/bold] {self.outcome_text}")
            if outcome.stopped_reason:
                self.console.print(f"[red]{outcome.stopped_reason}[/red]")
                self.last_error = outcome.stopped_reason
            # Квота и «подозрительная активность» бьют по всему аккаунту —
            # глушим и остальные прогоны, каждый доработает текущую задачу.
            if outcome.stop_kind in STOP_QUEUE:
                self.app.stop_all(f"{self.label}: {outcome.stopped_reason}", except_id=self.id)
        except Exception as exc:  # noqa: BLE001 — падение прогона не роняет панель
            self.last_error = str(exc)[:300]
            self.console.print(f"[red]прогон упал: {exc}[/red]")
        finally:
            self.parallel = None
            self.project_id = ""
            self.dry_running = False

    # ------------------------------------------------------------------ вид

    def snapshot(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        rows = []
        for r in self.rows:
            if r["id"] in self.removed:
                continue
            st = self.statuses.get(r["id"], "TODO")
            counts[st] = counts.get(st, 0) + 1
            rows.append({**r, "status": st, "error": self.errors.get(r["id"])})
        return {
            "id": self.id,
            "label": self.label,
            "running": self.running,
            "dry_running": self.dry_running,
            "source": self.source,
            "queue_project": self.queue_project,
            "project": self.project,
            "tabs": self.tabs_selected,
            "rows": rows,
            "counts": counts,
            "removed": len(self.removed),
            "workers": self.parallel.tab_states() if self.parallel is not None else [],
            "outcome": self.outcome_text,
            "error": self.last_error,
            "elapsed": int(time.time() - self.started_at) if self.running else 0,
            "refs": self.refs_stat,
            "log_tail": list(self.sink.lines)[-3:],
        }


class AppState:
    """Панель целиком: слоты прогонов + всё общее между ними."""

    def __init__(self, cfg: Config, default_source: str, saved: dict[str, Any] | None = None) -> None:
        self.cfg = cfg
        self.sink = LogSink(maxlen=900)
        self.log = RunLog(cfg.runs_log())
        self.notifier = Notifier()
        # Общее на все прогоны: ритм пауз, замок создания проектов, кэш
        # референсов. Смысл каждого — в докстринге модуля.
        self.pacer = Pacer(cfg)
        self.project_lock = threading.Lock()
        self.refcache = RefCache(cfg.runs_log().parent / ".flowbatch_refcache.json")

        self.slots: list[RunSlot] = []
        self._next_sid = 1
        self._tabs_cache: list[dict[str, Any]] = []
        self._tabs_error = ""
        self._tabs_at = 0.0
        self._browser_name = ""
        # (когда, статусы бэкендов, имя активного). Активный кэшируется вместе
        # со статусами: его вычисление тоже ходит в Ollama по сети.
        self._soften_cache: tuple[float, list[dict[str, Any]], str | None] = (0.0, [], None)
        self._soften_note = ""

        sources = (saved or {}).get("slots") or [default_source]
        for src in sources[:MAX_SLOTS]:
            slot = self.add_slot(save=False)
            if src:
                try:
                    slot.load_queue(str(src))
                except Exception as exc:  # noqa: BLE001 — панель должна открыться всегда
                    slot.console.print(f"[yellow]очередь не загружена: {exc}[/yellow]")

    # ------------------------------------------------------------------ слоты

    def add_slot(self, save: bool = True) -> RunSlot:
        if len(self.slots) >= MAX_SLOTS:
            raise ValueError(f"больше {MAX_SLOTS} прогонов нельзя")
        slot = RunSlot(self._next_sid, self)
        self._next_sid += 1
        self.slots.append(slot)
        if save:
            self.save_ui()
        return slot

    def slot(self, sid: Any) -> RunSlot:
        for s in self.slots:
            if s.id == int(sid):
                return s
        raise ValueError(f"нет прогона с id={sid}")

    def remove_slot(self, sid: Any) -> None:
        s = self.slot(sid)
        if s.running:
            raise RuntimeError(f"{s.label}: сначала останови прогон")
        self.slots.remove(s)
        if not self.slots:
            self.add_slot(save=False)
        self.save_ui()

    def stop_all(self, reason: str = "", except_id: int | None = None,
                 force: bool = False) -> None:
        for s in self.slots:
            if s.running and s.id != except_id:
                s.stop(force=force)
        if reason:
            self.sink.push(f"[глобально] остановка всех прогонов: {reason}")

    def tabs_used_elsewhere(self, sid: int) -> set[str]:
        """Вкладки, занятые другими слотами (выбор = бронь, прогон = тем более)."""
        return {t for s in self.slots if s.id != sid for t in s.tabs_selected}

    def project_conflict(self, sid: int, project_id: str) -> str | None:
        """Метка чужого работающего прогона в том же проекте, если есть."""
        for s in self.slots:
            if s.id != sid and s.running and s.project_id == project_id:
                return s.label
        return None

    # ---------------------------------------------------------------- вкладки

    def tabs(self, max_age: float = 3.0) -> list[dict[str, Any]]:
        now = time.time()
        if now - self._tabs_at < max_age and (self._tabs_cache or self._tabs_error):
            return self._tabs_cache
        self._tabs_at = now
        endpoint = str(self.cfg.get("cdp.endpoint", "http://localhost:9222"))
        try:
            self._tabs_cache = list_flow_tabs(
                endpoint, str(self.cfg.get("cdp.page_url_match", "labs.google/fx"))
            )
            self._tabs_error = ""
            if not self._browser_name:
                self._browser_name = browser_version(endpoint) or ""
        except Exception as exc:  # noqa: BLE001 — браузер может быть выключен
            self._tabs_cache = []
            self._tabs_error = f"{type(exc).__name__}: браузер не отвечает"
            self._browser_name = ""
        # Мёртвые вкладки не должны оставаться выбранными у простаивающих слотов.
        alive = {t["id"] for t in self._tabs_cache}
        if self._tabs_cache:
            for s in self.slots:
                if not s.running:
                    s.tabs_selected = [t for t in s.tabs_selected if t in alive]
        return self._tabs_cache

    def launch_browser(self, tabs: int = 0, add: int = 0) -> dict[str, Any]:
        endpoint = str(self.cfg.get("cdp.endpoint", "http://localhost:9222"))
        say = lambda s: self.sink.push(f"[браузер] {s}")  # noqa: E731
        if add:
            n = open_more_tabs(endpoint, int(add),
                               url=str(self.cfg.get("cdp.start_url", "") or "") or None, say=say)
            self._tabs_at = 0.0
            if not n:
                raise BrowserError("не смог открыть вкладку — браузер не отвечает?")
            return {"added": n}
        r = launch(self.cfg, tabs=int(tabs or 1), on_step=say)
        self._tabs_at = 0.0
        self.sink.push(f"[браузер] {r['browser']}, вкладок Flow {r['tabs']}, профиль {r['profile']}")
        return r

    # -------------------------------------------------------------- смягчение

    def prepare_soften(self, console: Console) -> None:
        """Перед прогоном: если выбран Ollama (или авто) — поднять сервер."""
        backend = str(self.cfg.get("moderation.soften.backend", "auto")).strip().lower()
        if backend not in ("auto", "ollama"):
            return
        st = ensure_ollama(self.cfg, on_step=lambda s: console.print(f"[dim]{s}[/dim]"))
        if st.get("started") or not st.get("running") or not st.get("model_present"):
            console.print(f"[dim]{st['note']}[/dim]")
        self._soften_cache = (0.0, [], None)  # статус мог поменяться

    def set_soften_backend(self, backend: str) -> dict[str, Any]:
        backend = (backend or "auto").strip().lower()
        allowed = {"auto", "rules", "ollama", "gemini", "claude"}
        if backend not in allowed:
            raise ValueError(f"неизвестный бэкенд {backend!r}")
        self.cfg.set("moderation.soften.backend", backend)
        self.save_ui()
        note = ""
        if backend in ("auto", "ollama"):
            st = ensure_ollama(self.cfg, on_step=lambda s: self.sink.push(f"[смягчение] {s}"))
            note = st["note"]
        self._soften_note = note
        self._soften_cache = (0.0, [], None)
        s = build_softener(self.cfg)
        active = s.name if s else "выключено"
        self.sink.push(f"[смягчение] выбран {backend}, работает: {active}")
        return {"active": active, "note": note}

    def soften_state(self, fresh: bool = False) -> dict[str, Any]:
        now = time.time()
        ts, cached, active = self._soften_cache
        if fresh or now - ts > 10 or not cached:
            cached = backend_status(self.cfg)
            # build_softener в режиме auto опрашивает Ollama по HTTP. Панель
            # дёргает snapshot раз в 1.5с, поэтому без кэша каждый тик стоил
            # бы пары секунд и опросы наслаивались бы друг на друга.
            s = build_softener(self.cfg)
            active = s.name if s else None
            self._soften_cache = (now, cached, active)
        return {
            "backend": str(self.cfg.get("moderation.soften.backend", "auto")),
            "active": active,
            "backends": cached,
            "note": self._soften_note,
        }

    # -------------------------------------------------------------- настройки

    def set_endpoint(self, endpoint: str) -> str:
        if any(s.running for s in self.slots):
            raise RuntimeError("идёт прогон — менять подключение сейчас нельзя")
        endpoint = (endpoint or "").strip().rstrip("/")
        if not endpoint.startswith(("http://", "https://")):
            raise ValueError("endpoint должен начинаться с http:// (например http://localhost:9222)")
        try:
            r = httpx.get(f"{endpoint}/json/version", timeout=4)
            browser = r.json().get("Browser", "неизвестный браузер")
        except Exception as exc:  # noqa: BLE001
            raise ValueError(
                f"браузер на {endpoint} не отвечает: {type(exc).__name__}. "
                "Запусти его с --remote-debugging-port и НЕдефолтным --user-data-dir."
            ) from exc
        self.cfg.set("cdp.endpoint", endpoint)
        self._tabs_at = 0.0
        self._browser_name = ""
        self.save_ui()
        return browser

    def save_ui(self) -> None:
        """Панельные настройки на диск: endpoint, бэкенд смягчения, источники слотов."""
        data: dict[str, Any] = {}
        try:
            data = json.loads(Path(UI_FILE).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        data.update({
            "endpoint": self.cfg.get("cdp.endpoint"),
            "soften_backend": self.cfg.get("moderation.soften.backend"),
            "slots": [s.source for s in self.slots],
        })
        try:
            Path(UI_FILE).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------------- вид

    def queue_files(self) -> list[str]:
        """Имена файлов очередей в папке промптов — для подсказки в панели."""
        folder = self.cfg.prompts_dir()
        if not folder.is_dir():
            return []
        exts = {".txt", ".text", ".prompts", ".yaml", ".yml", ".xlsx", ".xlsm"}
        return sorted(
            (f.name for f in folder.iterdir() if f.is_file() and f.suffix.lower() in exts),
            key=str.lower,
        )

    def products(self) -> list[dict[str, Any]]:
        base = self.cfg.products_dir()
        if not base.is_dir():
            return []
        from .promptfile import PRODUCT_IMAGE_EXTS

        out = []
        for d in sorted(base.iterdir(), key=lambda p: p.name.lower()):
            if not d.is_dir():
                continue
            n = sum(1 for f in d.iterdir()
                    if f.is_file() and f.suffix.lower() in PRODUCT_IMAGE_EXTS)
            out.append({"name": d.name, "files": n})
        return out

    def results(self) -> list[dict[str, Any]]:
        out = self.cfg.out_dir()
        if not out.exists():
            return []
        files = sorted(
            (f for f in out.iterdir() if f.is_file()),
            key=lambda f: f.stat().st_mtime, reverse=True,
        )[:40]
        return [
            {"name": f.name, "path": str(f.resolve()),
             "video": f.suffix.lower() in (".mp4", ".webm", ".mov")}
            for f in files
        ]

    def snapshot(self, fresh: bool = False) -> dict[str, Any]:
        tabs = self.tabs(max_age=0.0 if fresh else 3.0)
        owner: dict[str, RunSlot] = {}
        for s in self.slots:
            for t in s.tabs_selected:
                owner[t] = s
        return {
            "max_slots": MAX_SLOTS,
            "max_tabs": MAX_TABS,
            "total_tab_cap": TOTAL_TAB_CAP,
            "browser_ok": not self._tabs_error,
            "browser_name": self._browser_name,
            "endpoint": self.cfg.get("cdp.endpoint"),
            "tabs": [
                {**t,
                 "owner": owner[t["id"]].id if t["id"] in owner else None,
                 "owner_label": owner[t["id"]].label if t["id"] in owner else ""}
                for t in tabs
            ],
            "tabs_error": self._tabs_error,
            "slots": [s.snapshot() for s in self.slots],
            "log": self.sink.snapshot()[-300:],
            "results": self.results(),
            "products": self.products(),
            "products_dir": str(self.cfg.products_dir()),
            "prompts_dir": str(self.cfg.prompts_dir()),
            "queue_files": self.queue_files(),
            "soften": self.soften_state(fresh=fresh),
            "syntax_help": SYNTAX_HELP,
        }


def make_handler(state: AppState, token: str = "") -> type[BaseHTTPRequestHandler]:
    out_dir = state.cfg.out_dir().resolve()
    shots_dir = state.cfg.screenshots_dir().resolve()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a: Any) -> None:  # noqa: A003 — глушим лог сервера
            return

        # ------------------------------------------------------------- доступ

        def _token_ok(self) -> bool:
            """Токен из заголовка, cookie или ?token= в ссылке.

            compare_digest, а не ==: сравнение строк выходит раньше на первом
            несовпавшем символе, и по времени ответа токен подбирается побайтно.
            """
            if not token:
                return True
            given = [
                self.headers.get("X-Flowbatch-Token") or "",
                parse_qs(urlparse(self.path).query).get("token", [""])[0],
            ]
            for part in (self.headers.get("Cookie") or "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == "fb_token":
                    given.append(value)
            return any(secrets.compare_digest(g, token) for g in given if g)

        def _origin_ok(self) -> bool:
            """Защита от CSRF: чужая страница не должна дёргать наши POST.

            Браузер сам проставляет Origin на межсайтовых запросах и подделать
            его со стороны страницы нельзя. Пустой Origin — это curl или наш
            же запрос, там решает токен.
            """
            origin = self.headers.get("Origin")
            if not origin:
                return True
            return urlparse(origin).netloc == (self.headers.get("Host") or "")

        def _deny(self) -> None:
            self._json({"error": "нужен токен доступа — открой панель по ссылке с ?token=…"}, 403)

        def _send(self, code: int, body: bytes, ctype: str,
                  extra: list[tuple[str, str]] | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in extra or []:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj: Any, code: int = 200) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802
            u = urlparse(self.path)
            if not self._token_ok():
                self._deny()
                return
            # Токен пришёл ссылкой — перекладываем его в cookie и убираем из
            # адреса, чтобы он не остался в истории браузера и в закладках.
            if token and u.path in ("/", "/index.html") and "token=" in (u.query or ""):
                self._send(
                    302, b"", "text/plain",
                    extra=[("Location", "/"),
                           ("Set-Cookie", f"fb_token={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000")],
                )
                return
            if u.path in ("/", "/index.html"):
                self._send(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
            elif u.path == "/api/state":
                self._json(state.snapshot(fresh=bool(parse_qs(u.query).get("fresh"))))
            elif u.path == "/api/file":
                self._serve_file(parse_qs(u.query).get("path", [""])[0])
            else:
                self._json({"error": "not found"}, 404)

        def _serve_file(self, raw: str) -> None:
            """Только из out/ и screenshots/ — иначе path читал бы весь диск."""
            if not raw:
                self._json({"error": "no path"}, 400)
                return
            try:
                p = Path(raw).resolve()
            except OSError:
                self._json({"error": "bad path"}, 400)
                return
            if not any(p.is_relative_to(d) for d in (out_dir, shots_dir)):
                self._json({"error": "запрещено: файл вне out/ и screenshots/"}, 403)
                return
            if not p.is_file():
                self._json({"error": "not found"}, 404)
                return
            ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            self._send(200, p.read_bytes(), ctype)

        def do_POST(self) -> None:  # noqa: N802
            u = urlparse(self.path)
            if not self._token_ok() or not self._origin_ok():
                self._deny()
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                payload = {}
            try:
                if u.path == "/api/slot/add":
                    slot = state.add_slot()
                    self._json({"ok": True, "id": slot.id})
                elif u.path == "/api/slot/remove":
                    state.remove_slot(payload.get("id"))
                    self._json({"ok": True})
                elif u.path == "/api/slot/load":
                    state.slot(payload.get("id")).load_queue(payload.get("source") or "")
                    self._json({"ok": True})
                elif u.path == "/api/slot/upload":
                    saved = state.slot(payload.get("id")).upload_queue(
                        payload.get("name") or "", payload.get("data") or "")
                    self._json({"ok": True, "saved": saved})
                elif u.path == "/api/slot/parse":
                    state.slot(payload.get("id")).parse_text(payload.get("text") or "")
                    self._json({"ok": True})
                elif u.path == "/api/slot/rows":
                    state.slot(payload.get("id")).remove_rows(
                        payload.get("ids"), reset=bool(payload.get("reset")))
                    self._json({"ok": True})
                elif u.path == "/api/slot/tabs":
                    state.slot(payload.get("id")).set_tabs(payload.get("ids") or [])
                    self._json({"ok": True})
                elif u.path == "/api/slot/start":
                    state.slot(payload.get("id")).start(payload)
                    self._json({"ok": True})
                elif u.path == "/api/slot/stop":
                    state.slot(payload.get("id")).stop(force=bool(payload.get("force")))
                    self._json({"ok": True})
                elif u.path == "/api/stopall":
                    state.stop_all("по кнопке «Стоп всё»", force=bool(payload.get("force")))
                    self._json({"ok": True})
                elif u.path == "/api/browser":
                    self._json({"ok": True, **state.launch_browser(
                        tabs=payload.get("tabs") or 0, add=payload.get("add") or 0)})
                elif u.path == "/api/soften":
                    self._json({"ok": True,
                                **state.set_soften_backend(payload.get("backend") or "auto")})
                elif u.path == "/api/settings":
                    browser = state.set_endpoint(payload.get("endpoint") or "")
                    self._json({"ok": True, "browser": browser})
                else:
                    self._json({"error": "not found"}, 404)
            except Exception as exc:  # noqa: BLE001 — ошибку показываем в панели
                self._json({"error": str(exc)}, 400)

    return Handler


def _send_access_link(notifier: Notifier, url: str) -> None:
    """Отправить ссылку на панель в Telegram (в фоне, чтобы не тормозить старт).

    Ссылка содержит токен — то есть это ключ от аккаунта Flow целиком. Уходит
    только в личный чат из .env; в консоли и в runs.jsonl токен как был, так и
    остаётся невидимым.
    """
    if not notifier.enabled:
        print("  Telegram не настроен (нет TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID в .env) — "
              "ссылку не отправляю")
        return

    machine = os.environ.get("COMPUTERNAME") or platform.node() or "этот компьютер"
    text = (
        f"🔗 <b>Панель flowbatch запущена</b> на «{html.escape(machine)}»\n\n"
        f"{html.escape(url)}\n\n"
        "Открывается только с устройств твоей сети Tailscale. "
        "Ссылка содержит токен доступа — по ней пускают без пароля, "
        "поэтому никому её не пересылай."
    )

    def worker() -> None:
        # Повторы не для красоты: api.telegram.org из этой сети отвечает через
        # раз, и одиночная попытка регулярно ловит ConnectTimeout. Без ретраев
        # ссылка просто не доедет, а узнаешь об этом уже с телефона.
        for pause in (0, 5, 15, 40):
            if pause:
                time.sleep(pause)
            if notifier.send(text):
                print("  ссылка отправлена в Telegram")
                return
        print(f"  не удалось отправить ссылку в Telegram: {notifier.last_error}")
        print(f"  открой вручную: {url}")

    threading.Thread(target=worker, daemon=True).start()


def serve(cfg: Config, source: str, port: int = 8765, open_browser: bool = True,
          host: str = "127.0.0.1", token: str | None = None,
          notify_link: bool = True) -> None:
    """Запустить панель.

    host — какой интерфейс слушать. По умолчанию 127.0.0.1: панель видна
    только с этой машины. Значение "tailscale" подставляет IP машины в
    Tailscale, и тогда панель доступна устройствам сети — но ТОЛЬКО им,
    а не всему Wi-Fi, в отличие от 0.0.0.0.

    token — общий секрет. Пустая строка выключает проверку; None означает
    «включить автоматически, если слушаем не loopback». Токен обязателен при
    выходе наружу: в сети Tailscale могут быть чужие устройства, а панель
    распоряжается аккаунтом Flow целиком.

    notify_link — слать ли готовую ссылку с токеном в Telegram. Смысл имеет
    только в удалённом режиме: набирать токен на телефоне руками невозможно.
    """
    saved: dict[str, Any] = {}
    try:
        saved = json.loads(Path(UI_FILE).read_text(encoding="utf-8"))
        if saved.get("endpoint"):
            cfg.set("cdp.endpoint", saved["endpoint"])
        if saved.get("soften_backend"):
            cfg.set("moderation.soften.backend", saved["soften_backend"])
    except (OSError, json.JSONDecodeError):
        pass

    if host.strip().lower() in ("tailscale", "ts"):
        ip = tailscale_ip()
        if not ip:
            raise SystemExit(
                "Tailscale не отвечает — не смог определить IP этой машины в сети.\n"
                "Проверь, что он запущен: tailscale status"
            )
        host = ip

    remote = host not in LOOPBACK
    if token is None:
        token = ""
        if remote:
            # Живёт в .flowbatch_ui.json (он в .gitignore): ссылка остаётся
            # прежней между перезапусками, иначе токен пришлось бы переносить
            # на телефон заново после каждого рестарта.
            token = str(saved.get("token") or "") or secrets.token_urlsafe(24)
            saved["token"] = token
            try:
                Path(UI_FILE).write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass

    state = AppState(cfg, source, saved=saved)
    server = ThreadingHTTPServer((host, port), make_handler(state, token))
    local = f"http://127.0.0.1:{port}/"
    print(f"flowbatch: панель на http://{host}:{port}/  (Ctrl+C — выход)")
    if remote:
        suffix = f"?token={token}" if token else ""
        remote_url = f"http://{host}:{port}/{suffix}"
        print(f"  для устройств Tailscale: {remote_url}")
        print("  ссылку с токеном никому не пересылай: она даёт полный доступ к твоему Flow")
        if notify_link:
            _send_access_link(state.notifier, remote_url)
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(
            f"http://{host}:{port}/?token={token}" if remote and token else local)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nостановлено")
    finally:
        server.shutdown()
