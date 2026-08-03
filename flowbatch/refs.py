"""Разрешение локальных путей в media-uuid библиотеки Flow.

Ключевой факт: uuid живут ВНУТРИ проекта. Один и тот же файл в двух проектах —
два разных uuid, и uuid из чужого проекта в пикере не найдётся. Поэтому и лог,
и кэш загрузок всегда фильтруются по текущему project_id.

Порядок разрешения:
  1. файл — результат прошлой задачи в ЭТОМ проекте  -> uuid из runs.jsonl, без заливки
  2. файл уже заливался в ЭТОТ проект и не менялся   -> uuid из кэша, без заливки
  3. иначе                                            -> заливаем и запоминаем

Без шага 2 очередь из 7 строк, использующая одни и те же 4 референса 14 раз,
сделала бы 14 загрузок и намусорила бы 14 дублями в библиотеке.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .promptfile import LIB_PREFIX, LIB_PROJECT_SEP, USE_PREFIX
from .queue import RunLog, resolve_ref


class _NullLock:
    """Заглушка вместо замка для однопоточного прогона."""

    def __enter__(self) -> "_NullLock":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


@dataclass
class RefHandle:
    """Разрешённый референс, готовый к прикреплению.

    uuid задан      -> прикреплять по uuid (файл уже в библиотеке);
    uuid пуст       -> прикреплять поиском по имени (search) в библиотеке
                       проекта picker_project (None = текущий).
    """

    label: str
    uuid: str | None = None
    search: str | None = None
    picker_project: str | None = None


def parse_lib_spec(spec: str) -> tuple[str, str | None]:
    """Разобрать 'lib:имя' / 'lib:проект :: имя' -> (имя, проект|None)."""
    body = spec[len(LIB_PREFIX):].strip()
    if LIB_PROJECT_SEP in body:
        project, name = body.split(LIB_PROJECT_SEP, 1)
        return name.strip(), project.strip() or None
    return body, None


@dataclass
class RefStat:
    """Отпечаток файла: путь плюс то, по чему видно, что его переписали."""

    path: str
    mtime_ns: int
    size: int

    @classmethod
    def of(cls, p: Path) -> "RefStat":
        st = p.stat()
        return cls(path=str(p.resolve()), mtime_ns=st.st_mtime_ns, size=st.st_size)

    def key(self) -> str:
        return f"{self.path}|{self.mtime_ns}|{self.size}"


class RefCache:
    """Кэш «локальный файл -> uuid» в разрезе проекта, на диске в JSON.

    Потокобезопасен: в панели один экземпляр делят все прогоны, и без
    внутреннего замка две одновременные записи перемешали бы JSON на диске.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._data: dict[str, dict[str, str]] = {}
        self._io = threading.Lock()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            self._data = json.loads(self.path.read_text(encoding="utf-8")) or {}
        except (json.JSONDecodeError, OSError):
            # Битый кэш — не повод падать: просто зальём заново.
            self._data = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def get(self, project_id: str, stat: RefStat) -> str | None:
        with self._io:
            return self._data.get(project_id, {}).get(stat.key())

    def put(self, project_id: str, stat: RefStat, uuid: str) -> None:
        with self._io:
            self._data.setdefault(project_id, {})[stat.key()] = uuid
            self._save()

    def forget_project(self, project_id: str) -> None:
        with self._io:
            if self._data.pop(project_id, None) is not None:
                self._save()


class RefResolver:
    """Отдаёт uuid для локального файла, заливая его лишь при необходимости."""

    def __init__(
        self,
        client: Any,
        log: RunLog,
        cache: RefCache,
        project_id: str,
        on_upload: Callable[[Path], None] | None = None,
        lock: Any = None,
    ) -> None:
        self.client = client
        self.log = log
        self.cache = cache
        self.project_id = project_id
        self.on_upload = on_upload
        # Общий на все вкладки замок. В параллельном режиме у каждого воркера
        # свой резолвер (он заливает через СВОЮ вкладку), но кэш и проект —
        # общие: без замка три вкладки одновременно не найдут файл в кэше и
        # зальют его трижды, намусорив дублями в библиотеке.
        self.lock = lock or _NullLock()
        self.uploads = 0
        self.reused = 0

    def resolve(self, raw: str | Path) -> RefHandle:
        """Спека -> RefHandle. Бросает FileNotFoundError для отсутствующих файлов.

        Порядок для путей: журнал этого проекта -> кэш заливок -> загрузка.
        Спеки lib: не трогают диск вовсе — прикрепление пойдёт поиском по
        библиотеке (текущего или указанного проекта) на фазе attach.
        """
        with self.lock:
            return self._resolve(raw)

    def _resolve(self, raw: str | Path) -> RefHandle:
        if isinstance(raw, str) and raw.startswith(LIB_PREFIX):
            name, project = parse_lib_spec(raw)
            if not name:
                raise ValueError(f"Пустое имя в референсе библиотеки: {raw!r}")
            self.reused += 1  # из библиотеки — по определению без загрузки
            return RefHandle(label=raw, search=name, picker_project=project)

        # @use: результат другой задачи. Ищется по журналу — uuid известен
        # с момента генерации, файл на диске не нужен вовсе (скачивание
        # результатов может быть выключено).
        if isinstance(raw, str) and raw.startswith(USE_PREFIX):
            body = raw[len(USE_PREFIX):].strip()
            if LIB_PROJECT_SEP in body:
                # Межпроектная форма 'ПРОЕКТ :: id': uuid ищем по журналу во
                # всех проектах (id задач глобально уникальны по конвенции
                # имён), а пикер при прикреплении переключится на проект
                # по имени — там этот uuid и лежит.
                project_name, _, job_id = body.partition(LIB_PROJECT_SEP)
                project_name, job_id = project_name.strip(), job_id.strip()
                uuid = self.log.uuid_for_job(job_id, project=None)
                if not uuid:
                    raise FileNotFoundError(
                        f"@use {project_name} :: {job_id}: в журнале нет успешного "
                        f"результата задачи {job_id!r} ни в одном проекте — она "
                        "генерилась через это приложение?"
                    )
                self.reused += 1
                return RefHandle(
                    label=f"@use {project_name} :: {job_id}",
                    uuid=uuid,
                    picker_project=project_name,
                )
            job_id = body
            uuid = self.log.uuid_for_job(job_id, project=self.project_id)
            if not uuid:
                raise FileNotFoundError(
                    f"@use {job_id}: в этом проекте ещё нет успешного результата "
                    f"задачи {job_id!r} — она должна отработать раньше. Если она "
                    f"из другого проекта, пиши '@use Имя проекта :: {job_id}'"
                )
            self.reused += 1
            return RefHandle(label=f"@use {job_id}", uuid=uuid)

        path = resolve_ref(raw)

        # 1. Результат прошлой задачи в этом же проекте.
        uuid = self.log.uuid_for_file(path, project=self.project_id)
        if uuid:
            self.reused += 1
            return RefHandle(label=path.name, uuid=uuid)

        # 2. Уже заливали в этот проект и файл с тех пор не менялся.
        stat = RefStat.of(path)
        uuid = self.cache.get(self.project_id, stat)
        if uuid:
            self.reused += 1
            return RefHandle(label=path.name, uuid=uuid)

        # 3. Заливаем и запоминаем.
        if self.on_upload:
            self.on_upload(path)
        uuid = self.client.upload_to_library(path)
        self.cache.put(self.project_id, stat, uuid)
        self.uploads += 1
        return RefHandle(label=path.name, uuid=uuid)
