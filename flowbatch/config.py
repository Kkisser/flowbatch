"""Загрузка и валидация config.yaml."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

# Жёсткий потолок параллельности — выше не поднимаем ни при каких настройках.
MAX_CONCURRENCY = 3


class Config:
    """Тонкая обёртка над словарём конфига с доступом по точечному пути."""

    def __init__(self, data: dict[str, Any], path: Path) -> None:
        self._data = data
        self.path = path

    @classmethod
    def load(cls, path: str | Path = "config.yaml") -> "Config":
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Не найден конфиг: {p.resolve()}")
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        cfg = cls(data, p)
        cfg._validate()
        return cfg

    def set(self, dotted: str, value: Any) -> None:
        """Переопределить значение по точечному пути (например, из веб-панели)."""
        node = self._data
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def get(self, dotted: str, default: Any = None) -> Any:
        """Достать значение по пути вида 'antiban.concurrency'."""
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def _validate(self) -> None:
        conc = int(self.get("antiban.concurrency", 1) or 1)
        if conc > MAX_CONCURRENCY:
            raise ValueError(
                f"antiban.concurrency={conc} превышает потолок {MAX_CONCURRENCY}. "
                "Это защита от бана — правь конфиг."
            )
        if conc < 1:
            raise ValueError("antiban.concurrency должен быть >= 1")

    # --- часто используемые срезы ---

    @property
    def selectors(self) -> dict[str, str]:
        return dict(self.get("selectors", {}) or {})

    @property
    def locale(self) -> dict[str, str]:
        return dict(self.get("locale", {}) or {})

    def out_dir(self) -> Path:
        return Path(self.get("paths.out_dir", "out"))

    def screenshots_dir(self) -> Path:
        return Path(self.get("paths.screenshots_dir", "screenshots"))

    def runs_log(self) -> Path:
        return Path(self.get("paths.runs_log", "runs.jsonl"))

    def products_dir(self) -> Path:
        return Path(self.get("paths.products_dir", "products"))

    def prompts_dir(self) -> Path:
        return Path(self.get("paths.prompts_dir", "prompts"))

    def resolve_queue(self, source: str) -> Path:
        """Путь к файлу очереди: как указан, иначе из папки промптов.

        Позволяет писать в панели просто «MISS_ULYBKA.flow.txt» вместо
        полного пути и при этом не ломает уже сохранённые пути и абсолютные.
        """
        raw = (source or "").strip().strip('"')
        if not raw:
            raise ValueError("не указан путь к очереди")
        direct = Path(raw)
        if direct.exists():
            return direct
        inside = self.prompts_dir() / direct.name
        if inside.exists():
            return inside
        raise FileNotFoundError(
            f"не найден файл очереди: {direct} "
            f"(искал и в папке промптов: {inside})"
        )
