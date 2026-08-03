"""Текстовая очередь: промпты с @-директивами.

Сценарий использования: промпты для сцен и видео пишет LLM, человек вставляет
их одним куском в панель (или сохраняет в .txt) — и очередь собирается сама.
Чтобы программа знала, какой кадр каким референсом прикреплять, в тексте
ставятся флаги-директивы.

Формат:

    # комментарий — игнорируется

    === IMG K1
    @ref C:\\RVL\\ZASTRYALO\\S01E02\\refs\\E01_0A_geroi4.png
    @ref C:\\RVL\\ZASTRYALO\\S01E02\\refs\\E02_0C_kost.png
    Vertical 9:16 frame. Многострочный промпт как есть,
    переводы строк сохраняются и уходят в Flow дословно.

    === VID K1_anim
    @use K1
    @duration 8
    Animate this image. ...

Заголовок блока: `=== IMG <id>` или `=== VID <id>` (русские синонимы
КАРТИНКА/ФОТО/ВИДЕО тоже понимаются). Директивы — строки, начинающиеся с @:

    @project <имя>    В НАЧАЛЕ ФАЙЛА, до первого блока: к какому проекту Flow
                      относится очередь. Перед прогоном программа откроет этот
                      проект (или создаст, если его нет).
    @ref <путь>       прикрепить локальный файл референсом (можно несколько)
    @product <имя>    прикрепить ВСЕ фото продукта из базы products/<имя>/
                      (или одно: @product rl410/front.jpg). При прогоне фото
                      сами заливаются в текущий проект Flow, один раз на проект.
    @lib <имя>        прикрепить картинку ИЗ БИБЛИОТЕКИ Flow по имени —
                      без файла на диске. Форма `@lib <проект> :: <имя>`
                      берёт её из библиотеки другого проекта.
    @use <id>         прикрепить РЕЗУЛЬТАТ другой задачи: из этого файла или
                      из прошлых прогонов в том же проекте (по журналу, файл
                      на диске не нужен). Форма `@use <проект> :: <id>` берёт
                      результат задачи из ДРУГОГО проекта. Не путать с @lib:
                      у сгенерированных элементов имя в библиотеке не
                      совпадает с id задачи.
    @duration N       длительность видео: 4 | 6 | 8 | 10
    @out <имя>        имя выходного файла (по умолчанию — id задачи)

Директивы вычищаются из промпта до отправки — во Flow уходит только текст.
Неизвестная @-строка — это ошибка, а не часть промпта: иначе опечатка в
директиве молча утекла бы в генерацию.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .queue import Job

HEADER_RE = re.compile(r"^===\s*(?P<kind>\S+)\s+(?P<id>[\w.\-]+)\s*$")
# Три и более перевода строки подряд (после вырезания комментариев) → два.
_BLANK_RUN_RE = re.compile(r"\n{3,}")

KIND_MAP = {
    "img": "image", "image": "image", "картинка": "image", "фото": "image", "кадр": "image",
    "vid": "video", "video": "video", "видео": "video",
}

ALLOWED_DURATIONS = {4, 6, 8, 10}

# Короткая справка для панели — показывается рядом с полем ввода.
SYNTAX_HELP = (
    "@project <имя> — первой строкой: проект Flow этой очереди (откроется/создастся сам).\n"
    "=== IMG <id> — блок картинки, === VID <id> — блок видео. Дальше директивы и промпт.\n"
    "@ref <путь> — файл с диска; @product <имя> — все фото продукта из products/<имя>/;\n"
    "@lib <имя> — картинка из библиотеки Flow по имени (@lib <проект> :: <имя> — из\n"
    "другого проекта; для СГЕНЕРИРОВАННОГО бери @use — имена в библиотеке не равны id);\n"
    "@use <id> — результат другой задачи (@use <проект> :: <id> — из другого проекта);\n"
    "@duration 4|6|8|10 — длительность видео; @out <имя> — имя результата.\n"
    "Директивы в Flow не уходят — только текст промпта. Строки с # — комментарии."
)

# Расширения файлов, которые считаем фотографиями продукта. Имя файла роли
# не играет — что лежит в папке продукта, то и прикрепляется.
PRODUCT_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"}

# Форматы, которые фотографией быть могут, но во Flow не загрузятся: HEIC с
# айфона и RAW с зеркалки браузер не покажет, вектор Flow не принимает.
# Молча их пропускать нельзя — иначе на папке с одними .heic программа скажет
# «нет изображений», хотя фотографии там очевидно есть.
UNSUPPORTED_IMAGE_EXTS = {
    ".heic", ".heif", ".tif", ".tiff", ".svg",
    ".raw", ".cr2", ".cr3", ".nef", ".arw", ".dng",
}

# Внутренний префикс referencе-спеки «из библиотеки». Разбирается в refs.py.
LIB_PREFIX = "lib:"
LIB_PROJECT_SEP = " :: "
# Референс «результат другой задачи»: резолвится по журналу прогонов в uuid
# медиа, файл на диске не требуется (скачивание может быть выключено).
USE_PREFIX = "use:"


@dataclass
class _Block:
    """Промежуточное представление одного блока при разборе."""

    kind: str
    id: str
    line: int
    refs: list[str] = field(default_factory=list)
    uses: list[str] = field(default_factory=list)
    products: list[tuple[int, str]] = field(default_factory=list)  # (строка, спека)
    duration: int | None = None
    out: str | None = None
    prompt_lines: list[str] = field(default_factory=list)


def parse(
    text: str,
    out_dir: str | Path = "out",
    products_dir: str | Path = "products",
) -> tuple[list[Job], list[str], dict[str, str | None]]:
    """Разобрать текст в список Job. Возвращает (задачи, ошибки, мета).

    мета: {"project": имя проекта Flow из @project или None}.
    При непустом списке ошибок задачи использовать нельзя: разбор атомарный,
    чтобы кривой блок не прошёл молча.
    """
    out_dir = Path(out_dir)
    products_dir = Path(products_dir)
    blocks: list[_Block] = []
    errors: list[str] = []
    meta: dict[str, str | None] = {"project": None}
    cur: _Block | None = None

    for n, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip("\r")
        stripped = line.strip()

        # @project — файловая директива, живёт до первого блока.
        if stripped.lower().startswith("@project"):
            rest = stripped[len("@project"):].strip()
            if cur is not None or blocks:
                errors.append(f"строка {n}: @project ставится в начале файла, до первого блока")
            elif not rest:
                errors.append(f"строка {n}: @project без имени проекта")
            elif meta["project"] is not None:
                errors.append(f"строка {n}: @project указан повторно")
            else:
                meta["project"] = rest
            continue

        m = HEADER_RE.match(stripped)
        if m:
            kind_raw = m.group("kind").lower()
            kind = KIND_MAP.get(kind_raw)
            if kind is None:
                errors.append(
                    f"строка {n}: неизвестный тип {m.group('kind')!r} — жду IMG или VID"
                )
                cur = None
                continue
            cur = _Block(kind=kind, id=m.group("id"), line=n)
            blocks.append(cur)
            continue

        # Комментарий — любая строка, начинающаяся с # (после пробелов), где бы
        # она ни стояла. Раньше решётка гасилась только до начала текста промпта,
        # и комментарий-заголовок следующего блока прилипал к промпту предыдущего;
        # Flow такие строки честно рисует текстом прямо на картинке.
        # Экранирование: строка, начинающаяся с \#, попадёт в промпт как #.
        if stripped.startswith("#"):
            continue
        if stripped.startswith("\\#"):
            line = line.replace("\\#", "#", 1)

        if not stripped and cur is None:
            continue

        if cur is None:
            errors.append(f"строка {n}: текст вне блока — сначала заголовок === IMG/VID <id>")
            continue

        if stripped.startswith("@"):
            _parse_directive(cur, stripped, n, errors)
            continue

        cur.prompt_lines.append(line)

    jobs = _blocks_to_jobs(blocks, out_dir, products_dir, errors)
    return jobs, errors, meta


def _parse_directive(block: _Block, line: str, n: int, errors: list[str]) -> None:
    parts = line.split(None, 1)
    keyword = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""

    if keyword == "@ref":
        if not rest:
            errors.append(f"строка {n}: @ref без пути")
        else:
            block.refs.append(rest)
    elif keyword == "@lib":
        if not rest:
            errors.append(f"строка {n}: @lib без имени элемента библиотеки")
        elif LIB_PROJECT_SEP in rest:
            project, name = rest.split(LIB_PROJECT_SEP, 1)
            if not project.strip() or not name.strip():
                errors.append(f"строка {n}: @lib — пустое имя проекта или элемента вокруг ' :: '")
            else:
                block.refs.append(LIB_PREFIX + rest)
        elif "::" in rest:
            # '::' без пробелов не является разделителем — почти наверняка
            # опечатка, которая молча превратила бы «проект :: имя» в имя.
            errors.append(
                f"строка {n}: @lib — разделитель проекта пишется как ' :: ' "
                f"(с пробелами с обеих сторон), получено {rest!r}"
            )
        else:
            # Внутри refs живёт спека с префиксом lib: — её разбирает resolver.
            block.refs.append(LIB_PREFIX + rest)
    elif keyword == "@product":
        if not rest:
            errors.append(f"строка {n}: @product без имени продукта")
        else:
            # Раскрытие в файлы — на этапе сборки: там же и валидация базы.
            block.products.append((n, rest))
    elif keyword == "@use":
        if "::" in rest:
            # Межпроектная форма: '@use ПРОЕКТ :: id' — результат задачи из
            # другого проекта. Валидация id внутри файла тут не нужна.
            # Ловим и кривые разделители (без пробелов, без id): иначе
            # опечатка молча превратилась бы в «id с двоеточиями».
            project, _, job_id = rest.partition(LIB_PROJECT_SEP)
            if (LIB_PROJECT_SEP in rest and project.strip() and job_id.strip()
                    and len(job_id.split()) == 1):
                block.uses.append(rest)
            else:
                errors.append(
                    f"строка {n}: @use с проектом пишется как "
                    f"'@use Имя проекта :: id_задачи' (разделитель ' :: ' "
                    f"с пробелами с обеих сторон), получено {rest!r}"
                )
        elif not rest or len(rest.split()) != 1:
            errors.append(f"строка {n}: @use ждёт ровно один id задачи")
        else:
            block.uses.append(rest)
    elif keyword == "@duration":
        try:
            dur = int(rest)
        except ValueError:
            errors.append(f"строка {n}: @duration ждёт число, получено {rest!r}")
            return
        if dur not in ALLOWED_DURATIONS:
            errors.append(
                f"строка {n}: @duration {dur} — допустимы {sorted(ALLOWED_DURATIONS)}"
            )
        elif block.kind != "video":
            errors.append(f"строка {n}: @duration имеет смысл только в блоке VID")
        else:
            block.duration = dur
    elif keyword == "@out":
        if not rest:
            errors.append(f"строка {n}: @out без имени")
        else:
            block.out = rest
    else:
        errors.append(
            f"строка {n}: неизвестная директива {keyword!r} "
            "(знаю @project, @ref, @product, @lib, @use, @duration, @out)"
        )


def _expand_product(spec: str, products_dir: Path, line: int, errors: list[str]) -> list[str]:
    """Раскрыть @product в список путей к фото.

    'rl410'            -> все изображения из products/rl410/, по алфавиту
    'rl410/front.jpg'  -> конкретный файл
    """
    spec = spec.replace("\\", "/").strip("/")
    if "/" in spec:
        path = products_dir / Path(spec)
        if not path.is_file():
            errors.append(f"строка {line}: @product — файла {path} нет")
            return []
        return [str(path)]

    folder = products_dir / spec
    if not folder.is_dir():
        have = sorted(p.name for p in products_dir.iterdir() if p.is_dir()) \
            if products_dir.is_dir() else []
        errors.append(
            f"строка {line}: @product {spec!r} — нет папки {folder}"
            + (f"; есть продукты: {', '.join(have)}" if have else
               f"; создай {folder} и положи туда фото")
        )
        return []
    files = sorted(
        (p for p in folder.iterdir()
         if p.is_file() and p.suffix.lower() in PRODUCT_IMAGE_EXTS),
        key=lambda p: p.name.lower(),
    )
    if not files:
        bad = sorted({p.suffix.lower() for p in folder.iterdir()
                      if p.is_file() and p.suffix.lower() in UNSUPPORTED_IMAGE_EXTS})
        if bad:
            errors.append(
                f"строка {line}: @product {spec!r} — в {folder} лежат только "
                f"{', '.join(e.lstrip('.').upper() for e in bad)}, а Flow их не принимает. "
                "Пересохрани фото в JPG или PNG."
            )
        else:
            errors.append(
                f"строка {line}: @product {spec!r} — в {folder} нет изображений "
                f"({'/'.join(sorted(e.lstrip('.') for e in PRODUCT_IMAGE_EXTS))})"
            )
        return []
    return [str(p) for p in files]


def _blocks_to_jobs(
    blocks: list[_Block], out_dir: Path, products_dir: Path, errors: list[str]
) -> list[Job]:
    # Карта id -> (позиция, тип, основа имени результата) для проверки @use.
    seen: dict[str, tuple[int, str, str]] = {}
    for i, b in enumerate(blocks):
        if b.id in seen:
            errors.append(f"строка {b.line}: повтор id {b.id!r} — id должны быть уникальны")
        else:
            stem = Path(b.out).stem if b.out else b.id
            seen[b.id] = (i, b.kind, stem)

    jobs: list[Job] = []
    for i, b in enumerate(blocks):
        # Вырезанные комментарии оставляют после себя лишние пустые строки —
        # схлопываем, чтобы в Flow не уезжали дыры в тексте.
        prompt = _BLANK_RUN_RE.sub("\n\n", "\n".join(b.prompt_lines)).strip()
        if not prompt:
            errors.append(f"строка {b.line}: блок {b.id!r} без текста промпта")

        refs: list[str] = []
        for use in b.uses:
            # Межпроектная форма 'ПРОЕКТ :: id' с локальными блоками не
            # пересекается — проверки порядка и типа только для своих.
            target = None if LIB_PROJECT_SEP in use else seen.get(use)
            if target is not None:
                pos, kind, _stem = target
                if kind == "video":
                    errors.append(
                        f"блок {b.id!r}: @use {use} указывает на видео — "
                        "референсом может быть только картинка"
                    )
                    continue
                if pos >= i:
                    errors.append(
                        f"блок {b.id!r}: @use {use} указывает на задачу, которая "
                        "генерится позже него — поставь её выше в файле"
                    )
                    continue
            # Резолв — на прогоне, по журналу (uuid медиа). Задачи, которых
            # нет в этом файле, тоже законны: результат прошлого прогона в
            # этом же или другом проекте. Файл на диске не нужен совсем.
            refs.append(f"{USE_PREFIX}{use}")

        refs += b.refs
        for line, spec in b.products:
            refs += _expand_product(spec, products_dir, line, errors)
        # дубли убираем, порядок сохраняем
        dedup: list[str] = []
        for r in refs:
            if r not in dedup:
                dedup.append(r)

        jobs.append(
            Job(
                id=b.id,
                kind=b.kind,  # type: ignore[arg-type]
                prompt=prompt,
                refs=dedup,
                duration=b.duration,
                output_name=b.out,
            )
        )
    return jobs
