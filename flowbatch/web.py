"""Локальная веб-панель управления очередью.

Стандартная библиотека, ноль зависимостей. Очередь крутится в фоновом потоке,
страница опрашивает /api/state и показывает таблицу, живой лог и результаты.

Источники очереди:
  - .xlsx (листы IMG_QUEUE/VID_QUEUE) — статусы пишутся обратно в книгу;
  - .yaml (формат jobs.yaml);
  - .txt / вставленный текст с @-директивами (@project/@ref/@use/@duration/@out).

Правило выбора: отмеченные галочками строки запускаются ровно как отмечены
(даже если уже DONE — это явное «перегенерить»). Без галочек идут все строки
в статусе TODO. Фильтры тип/батч/лимит применяются поверх.

Проект: если очередь объявила @project (текст) или PROJECT_NAME (xlsx),
перед прогоном этот проект Flow открывается, а при отсутствии — создаётся.
Резюм в таком случае считается в рамках проекта.

Сервер намеренно слушает только 127.0.0.1 и отдаёт файлы только из out/ и
screenshots/: он управляет твоим браузером, наружу его выставлять нельзя.
"""

from __future__ import annotations

import json
import mimetypes
import threading
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
from rich.console import Console

from .config import Config
from .flow_client import FlowClient, FlowClientError
from .notify import Notifier
from .promptfile import SYNTAX_HELP
from .promptfile import parse as parse_prompts
from .queue import RunLog, load_jobs
from .refs import RefCache, RefResolver, parse_lib_spec
from .runner import Runner
from .sheet import ST_DONE, ST_ERROR, SheetQueue

# Файл, куда сохраняется текст, вставленный в панель.
PASTED_FILE = "prompts_pasted.flow.txt"
# Локальные настройки панели (endpoint CDP). В git не попадает.
UI_FILE = ".flowbatch_ui.json"

STATUS_DISPLAY = {"ok": "DONE", "failed": "ERROR", "dry_run": "DRY", "IN_PROGRESS": "IN_PROGRESS"}

HTML = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>flowbatch</title>
<style>
:root{--bg:#0a0c10;--panel:#12161d;--panel2:#171c25;--line:#232b37;--line2:#2c3542;
--fg:#e9ecf1;--dim:#8b95a5;--dim2:#5c6675;--ok:#3fb950;--err:#f85149;--run:#d29922;
--todo:#768390;--dry:#a371f7;--accent:#4493f8;--accent2:#2f6fd0;
--r:10px;font-synthesis:none}
*{box-sizing:border-box}
::selection{background:#264f78}
body{margin:0;background:var(--bg);color:var(--fg);
font:13.5px/1.55 -apple-system,"Segoe UI",Roboto,sans-serif;
-webkit-font-smoothing:antialiased}
header{padding:10px 20px;border-bottom:1px solid var(--line);display:flex;gap:12px;
align-items:center;flex-wrap:wrap;background:linear-gradient(180deg,#141922,#10141b);
position:sticky;top:0;z-index:5}
h1{font-size:15px;margin:0;font-weight:700;letter-spacing:.02em}
h1 b{color:var(--accent)}
.pill{display:inline-flex;gap:6px;align-items:center;background:var(--panel2);
border:1px solid var(--line);border-radius:20px;padding:3px 12px;font-size:12px;color:var(--dim)}
.pill b{color:var(--fg);font-weight:600}
.dot{width:8px;height:8px;border-radius:50%;flex:none}
main{display:grid;grid-template-columns:minmax(0,1fr) 425px;height:calc(100vh - 49px)}
@media(max-width:1150px){main{display:block;height:auto}}
section{overflow:auto;padding:14px 20px 40px}
aside{border-left:1px solid var(--line);overflow:auto;padding:14px 18px;background:#0d1016}
@media(max-width:1150px){aside{border-left:none;border-top:1px solid var(--line)}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);
padding:12px 14px;margin-bottom:12px}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.row+.row{margin-top:9px}
input,select,textarea{font:inherit;background:var(--panel2);color:var(--fg);
border:1px solid var(--line2);border-radius:8px;padding:7px 11px;outline:none;
transition:border-color .12s}
input:focus,select:focus,textarea:focus{border-color:var(--accent)}
input[type=text]{flex:1;min-width:220px}
input[type=number]{width:88px}
textarea{width:100%;min-height:170px;resize:vertical;
font:12px/1.5 ui-monospace,Consolas,monospace;white-space:pre;tab-size:2}
.btn{font:inherit;font-weight:500;cursor:pointer;border-radius:8px;padding:7px 15px;
background:var(--panel2);color:var(--fg);border:1px solid var(--line2);
transition:all .12s;display:inline-flex;align-items:center;gap:6px}
.btn:hover:not(:disabled){border-color:var(--accent);transform:translateY(-1px)}
.btn:active:not(:disabled){transform:translateY(0)}
.btn:disabled{opacity:.4;cursor:not-allowed}
.btn.primary{background:linear-gradient(180deg,#4b9bff,#3576dd);border-color:#2f6fd0;
color:#fff;font-weight:600;box-shadow:0 2px 8px #2f6fd044}
.btn.primary:hover:not(:disabled){filter:brightness(1.08);border-color:#4b9bff}
.btn.danger{background:#2b1518;border-color:#5c2b2b;color:#ff9d96}
.btn.ghost{background:transparent;border-color:transparent;color:var(--dim)}
.btn.ghost:hover:not(:disabled){border-color:var(--line2);color:var(--fg);transform:none}
.btn.sm{padding:3px 9px;font-size:12px;border-radius:6px}
label.chk{display:flex;gap:6px;align-items:center;color:var(--dim);font-size:13px;cursor:pointer}
.qm{display:inline-flex;width:15px;height:15px;border-radius:50%;background:var(--line2);
color:var(--dim);font-size:10px;align-items:center;justify-content:center;cursor:help;flex:none}
details summary{cursor:pointer;color:var(--dim);font-size:13px;user-select:none;list-style:none}
details summary::before{content:"▸ ";color:var(--dim2)}
details[open] summary::before{content:"▾ "}
details[open] summary{margin-bottom:9px;color:var(--fg)}
.hint{font:11.5px/1.65 ui-monospace,Consolas,monospace;color:var(--dim);
white-space:pre-wrap;background:#0c0f14;border:1px solid var(--line);
border-radius:8px;padding:9px 11px;margin:8px 0}
.helpgrid{display:grid;grid-template-columns:auto 1fr;gap:5px 14px;font-size:12.5px;
color:var(--dim);margin:4px 0 2px}
.helpgrid b{color:var(--fg);font-weight:600;white-space:nowrap}
.stat{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 10px;align-items:center}
.stat .pill b{font-variant-numeric:tabular-nums}
table{width:100%;border-collapse:collapse}
thead th{position:sticky;top:-14px;background:var(--bg);z-index:2;
text-align:left;font-size:10.5px;text-transform:uppercase;letter-spacing:.08em;
color:var(--dim);font-weight:600;padding:8px;border-bottom:1px solid var(--line);
white-space:nowrap}
thead th.sortable{cursor:pointer;user-select:none}
thead th.sortable:hover{color:var(--fg)}
thead th .arr{color:var(--accent);margin-left:3px}
td{padding:8px;border-bottom:1px solid #171d26;vertical-align:top}
tbody tr{transition:background .1s}
tbody tr:hover td{background:#141a24}
tr.active td{background:#182234}
.id{font-family:ui-monospace,Consolas,monospace;font-size:12px;white-space:nowrap}
.prompt{color:var(--dim);font-size:12px;max-width:520px;overflow:hidden;cursor:pointer;
display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.prompt.full{-webkit-line-clamp:unset}
.badge{display:inline-flex;gap:5px;align-items:center;padding:2px 10px;border-radius:20px;
font-size:11px;font-weight:600;white-space:nowrap}
.badge::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor}
.s-TODO{background:#1d232c;color:var(--todo)}
.s-IN_PROGRESS{background:#3a2d10;color:var(--run);animation:pulse 1.1s infinite alternate}
.s-DONE{background:#122a1c;color:var(--ok)}
.s-ERROR{background:#31171b;color:var(--err)}
.s-DRY{background:#241a38;color:var(--dry)}
.s-SKIP{background:#1d232c;color:var(--todo)}
@keyframes pulse{from{opacity:.6}to{opacity:1}}
.err{color:var(--err);font-size:11px;margin-top:3px;max-width:520px}
.del{opacity:0;transition:opacity .12s;color:var(--dim2);background:none;border:none;
cursor:pointer;font-size:14px;padding:0 4px;line-height:1}
tbody tr:hover .del{opacity:1}
.del:hover{color:var(--err)}
pre#log{background:#090c11;border:1px solid var(--line);border-radius:8px;padding:11px;
font:12px/1.55 ui-monospace,Consolas,monospace;white-space:pre-wrap;word-break:break-word;
max-height:330px;overflow:auto;margin:0}
.gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(96px,1fr));gap:8px;margin-top:8px}
.gallery a{display:block;border:1px solid var(--line);border-radius:8px;overflow:hidden;
background:#000;transition:all .12s}
.gallery a:hover{border-color:var(--accent);transform:translateY(-2px)}
.gallery img,.gallery video{width:100%;height:148px;object-fit:cover;display:block}
.gallery span{display:block;font-size:10px;color:var(--dim);padding:3px 6px;
overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
h2{font-size:11px;text-transform:uppercase;letter-spacing:.09em;color:var(--dim);margin:16px 0 7px}
h2:first-child{margin-top:0}
#toast{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);
border-radius:9px;padding:11px 18px;font-size:13px;max-width:640px;white-space:pre-wrap;
display:none;z-index:9;box-shadow:0 8px 30px #000a}
#toast.err{background:#2d1518;border:1px solid #5c2b2b;color:#ff9d96}
#toast.ok{background:#122a1c;border:1px solid #2b5c3b;color:#7ee29a}
.settings-status{font-size:12px;color:var(--dim);margin-top:6px}
</style></head><body>
<header>
  <h1>flow<b>batch</b></h1>
  <span class="pill" id="conn"><span class="dot" style="background:var(--todo)"></span>…</span>
  <span style="flex:1"></span>
  <span class="pill" id="qproj" style="display:none"></span>
  <span class="pill" id="proj" style="display:none"></span>
</header>
<main>
<section>
  <div class="card">
    <div class="row">
      <input type="text" id="src" placeholder="очередь: .xlsx / .yaml / .txt с @-директивами">
      <button class="btn" id="reload" title="перечитать файл очереди; убранные строки вернутся">Загрузить</button>
    </div>
    <details id="pastebox">
      <summary>Вставить промпты текстом</summary>
      <textarea id="ptext" spellcheck="false" placeholder="@project Название проекта&#10;&#10;=== IMG K1&#10;@ref C:\путь\референс.png&#10;Текст промпта...&#10;&#10;=== VID K1_anim&#10;@use K1&#10;@duration 8&#10;Animate this image..."></textarea>
      <div class="hint" id="syntax"></div>
      <div class="row"><button class="btn" id="parse">Разобрать и загрузить</button></div>
    </details>
    <details id="settingsbox">
      <summary>Подключение к браузеру</summary>
      <div class="row">
        <input type="text" id="endpoint" placeholder="http://localhost:9222">
        <button class="btn" id="saveep">Сохранить и проверить</button>
      </div>
      <div class="settings-status" id="epstatus"></div>
      <div class="helpgrid" style="margin-top:8px">
        <b>чужой доступ</b><span>токен вставлять не нужно и негде: авторизация — это залогиненный
        браузер. Другой человек запускает свой Edge/Chrome со своим профилем
        (команда в README), логинится в Google один раз и указывает здесь порт своего браузера.</span>
      </div>
    </details>
    <div class="row">
      <select id="kind" title="фильтр по типу задач">
        <option value="">все типы</option>
        <option value="image">только image</option><option value="video">только video</option></select>
      <input type="text" id="batch" placeholder="BATCH" style="min-width:104px;flex:0"
             title="фильтр по колонке 04_BATCH из Excel-очереди (например BATCH_A)">
      <input type="number" id="limit" placeholder="лимит" title="взять не больше N строк после фильтров">
      <label class="chk" title="репетиция: всё, кроме нажатия «Создать» — генерации не тратятся">
        <input type="checkbox" id="dry"> dry-run</label>
      <span style="flex:1"></span>
      <button class="btn danger sm" id="delsel" style="display:none">✕ убрать отмеченные</button>
      <button class="btn primary" id="start">▶ Старт</button>
      <button class="btn danger" id="stop" disabled>■ Стоп</button>
    </div>
    <details>
      <summary>Справка: галочки, dry-run, BATCH, лимит</summary>
      <div class="helpgrid">
        <b>галочки</b><span>отмеченные строки идут в прогон ровно как отмечены — даже со статусом
        DONE (это явная перегенерация). Ничего не отмечено — идут все TODO.</span>
        <b>dry-run</b><span>полная репетиция без траты генераций: откроется проект, выставятся
        формат и настройки, введётся промпт, прикрепятся референсы, сохранится скриншот —
        но кнопка «Создать» нажата не будет. Проверяй так новые очереди и референсы.</span>
        <b>BATCH</b><span>фильтр по колонке 04_BATCH Excel-очереди (BATCH_A, SOLO…).
        Для yaml и текста не используется.</span>
        <b>лимит</b><span>взять не больше N первых строк после всех фильтров.</span>
        <b>✕ у строки</b><span>убирает строку из текущей очереди панели. Исходный файл не меняется;
        «Загрузить» возвращает всё обратно.</span>
        <b>проект</b><span>если очередь объявила @project (текст) или PROJECT_NAME (xlsx),
        перед стартом этот проект Flow откроется, а при отсутствии — создастся.</span>
      </div>
    </details>
  </div>
  <div class="stat" id="stat"></div>
  <table><thead><tr id="headrow">
    <th style="width:26px"><input type="checkbox" id="selall" title="выбрать все видимые"></th>
    <th class="sortable" data-k="id">ID</th>
    <th class="sortable" data-k="kind">тип</th>
    <th class="sortable" data-k="batch">batch</th>
    <th class="sortable" data-k="duration">сек</th>
    <th class="sortable" data-k="refs">реф</th>
    <th class="sortable" data-k="out">файл</th>
    <th>промпт</th>
    <th class="sortable" data-k="status">статус</th>
    <th style="width:26px"></th>
  </tr></thead><tbody id="rows"></tbody></table>
</section>
<aside>
  <h2>Лог</h2>
  <pre id="log">—</pre>
  <h2>Результаты <span class="pill" id="rescount" style="display:none"></span></h2>
  <div class="gallery" id="gal"></div>
</aside>
</main>
<div id="toast"></div>
<script>
const $ = s => document.querySelector(s);
const sel = new Set();
let knownIds = [];
let sortK = null, sortDir = 1;

function toast(msg, kind){
  const t = $('#toast'); t.textContent = msg;
  t.className = kind || 'err'; t.style.display = 'block';
  clearTimeout(t._h); t._h = setTimeout(() => t.style.display = 'none', kind==='ok'?3500:7000);
}
async function api(path, body){
  const opt = body ? {method:'POST',headers:{'Content-Type':'application/json'},
                      body:JSON.stringify(body)} : {};
  const r = await fetch(path, opt);
  const j = await r.json();
  if (j.error) throw new Error(j.error);
  return j;
}
function esc(s){ return (s||'').toString().replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

function sortRows(rows){
  if (!sortK) return rows;
  const num = v => typeof v === 'number';
  return rows.slice().sort((a,b) => {
    let x = a[sortK], y = b[sortK];
    if (x == null && y == null) return 0;
    if (x == null) return 1;
    if (y == null) return -1;
    if (num(x) && num(y)) return (x - y) * sortDir;
    return String(x).localeCompare(String(y), 'ru') * sortDir;
  });
}

function render(st){
  $('#start').disabled = st.running;
  $('#stop').disabled = !st.running;
  const c1 = $('#conn');
  c1.innerHTML = '<span class="dot" style="background:'
    + (st.running ? (st.connected ? 'var(--ok)' : 'var(--err)') : 'var(--todo)') + '"></span>'
    + (st.running ? (st.connected ? 'прогон идёт' : 'нет связи с браузером') : 'простаивает');
  const qp = $('#qproj');
  if (st.queue_project) { qp.style.display=''; qp.innerHTML = 'очередь → <b>' + esc(st.queue_project) + '</b>'; }
  else qp.style.display = 'none';
  const pp = $('#proj');
  if (st.project) { pp.style.display=''; pp.innerHTML = 'открыт: <b>' + esc(st.project) + '</b>'; }
  else pp.style.display = 'none';

  if (document.activeElement !== $('#src') && !$('#src').value) $('#src').value = st.source || '';
  if (document.activeElement !== $('#endpoint') && !$('#endpoint').value) $('#endpoint').value = st.endpoint || '';
  $('#syntax').textContent = st.syntax_help || '';

  const c = st.counts || {};
  $('#stat').innerHTML = Object.keys(c).map(k =>
      `<span class="pill">${k}: <b>${c[k]}</b></span>`).join('')
    + (st.removed ? `<span class="pill">скрыто: <b>${st.removed}</b>
        <button class="btn ghost sm" onclick="restoreRows()">вернуть</button></span>` : '')
    + (st.refs && (st.refs.uploads || st.refs.reused)
       ? `<span class="pill">реф: залито <b>${st.refs.uploads}</b>, повторно <b>${st.refs.reused}</b></span>` : '');

  const rows = sortRows(st.rows || []);
  knownIds = rows.map(r => r.id);
  document.querySelectorAll('#headrow th.sortable').forEach(th => {
    const base = th.textContent.replace(/[▲▼]/g, '').trim();
    th.innerHTML = esc(base) + (th.dataset.k === sortK
      ? `<span class="arr">${sortDir > 0 ? '▲' : '▼'}</span>` : '');
  });
  $('#rows').innerHTML = rows.map(r => `
    <tr class="${r.status==='IN_PROGRESS'?'active':''}">
      <td><input type="checkbox" data-id="${esc(r.id)}" ${sel.has(r.id)?'checked':''}></td>
      <td class="id">${esc(r.id)}</td>
      <td title="${r.kind}">${r.kind==='video'?'🎬':'🖼'}</td>
      <td class="id" style="color:var(--dim)">${esc(r.batch||'')}</td>
      <td style="color:var(--dim)">${r.duration||''}</td>
      <td title="${esc((r.refs_list||[]).join('\n'))}">${r.refs||''}</td>
      <td class="id" style="color:var(--dim);font-size:11px">${esc(r.out||'')}</td>
      <td><div class="prompt">${esc(r.prompt)}</div>
          ${r.error?`<div class="err">${esc(r.error)}</div>`:''}</td>
      <td><span class="badge s-${r.status}">${r.status}</span></td>
      <td><button class="del" data-del="${esc(r.id)}" title="убрать из очереди (файл не меняется)">✕</button></td>
    </tr>`).join('') || '<tr><td colspan="10" style="color:var(--dim)">очередь пуста — укажи файл или вставь промпты</td></tr>';
  $('#selall').checked = knownIds.length > 0 && knownIds.every(id => sel.has(id));
  $('#delsel').style.display = sel.size ? '' : 'none';

  const log = $('#log');
  const stick = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  log.textContent = (st.log||[]).join('\n') || '—';
  if (stick) log.scrollTop = log.scrollHeight;

  const rc = $('#rescount');
  rc.style.display = (st.results||[]).length ? '' : 'none';
  rc.textContent = (st.results||[]).length;
  $('#gal').innerHTML = (st.results||[]).map(f => {
    const u = '/api/file?path=' + encodeURIComponent(f.path);
    return `<a href="${u}" target="_blank">${f.video
      ? `<video src="${u}" muted loop onmouseover="this.play()" onmouseout="this.pause()"></video>`
      : `<img src="${u}" loading="lazy">`}<span title="${esc(f.name)}">${esc(f.name)}</span></a>`;
  }).join('');
}

document.body.addEventListener('change', e => {
  if (e.target.matches('tbody input[type=checkbox]')) {
    e.target.checked ? sel.add(e.target.dataset.id) : sel.delete(e.target.dataset.id);
    $('#delsel').style.display = sel.size ? '' : 'none';
  }
});
$('#selall').addEventListener('change', e => {
  knownIds.forEach(id => e.target.checked ? sel.add(id) : sel.delete(id));
  tick();
});
document.body.addEventListener('click', async e => {
  const p = e.target.closest('.prompt'); if (p) { p.classList.toggle('full'); return; }
  const th = e.target.closest('th.sortable');
  if (th) {
    const k = th.dataset.k;
    if (sortK === k) { if (sortDir === 1) sortDir = -1; else { sortK = null; sortDir = 1; } }
    else { sortK = k; sortDir = 1; }
    tick(); return;
  }
  const del = e.target.closest('.del');
  if (del) {
    try { sel.delete(del.dataset.del); await api('/api/remove', {ids:[del.dataset.del]}); }
    catch(err){ toast(err.message); }
    tick();
  }
});
async function restoreRows(){
  try { await api('/api/remove', {reset:true}); } catch(e){ toast(e.message); }
  tick();
}
async function tick(){ try { render(await api('/api/state')); } catch(e){} }

$('#start').onclick = async () => {
  $('#start').disabled = true;
  try {
    await api('/api/start', {
      source: $('#src').value.trim(), kind: $('#kind').value,
      batch: $('#batch').value.trim(), limit: $('#limit').value,
      dry_run: $('#dry').checked, selected: [...sel],
    });
    toast('Прогон запущен', 'ok');
  } catch(e){ toast(e.message); }
  tick();
};
$('#stop').onclick = async () => { try { await api('/api/stop', {}); } catch(e){ toast(e.message); } tick(); };
$('#reload').onclick = async () => {
  try { sel.clear(); await api('/api/reload', {source: $('#src').value.trim()});
        toast('Очередь загружена', 'ok'); }
  catch(e){ toast(e.message); }
  tick();
};
$('#parse').onclick = async () => {
  try {
    sel.clear();
    await api('/api/parse', {text: $('#ptext').value});
    $('#pastebox').open = false;
    toast('Разобрано и загружено', 'ok');
  } catch(e){ toast(e.message); }
  tick();
};
$('#delsel').onclick = async () => {
  try { await api('/api/remove', {ids:[...sel]}); sel.clear(); }
  catch(e){ toast(e.message); }
  tick();
};
$('#saveep').onclick = async () => {
  const st = $('#epstatus');
  st.textContent = 'проверяю…';
  try {
    const r = await api('/api/settings', {endpoint: $('#endpoint').value.trim()});
    st.textContent = '✓ ' + r.browser;
    toast('Подключение сохранено: ' + r.browser, 'ok');
  } catch(e){ st.textContent = '✗ ' + e.message; toast(e.message); }
};

tick();
setInterval(tick, 1500);
</script></body></html>
"""


class LogSink:
    """Приёмник вывода rich.Console: копит строки для веб-панели."""

    def __init__(self, maxlen: int = 600) -> None:
        self.lines: deque[str] = deque(maxlen=maxlen)
        self._buf = ""
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        with self._lock:
            self._buf += text
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                line = line.rstrip()
                if line:
                    self.lines.append(line)
        return len(text)

    def flush(self) -> None:
        return None

    def snapshot(self) -> list[str]:
        with self._lock:
            return list(self.lines)


class AppState:
    """Состояние панели: очередь, статусы, лог, фоновый поток прогона."""

    def __init__(self, cfg: Config, default_source: str) -> None:
        self.cfg = cfg
        self.source = default_source
        self.sink = LogSink()
        self.console = Console(file=self.sink, force_terminal=False, width=110, highlight=False)
        self.log = RunLog(cfg.runs_log())
        self.notifier = Notifier()
        self.rows: list[dict[str, Any]] = []
        self.jobs_by_id: dict[str, Any] = {}
        self.statuses: dict[str, str] = {}
        self.errors: dict[str, str] = {}
        self.removed: set[str] = set()
        self.queue_project: str | None = None
        self.sheet: SheetQueue | None = None
        self.sheet_by_id: dict[str, Any] = {}
        self.thread: threading.Thread | None = None
        self.runner: Runner | None = None
        self.project: str | None = None
        self.connected = False
        self.refs_stat = {"uploads": 0, "reused": 0}
        self._skip_completed = False
        try:
            self.load_queue(default_source)
        except Exception as exc:  # noqa: BLE001 — панель должна открыться даже с плохим путём
            self.console.print(f"[yellow]очередь не загружена: {exc}[/yellow]")

    # ------------------------------------------------------------- очередь

    def _require_idle(self) -> None:
        if self.running:
            raise RuntimeError("прогон идёт — сначала останови его")

    def load_queue(self, source: str) -> None:
        """Прочитать очередь из .xlsx / .yaml / .txt."""
        self._require_idle()
        src = (source or "").strip()
        if not src:
            raise ValueError("не указан путь к очереди")
        p = Path(src)
        if not p.exists():
            raise FileNotFoundError(f"не найден файл очереди: {p}")

        self.source = src
        self.errors = {}
        self.removed = set()
        self.queue_project = None
        suffix = p.suffix.lower()

        if suffix in (".xlsx", ".xlsm"):
            self.sheet = SheetQueue(p)
            # Для отображения берём ВСЕ строки: DONE и ERROR видно глазами,
            # а что реально запускать — решается в start().
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
                jobs, errors, meta = parse_prompts(p.read_text(encoding="utf-8-sig"), self.cfg.out_dir())
                if errors:
                    raise ValueError("ошибки разбора промптов:\n" + "\n".join(f"• {e}" for e in errors))
                self.queue_project = meta.get("project")
            else:
                jobs = load_jobs(p)
            self.jobs_by_id = {j.id: j for j in jobs}
            self.rows = [self._row_dict(j) for j in jobs]
            if self.queue_project:
                # Очередь привязана к проекту: что в нём реально сделано, станет
                # ясно после его открытия — резюм применит прогон, а не загрузка.
                self.statuses = {j.id: "TODO" for j in jobs}
            else:
                done = self.log.completed_ids()
                self.statuses = {j.id: (ST_DONE if j.id in done else "TODO") for j in jobs}

        self.console.print(
            f"очередь загружена: {len(self.rows)} строк из {p.name}"
            + (f" (проект: {self.queue_project})" if self.queue_project else "")
        )

    def parse_text(self, text: str) -> None:
        """Разобрать вставленный текст. Сохраняется в файл — переживает рестарт."""
        self._require_idle()
        if not (text or "").strip():
            raise ValueError("пустой текст — нечего разбирать")
        # Сначала валидируем, потом сохраняем: битый текст файл не перетирает.
        _, errors, _ = parse_prompts(text, self.cfg.out_dir())
        if errors:
            raise ValueError("ошибки разбора:\n" + "\n".join(f"• {e}" for e in errors))
        Path(PASTED_FILE).write_text(text, encoding="utf-8")
        self.load_queue(PASTED_FILE)

    def remove_rows(self, ids: list[str] | None, reset: bool = False) -> None:
        """Убрать строки из очереди панели (файл не трогается)."""
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

    # ------------------------------------------------------------ настройки

    def set_endpoint(self, endpoint: str) -> str:
        """Сменить CDP endpoint. Возвращает имя браузера после проверки."""
        self._require_idle()
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
        try:
            Path(UI_FILE).write_text(
                json.dumps({"endpoint": endpoint}, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass
        return browser

    # --------------------------------------------------------------- прогон

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

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

        jobs = [self.jobs_by_id[cid] for cid in chosen]
        for cid in chosen:
            self.statuses[cid] = "TODO"
            self.errors.pop(cid, None)
        self.refs_stat = {"uploads": 0, "reused": 0}
        # Резюм внутри прогона (по проекту) — только когда выбор неявный.
        self._skip_completed = not selected and self.sheet is None

        dry = bool(opts.get("dry_run"))
        self.thread = threading.Thread(target=self._run, args=(jobs, dry), daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.runner is not None:
            self.runner.stop_requested = True
            self.console.print("[yellow]запрошена остановка — доработаю текущую задачу[/yellow]")

    def _run(self, jobs: list[Any], dry: bool) -> None:
        client = FlowClient(self.cfg)
        try:
            client.connect()
            self.connected = True
        except FlowClientError as exc:
            self.connected = False
            self.console.print(f"[red]{exc}[/red]")
            return

        try:
            if self.queue_project:
                how = client.ensure_project(self.queue_project)
                verb = {"current": "уже открыт", "opened": "открыт", "created": "создан"}[how]
                self.console.print(f"проект {self.queue_project!r} {verb}")

            self.project = client.project_name() or client.current_project_id()
            project_id = client.current_project_id() or ""
            if not project_id:
                self.console.print("[red]открыт список проектов, а не проект — открой проект во вкладке[/red]")
                return

            # Резюм в рамках проекта: сделанное здесь — пропускаем.
            if self._skip_completed and not dry:
                done = self.log.completed_ids(project=project_id)
                skipped = [j for j in jobs if j.id in done]
                jobs = [j for j in jobs if j.id not in done]
                for j in skipped:
                    self.statuses[j.id] = ST_DONE
                if skipped:
                    self.console.print(f"резюм: в этом проекте уже сделано {len(skipped)}")
                if not jobs:
                    self.console.print("[yellow]все выбранные задачи уже выполнены в этом проекте[/yellow]")
                    return

            resolver = RefResolver(
                client, self.log,
                RefCache(self.cfg.runs_log().parent / ".flowbatch_refcache.json"),
                project_id,
                on_upload=lambda p: self.console.print(f"  заливаю в библиотеку: {p.name}"),
            )

            def on_status(job: Any, status: str, result_path: str | None = None,
                          error: str | None = None) -> None:
                self.statuses[job.id] = STATUS_DISPLAY.get(status, status)
                if error:
                    self.errors[job.id] = error
                self.refs_stat = {"uploads": resolver.uploads, "reused": resolver.reused}
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

            self.runner = Runner(
                self.cfg, client, self.notifier, self.log, self.console,
                dry_run=dry, resolver=resolver, on_status=on_status, project_id=project_id,
            )
            outcome = self.runner.run(jobs)
            self.console.print(
                f"[bold]Итог:[/bold] сделано {outcome.done} из {outcome.total}"
                + (f", провалено {outcome.failed}" if outcome.failed else "")
                + f", за {outcome.elapsed_sec / 60:.1f} мин"
            )
            if outcome.stopped_reason:
                self.console.print(f"[red]{outcome.stopped_reason}[/red]")
        except Exception as exc:  # noqa: BLE001
            self.console.print(f"[red]прогон упал: {exc}[/red]")
        finally:
            self.runner = None
            self.connected = False
            client.close()

    # ------------------------------------------------------------- состояние

    def results(self) -> list[dict[str, Any]]:
        """Файлы из out/, свежие сверху."""
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
            "running": self.running,
            "connected": self.connected,
            "project": self.project,
            "queue_project": self.queue_project,
            "source": self.source,
            "endpoint": self.cfg.get("cdp.endpoint"),
            "syntax_help": SYNTAX_HELP,
            "rows": rows,
            "counts": counts,
            "removed": len(self.removed),
            "log": self.sink.snapshot()[-250:],
            "results": self.results(),
            "refs": self.refs_stat,
        }


def make_handler(state: AppState) -> type[BaseHTTPRequestHandler]:
    out_dir = state.cfg.out_dir().resolve()
    shots_dir = state.cfg.screenshots_dir().resolve()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a: Any) -> None:  # noqa: A003 — глушим лог сервера
            return

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj: Any, code: int = 200) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                self._send(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
            elif u.path == "/api/state":
                self._json(state.snapshot())
            elif u.path == "/api/file":
                self._serve_file(parse_qs(u.query).get("path", [""])[0])
            else:
                self._json({"error": "not found"}, 404)

        def _serve_file(self, raw: str) -> None:
            """Отдать файл результата.

            Только из out/ и screenshots/: без этой проверки параметр path
            превратился бы в чтение любого файла на диске.
            """
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
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                payload = {}
            try:
                if u.path == "/api/start":
                    state.start(payload)
                    self._json({"ok": True})
                elif u.path == "/api/stop":
                    state.stop()
                    self._json({"ok": True})
                elif u.path == "/api/reload":
                    state.load_queue(payload.get("source") or state.source)
                    self._json({"ok": True})
                elif u.path == "/api/parse":
                    state.parse_text(payload.get("text") or "")
                    self._json({"ok": True})
                elif u.path == "/api/remove":
                    state.remove_rows(payload.get("ids"), reset=bool(payload.get("reset")))
                    self._json({"ok": True})
                elif u.path == "/api/settings":
                    browser = state.set_endpoint(payload.get("endpoint") or "")
                    self._json({"ok": True, "browser": browser})
                else:
                    self._json({"error": "not found"}, 404)
            except Exception as exc:  # noqa: BLE001 — ошибку показываем в панели
                self._json({"error": str(exc)}, 400)

    return Handler


def serve(cfg: Config, source: str, port: int = 8765, open_browser: bool = True) -> None:
    """Запустить панель на 127.0.0.1:<port>."""
    # Сохранённые настройки панели (endpoint) перекрывают config.yaml.
    try:
        saved = json.loads(Path(UI_FILE).read_text(encoding="utf-8"))
        if saved.get("endpoint"):
            cfg.set("cdp.endpoint", saved["endpoint"])
    except (OSError, json.JSONDecodeError):
        pass

    state = AppState(cfg, source)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(state))
    url = f"http://127.0.0.1:{port}/"
    print(f"flowbatch: панель на {url}  (Ctrl+C — выход)")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nостановлено")
    finally:
        server.shutdown()
