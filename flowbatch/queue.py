"""Очередь задач: чтение jobs.yaml, лог прогонов, резюм."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal

import yaml

from .scrub import scrub

Kind = Literal["image", "video"]

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_DRY_RUN = "dry_run"


@dataclass
class Job:
    """Одна задача очереди."""

    id: str
    kind: Kind
    prompt: str
    refs: list[str] = field(default_factory=list)
    duration: int | None = None  # только для видео; None = взять из config.yaml
    # Имя выходного файла без расширения. Нужно для Excel-очереди, где оно
    # задано отдельной колонкой (19_OUTPUT_NAME) и не совпадает с id.
    output_name: str | None = None

    @property
    def out_stem(self) -> str:
        """Основа имени файла в out/: явное имя, иначе id."""
        if self.output_name:
            return Path(self.output_name).stem
        return self.id

    @classmethod
    def from_dict(cls, raw: dict[str, Any], index: int) -> "Job":
        where = f"задача #{index + 1}"
        if not isinstance(raw, dict):
            raise ValueError(f"{where}: ожидался словарь, получено {type(raw).__name__}")

        job_id = str(raw.get("id") or "").strip()
        if not job_id:
            raise ValueError(f"{where}: пустое поле id")

        kind = str(raw.get("kind") or "").strip().lower()
        if kind not in ("image", "video"):
            raise ValueError(f"{job_id}: kind должен быть image или video, а не {kind!r}")

        prompt = str(raw.get("prompt") or "").strip()
        if not prompt:
            raise ValueError(f"{job_id}: пустой prompt")

        refs_raw = raw.get("refs") or []
        if isinstance(refs_raw, str):
            refs_raw = [refs_raw]
        if not isinstance(refs_raw, list):
            raise ValueError(f"{job_id}: refs должен быть списком путей")
        refs = [str(r) for r in refs_raw]

        duration = raw.get("duration")
        if duration is not None:
            if kind != "video":
                raise ValueError(f"{job_id}: duration имеет смысл только для kind: video")
            duration = int(duration)

        return cls(id=job_id, kind=kind, prompt=prompt, refs=refs, duration=duration)


def load_jobs(path: str | Path) -> list[Job]:
    """Прочитать и провалидировать jobs.yaml."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Не найден файл очереди: {p.resolve()}")
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{p}: на верхнем уровне ожидался список задач")

    jobs = [Job.from_dict(raw, i) for i, raw in enumerate(data)]

    seen: set[str] = set()
    for j in jobs:
        if j.id in seen:
            raise ValueError(f"Дубликат id в очереди: {j.id!r}. id используется как имя файла.")
        seen.add(j.id)
    return jobs


def select_jobs(
    jobs: list[Job],
    only: str | None = None,
    from_id: str | None = None,
    limit: int | None = None,
) -> list[Job]:
    """Применить фильтры --only / --from / --limit в этом порядке."""
    result = jobs

    if only:
        result = [j for j in result if j.id == only]
        if not result:
            raise ValueError(f"--only {only!r}: такой задачи в очереди нет")

    if from_id:
        ids = [j.id for j in result]
        if from_id not in ids:
            raise ValueError(f"--from {from_id!r}: такой задачи в очереди нет")
        result = result[ids.index(from_id):]

    if limit is not None:
        if limit < 1:
            raise ValueError("--limit должен быть >= 1")
        result = result[:limit]

    return result


def resolve_ref(raw: str | Path) -> Path:
    """Найти файл референса, не требуя точного расширения.

    Расширение результата задаёт Flow (по content-type), и на момент написания
    jobs.yaml оно неизвестно: картинка может приехать .jpg, а не .png. Поэтому
    если точного пути нет — ищем соседа с тем же именем и любым расширением.
    """
    p = Path(raw)
    if p.exists():
        return p
    matches = sorted(m for m in p.parent.glob(p.stem + ".*") if m.is_file())
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise FileNotFoundError(
            f"Референс не найден: {p} (и ничего похожего на {p.parent / (p.stem + '.*')})"
        )
    raise ValueError(
        f"Референс {p} неоднозначен — подходят несколько файлов: "
        + ", ".join(m.name for m in matches)
        + ". Укажи расширение явно."
    )


def now_iso() -> str:
    """Текущее время в UTC, ISO-8601."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RunLog:
    """Журнал прогонов в формате JSON Lines."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def records(self) -> Iterator[dict[str, Any]]:
        """Прочитать все записи. Битые строки молча пропускаем."""
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    def uuid_for_file(self, path: str | Path, project: str | None = None) -> str | None:
        """Найти media-uuid по пути к ранее скачанному файлу.

        Нужно для видео-задач: референс, сгенерированный прошлой задачей, уже
        лежит в библиотеке Flow — его можно выбрать по uuid, не загружая заново.

        project обязателен на практике: uuid действителен только внутри того
        проекта, где элемент создан. Без фильтра запись из старого проекта
        вернула бы uuid, которого в текущем пикере нет.
        """
        target = Path(path).resolve()
        found: str | None = None
        for rec in self.records():
            if rec.get("status") != STATUS_OK or not rec.get("file") or not rec.get("url"):
                continue
            if project is not None and rec.get("project") != project:
                continue
            try:
                if Path(str(rec["file"])).resolve() != target:
                    continue
            except OSError:
                continue
            m = re.search(r"[?&]name=([^&]+)", str(rec["url"]))
            if m:
                found = m.group(1)  # берём самую свежую запись
        return found

    def completed_ids(self) -> set[str]:
        """id задач, уже успешно выполненных — их при резюме пропускаем."""
        done: set[str] = set()
        for rec in self.records():
            if rec.get("status") == STATUS_OK and rec.get("id"):
                done.add(str(rec["id"]))
        return done

    def append(self, record: dict[str, Any]) -> None:
        """Дописать запись. Флашим сразу: процесс могут убить в любой момент."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()

    def write_result(
        self,
        job: Job,
        status: str,
        started_at: str,
        finished_at: str | None = None,
        url: str | None = None,
        file: str | None = None,
        error: str | None = None,
        error_kind: str | None = None,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Собрать и записать строку результата."""
        # scrub здесь — последний рубеж: даже если ошибка пришла не через
        # FlowError, секрет не должен осесть в runs.jsonl.
        rec: dict[str, Any] = {
            "id": job.id,
            "kind": job.kind,
            "project": project,
            "status": status,
            "started_at": started_at,
            "finished_at": finished_at or now_iso(),
            "url": url,
            "file": file,
            "error": scrub(error) if error else None,
            "error_kind": error_kind,
        }
        self.append(rec)
        return rec
