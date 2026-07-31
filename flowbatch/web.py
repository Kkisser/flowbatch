"""Локальная веб-панель управления очередью.

Стандартная библиотека, ноль зависимостей. Очередь крутится в фоновом потоке,
страница опрашивает /api/state и показывает таблицу, живой лог и результаты.

Источники очереди:
  - .xlsx (листы IMG_QUEUE/VID_QUEUE) — статусы пишутся обратно в книгу;
  - .yaml (формат jobs.yaml);
  - .txt / вставленный текст с @-директивами (@ref/@use/@duration/@out).

Правило выбора: отмеченные галочками строки запускаются ровно как отмечены
(даже если уже DONE — это явное «перегенерить»). Без галочек идут все строки
в статусе TODO. Фильтры тип/батч/лимит применяются поверх.

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

from rich.console import Console

from .config import Config
from .flow_client import FlowClient, FlowClientError
from .notify import Notifier
from .promptfile import SYNTAX_HELP
from .promptfile import parse as parse_prompts
from .queue import RunLog, load_jobs
from .refs import RefCache, RefResolver
from .runner import Runner
from .sheet import ST_DONE, ST_ERROR, ST_IN_PROGRESS, SheetQueue

# Файл, куда сохраняется текст, вставленный в панель: перезапуск панели его
# не теряет, а CLI может прогнать то же самое через run --prompts.
PASTED_FILE = "prompts_pasted.flow.txt"

STATUS_DISPLAY = {"ok": "DONE", "failed": "ERROR", "dry_run": "DRY", "IN_PROGRESS": "IN_PROGRESS"}

HTML = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>flowbatch</title>
<style>
:root{--bg:#0b0d11;--panel:#14181f;--panel2:#181d26;--line:#252c37;--fg:#e8ebf0;
--dim:#8b95a5;--ok:#3fb950;--err:#f85149;--run:#d29922;--todo:#6e7681;--dry:#8957e5;
--accent:#4493f8;--accent2:#2f6fd0}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 -apple-system,"Segoe UI",Roboto,sans-serif}
header{padding:12px 20px;border-bottom:1px solid var(--line);display:flex;gap:14px;
align-items:center;flex-wrap:wrap;background:var(--panel)}
h1{font-size:15px;margin:0;font-weight:700;letter-spacing:.03em}
h1 b{color:var(--accent)}
.muted{color:var(--dim);font-size:12px}
main{display:grid;grid-template-columns:minmax(0,1fr) 430px;height:calc(100vh - 51px)}
@media(max-width:1150px){main{display:block;height:auto}}
section{overflow:auto;padding:14px 20px}
aside{border-left:1px solid var(--line);overflow:auto;padding:14px 20px;background:#0f1218}
@media(max-width:1150px){aside{border-left:none;border-top:1px solid var(--line)}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
padding:12px 14px;margin-bottom:12px}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.row+.row{margin-top:8px}
input,select,button,textarea{font:inherit;background:var(--panel2);color:var(--fg);
border:1px solid var(--line);border-radius:7px;padding:6px 10px}
input[type=text]{flex:1;min-width:220px}
input[type=number]{width:84px}
textarea{width:100%;min-height:170px;resize:vertical;
font:12px/1.5 ui-monospace,Consolas,monospace;white-space:pre;tab-size:2}
button{cursor:pointer;transition:border-color .12s}
button:hover:not(:disabled){border-color:var(--accent)}
button:disabled{opacity:.4;cursor:not-allowed}
button.primary{background:var(--accent);border-color:var(--accent2);color:#fff;font-weight:600}
button.danger{background:#38191c;border-color:#5c2b2b;color:#ff9d96}
label.chk{display:flex;gap:6px;align-items:center;color:var(--dim);font-size:13px}
details summary{cursor:pointer;color:var(--dim);font-size:13px;user-select:none}
details[open] summary{margin-bottom:8px;color:var(--fg)}
.hint{font:11px/1.6 ui-monospace,Consolas,monospace;color:var(--dim);
white-space:pre-wrap;background:#0d1015;border:1px solid var(--line);
border-radius:7px;padding:8px 10px;margin:8px 0}
.stat{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 10px}
.stat span{background:var(--panel);border:1px solid var(--line);border-radius:16px;
padding:2px 11px;font-size:12px;color:var(--dim)}
.stat b{color:var(--fg)}
table{width:100%;border-collapse:collapse}
thead th{position:sticky;top:-14px;background:var(--bg);z-index:2;
text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.07em;
color:var(--dim);font-weight:600;padding:7px 8px;border-bottom:1px solid var(--line)}
td{padding:7px 8px;border-bottom:1px solid #1a2029;vertical-align:top}
tr:hover td{background:#151a22}
tr.active td{background:#1a2333}
.id{font-family:ui-monospace,Consolas,monospace;font-size:12px;white-space:nowrap}
.prompt{color:var(--dim);font-size:12px;max-width:560px;overflow:hidden;cursor:pointer;
display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.prompt.full{-webkit-line-clamp:unset}
.badge{display:inline-block;padding:1px 9px;border-radius:20px;font-size:11px;font-weight:600;white-space:nowrap}
.s-TODO{background:#21262d;color:var(--todo)}
.s-IN_PROGRESS{background:#3a2d10;color:var(--run);animation:pulse 1.2s infinite alternate}
.s-DONE{background:#12261a;color:var(--ok)}
.s-ERROR{background:#2d1518;color:var(--err)}
.s-DRY{background:#231a33;color:var(--dry)}
.s-SKIP{background:#21262d;color:var(--todo)}
@keyframes pulse{from{opacity:.65}to{opacity:1}}
.err{color:var(--err);font-size:11px;margin-top:3px;max-width:560px}
pre#log{background:#0a0d12;border:1px solid var(--line);border-radius:8px;padding:11px;
font:12px/1.55 ui-monospace,Consolas,monospace;white-space:pre-wrap;word-break:break-word;
max-height:330px;overflow:auto;margin:0}
.gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(96px,1fr));gap:8px;margin-top:8px}
.gallery a{display:block;border:1px solid var(--line);border-radius:7px;overflow:hidden;
background:#000;transition:border-color .12s}
.gallery a:hover{border-color:var(--accent)}
.gallery img,.gallery video{width:100%;height:148px;object-fit:cover;display:block}
.gallery span{display:block;font-size:10px;color:var(--dim);padding:3px 6px;
overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
h2{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--dim);margin:16px 0 6px}
h2:first-child{margin-top:0}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:6px}
#toast{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);
background:#2d1518;border:1px solid #5c2b2b;color:#ff9d96;border-radius:8px;
padding:10px 16px;font-size:13px;max-width:640px;white-space:pre-wrap;display:none;z-index:9}
</style></head><body>
<header>
  <h1>flow<b>batch</b></h1>
  <span id="conn" class="muted">…</span>
  <span style="flex:1"></span>
  <span id="proj" class="muted"></span>
</header>
<main>
<section>
  <div class="card">
    <div class="row">
      <input type="text" id="src" placeholder="очередь: .xlsx / .yaml / .txt с @-директивами">
      <button id="reload">Загрузить</button>
    </div>
    <details id="pastebox">
      <summary>Вставить промпты текстом (=== IMG/VID + @ref/@use)</summary>
      <textarea id="ptext" spellcheck="false" placeholder="=== IMG K1&#10;@ref C:\путь\референс.png&#10;Текст промпта...&#10;&#10;=== VID K1_anim&#10;@use K1&#10;@duration 8&#10;Animate this image..."></textarea>
      <div class="hint" id="syntax"></div>
      <div class="row"><button id="parse">Разобрать и загрузить</button></div>
    </details>
    <div class="row">
      <select id="kind"><option value="">все типы</option>
        <option value="image">только image</option><option value="video">только video</option></select>
      <input type="text" id="batch" placeholder="BATCH" style="min-width:110px;flex:0">
      <input type="number" id="limit" placeholder="лимит">
      <label class="chk"><input type="checkbox" id="dry"> dry-run</label>
      <span style="flex:1"></span>
      <button class="primary" id="start">Старт</button>
      <button class="danger" id="stop" disabled>Стоп</button>
    </div>
    <div class="row muted" style="margin-top:6px">
      Галочки — что генерить (отмеченные пойдут даже если DONE). Без галочек — все TODO.
    </div>
  </div>
  <div class="stat" id="stat"></div>
  <table><thead><tr>
    <th style="width:26px"><input type="checkbox" id="selall" title="выбрать все"></th>
    <th>ID</th><th>тип</th><th>batch</th><th>сек</th><th>реф</th><th>файл</th>
    <th>промпт</th><th>статус</th>
  </tr></thead><tbody id="rows"></tbody></table>
</section>
<aside>
  <h2>Лог</h2>
  <pre id="log">—</pre>
  <h2>Результаты <span class="muted" id="rescount"></span></h2>
  <div class="gallery" id="gal"></div>
</aside>
</main>
<div id="toast"></div>
<script>
const $ = s => document.querySelector(s);
const sel = new Set();          // отмеченные id — живут между перерисовками
let knownIds = [];

function toast(msg){
  const t = $('#toast'); t.textContent = msg; t.style.display = 'block';
  clearTimeout(t._h); t._h = setTimeout(() => t.style.display = 'none', 6000);
}
async function api(path, body){
  const opt = body ? {method:'POST',headers:{'Content-Type':'application/json'},
                      body:JSON.stringify(body)} : {};
  const r = await fetch(path, opt);
  const j = await r.json();
  if (j.error) throw new Error(j.error);
  return j;
}
function esc(s){ return (s||'').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

function render(st){
  $('#start').disabled = st.running;
  $('#stop').disabled = !st.running;
  $('#conn').innerHTML = '<span class="dot" style="background:'
    + (st.running ? (st.connected ? 'var(--ok)' : 'var(--err)') : 'var(--todo)') + '"></span>'
    + (st.running ? (st.connected ? 'прогон идёт' : 'нет связи с браузером') : 'простаивает');
  $('#proj').textContent = st.project ? ('проект: ' + st.project) : '';
  if (document.activeElement !== $('#src') && !$('#src').value) $('#src').value = st.source || '';
  $('#syntax').textContent = st.syntax_help || '';

  const c = st.counts || {};
  $('#stat').innerHTML = Object.keys(c).map(k => `<span>${k}: <b>${c[k]}</b></span>`).join('')
    + (st.refs && (st.refs.uploads || st.refs.reused)
       ? `<span>реф: залито <b>${st.refs.uploads}</b>, повторно <b>${st.refs.reused}</b></span>` : '');

  knownIds = (st.rows||[]).map(r => r.id);
  $('#rows').innerHTML = (st.rows||[]).map(r => `
    <tr class="${r.status==='IN_PROGRESS'?'active':''}">
      <td><input type="checkbox" data-id="${esc(r.id)}" ${sel.has(r.id)?'checked':''}></td>
      <td class="id">${esc(r.id)}</td>
      <td>${r.kind==='video'?'🎬':'🖼'}</td>
      <td class="muted">${esc(r.batch||'')}</td>
      <td class="muted">${r.duration||''}</td>
      <td title="${esc((r.refs_list||[]).join('\n'))}">${r.refs||''}</td>
      <td class="muted" style="font-size:11px">${esc(r.out||'')}</td>
      <td><div class="prompt">${esc(r.prompt)}</div>
          ${r.error?`<div class="err">${esc(r.error)}</div>`:''}</td>
      <td><span class="badge s-${r.status}">${r.status}</span></td>
    </tr>`).join('') || '<tr><td colspan="9" class="muted">очередь пуста — укажи файл или вставь промпты</td></tr>';
  $('#selall').checked = knownIds.length > 0 && knownIds.every(id => sel.has(id));

  const log = $('#log');
  const stick = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  log.textContent = (st.log||[]).join('\n') || '—';
  if (stick) log.scrollTop = log.scrollHeight;

  $('#rescount').textContent = (st.results||[]).length ? `(${st.results.length})` : '';
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
  }
});
$('#selall').addEventListener('change', e => {
  knownIds.forEach(id => e.target.checked ? sel.add(id) : sel.delete(id));
  tick();
});
document.body.addEventListener('click', e => {
  const p = e.target.closest('.prompt'); if (p) p.classList.toggle('full');
});

async function tick(){ try { render(await api('/api/state')); } catch(e){} }

$('#start').onclick = async () => {
  $('#start').disabled = true;
  try {
    await api('/api/start', {
      source: $('#src').value.trim(), kind: $('#kind').value,
      batch: $('#batch').value.trim(), limit: $('#limit').value,
      dry_run: $('#dry').checked, selected: [...sel],
    });
  } catch(e){ toast(e.message); }
  tick();
};
$('#stop').onclick = async () => { try { await api('/api/stop', {}); } catch(e){ toast(e.message); } tick(); };
$('#reload').onclick = async () => {
  try { sel.clear(); await api('/api/reload', {source: $('#src').value.trim()}); }
  catch(e){ toast(e.message); }
  tick();
};
$('#parse').onclick = async () => {
  try {
    sel.clear();
    await api('/api/parse', {text: $('#ptext').value});
    $('#pastebox').open = false;
  } catch(e){ toast(e.message); }
  tick();
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
        self.sheet: SheetQueue | None = None
        self.sheet_by_id: dict[str, Any] = {}
        self.thread: threading.Thread | None = None
        self.runner: Runner | None = None
        self.project: str | None = None
        self.connected = False
        self.refs_stat = {"uploads": 0, "reused": 0}
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
        else:
            self.sheet = None
            self.sheet_by_id = {}
            if suffix in (".txt", ".text", ".prompts"):
                jobs, errors = parse_prompts(p.read_text(encoding="utf-8-sig"), self.cfg.out_dir())
                if errors:
                    raise ValueError("ошибки разбора промптов:\n" + "\n".join(f"• {e}" for e in errors))
            else:
                jobs = load_jobs(p)
            self.jobs_by_id = {j.id: j for j in jobs}
            self.rows = [self._row_dict(j) for j in jobs]
            # Резюм для не-Excel очередей идёт по runs.jsonl — DONE видно сразу.
            done = self.log.completed_ids()
            self.statuses = {j.id: (ST_DONE if j.id in done else "TODO") for j in jobs}

        self.console.print(f"очередь загружена: {len(self.rows)} строк из {p.name}")

    def parse_text(self, text: str) -> None:
        """Разобрать вставленный текст. Сохраняется в файл — переживает рестарт."""
        self._require_idle()
        if not (text or "").strip():
            raise ValueError("пустой текст — нечего разбирать")
        # Сначала валидируем, потом сохраняем: битый текст файл не перетирает.
        _, errors = parse_prompts(text, self.cfg.out_dir())
        if errors:
            raise ValueError("ошибки разбора:\n" + "\n".join(f"• {e}" for e in errors))
        Path(PASTED_FILE).write_text(text, encoding="utf-8")
        self.load_queue(PASTED_FILE)

    @staticmethod
    def _row_dict(job: Any, batch: str = "") -> dict[str, Any]:
        return {
            "id": job.id,
            "kind": job.kind,
            "batch": batch,
            "duration": job.duration,
            "refs": len(job.refs),
            "refs_list": [str(r) for r in job.refs],
            "out": job.out_stem if (job.output_name or "") else "",
            "prompt": job.prompt[:300],
        }

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
            self.project = client.project_name() or client.current_project_id()
            project_id = client.current_project_id() or ""
            if not project_id:
                self.console.print("[red]открыт список проектов, а не проект — открой проект во вкладке[/red]")
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
            st = self.statuses.get(r["id"], "TODO")
            counts[st] = counts.get(st, 0) + 1
            rows.append({**r, "status": st, "error": self.errors.get(r["id"])})
        return {
            "running": self.running,
            "connected": self.connected,
            "project": self.project,
            "source": self.source,
            "syntax_help": SYNTAX_HELP,
            "rows": rows,
            "counts": counts,
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
                else:
                    self._json({"error": "not found"}, 404)
            except Exception as exc:  # noqa: BLE001 — ошибку показываем в панели
                self._json({"error": str(exc)}, 400)

    return Handler


def serve(cfg: Config, source: str, port: int = 8765, open_browser: bool = True) -> None:
    """Запустить панель на 127.0.0.1:<port>."""
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
