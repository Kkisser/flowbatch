"""Смягчение промптов после отказа модерации Flow.

Три бэкенда, по нарастанию качества и цены:

  rules   — офлайн-правила из config.yaml: замены слов + смягчающая добавка.
            Бесплатно, мгновенно, работает без сети. Качество ограничено:
            правила не понимают смысл, только лексику.
  gemini  — Google Gemini API. У AI Studio есть бесплатный тариф с дневными
            лимитами (ключ: aistudio.google.com/apikey, переменная
            GEMINI_API_KEY в .env). Тот же Google-аккаунт, что и Flow.
  claude  — Anthropic Claude API. Бесплатного тарифа нет (опция для качества;
            переменная ANTHROPIC_API_KEY, пакет `pip install anthropic`).

LLM-бэкенды при любой ошибке (нет сети, кончился лимит, отказ) падают обратно
на правила — смягчение никогда не роняет задачу само по себе.
"""

from __future__ import annotations

import os
import re
from typing import Callable

import httpx

from .config import Config

# Инструкция переписчику. Бренд и реплики трогать нельзя — это продакшн-промпты.
LLM_INSTRUCTION = (
    "Ты редактируешь промпт для генерации изображений/видео, который отклонила "
    "автоматическая модерация Google. Перепиши его так, чтобы он прошёл модерацию:\n"
    "- сохрани структуру, сцену, стиль, тайминги и ВСЕ реплики дословно;\n"
    "- сохрани упоминания бренда (Revyline и модели устройств) без изменений;\n"
    "- переформулируй или убери потенциально проблемное: насилие, травмы, оружие, "
    "пугающее, анатомическое, суггестивное, жестокость — замени на безобидные "
    "мультяшные аналоги;\n"
    "- не добавляй пояснений и комментариев.\n"
    "Верни ТОЛЬКО переписанный промпт."
)


class SoftenError(RuntimeError):
    """Бэкенд не смог переписать промпт (сеть, лимит, отказ)."""


class RuleSoftener:
    """Офлайн-смягчение по правилам из конфига. Эскалация по попыткам."""

    name = "rules"

    def __init__(self, cfg: Config) -> None:
        self.replacements: dict[str, str] = dict(cfg.get("moderation.soften.replacements", {}) or {})
        self.suffix: str = str(cfg.get("moderation.soften.suffix", "") or "").strip()

    def soften(self, prompt: str, attempt: int) -> tuple[str, str]:
        """Вернуть (новый промпт, описание что сделано).

        Попытка 1: добавить смягчающую добавку в конец.
        Попытка 2+: ещё и применить замены слов.
        """
        out = prompt
        actions: list[str] = []

        if attempt >= 2 and self.replacements:
            applied: list[str] = []
            for bad, good in self.replacements.items():
                pattern = re.compile(rf"\b{re.escape(bad)}\b", re.IGNORECASE)
                if pattern.search(out):
                    out = pattern.sub(good, out)
                    applied.append(f"{bad}→{good or '∅'}")
            if applied:
                actions.append("замены: " + ", ".join(applied))

        if self.suffix and self.suffix.lower() not in out.lower():
            out = out.rstrip() + "\n\n" + self.suffix
            actions.append("добавлена смягчающая приписка")

        if not actions:
            actions.append("правилам нечего менять")
        return out, "; ".join(actions)


class GeminiSoftener:
    """Переписывание через Google Gemini API (ключ из AI Studio).

    Ключ передаётся заголовком x-goog-api-key, а НЕ параметром ?key= в URL.
    Две причины: новые ключи AI Studio создаются как «auth keys», для которых
    заголовок — рекомендованная форма (старые «standard keys» отключаются
    в сентябре 2026), и ключ не попадает в URL, а значит не оседает в логах
    прокси и в текстах ошибок.
    """

    name = "gemini"
    BASE = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self, cfg: Config) -> None:
        self.model = str(cfg.get("moderation.soften.gemini_model", "gemini-2.0-flash"))
        self.key = (os.getenv("GEMINI_API_KEY") or "").strip()

    @property
    def available(self) -> bool:
        return bool(self.key)

    def _headers(self) -> dict[str, str]:
        return {"x-goog-api-key": self.key, "Content-Type": "application/json"}

    def list_models(self) -> list[str]:
        """Имена моделей, доступных этому ключу. Для диагностики."""
        if not self.key:
            raise SoftenError("нет GEMINI_API_KEY")
        try:
            r = httpx.get(f"{self.BASE}/models", headers=self._headers(), timeout=30)
        except Exception as exc:  # noqa: BLE001
            raise SoftenError(f"Gemini недоступен: {type(exc).__name__}") from exc
        if r.status_code != 200:
            raise SoftenError(f"Gemini HTTP {r.status_code}: {_api_error(r.text)}")
        out = []
        for m in r.json().get("models", []):
            name = str(m.get("name", "")).removeprefix("models/")
            if name and "generateContent" in (m.get("supportedGenerationMethods") or []):
                out.append(name)
        return sorted(out)

    def soften(self, prompt: str, attempt: int) -> tuple[str, str]:
        if not self.key:
            raise SoftenError("нет GEMINI_API_KEY")
        harder = (
            "\nЭто уже НЕ ПЕРВАЯ попытка — предыдущее смягчение модерация тоже "
            "отклонила. Переписывай агрессивнее." if attempt >= 2 else ""
        )
        try:
            r = httpx.post(
                f"{self.BASE}/models/{self.model}:generateContent",
                headers=self._headers(),
                json={
                    "contents": [{
                        "parts": [{"text": f"{LLM_INSTRUCTION}{harder}\n\nПромпт:\n{prompt}"}],
                    }],
                },
                timeout=45,
            )
        except Exception as exc:  # noqa: BLE001
            raise SoftenError(f"Gemini недоступен: {type(exc).__name__}") from exc
        if r.status_code != 200:
            raise SoftenError(f"Gemini HTTP {r.status_code}: {_api_error(r.text)}")
        data = r.json()
        try:
            parts = data["candidates"][0]["content"]["parts"]
            text = "\n".join(p.get("text", "") for p in parts).strip()
        except (KeyError, IndexError, ValueError) as exc:
            # Пустой candidates обычно означает, что промпт зарубила уже
            # модерация самого Gemini — это стоит показать открытым текстом.
            reason = (data.get("promptFeedback") or {}).get("blockReason")
            if reason:
                raise SoftenError(f"Gemini сам заблокировал промпт ({reason})") from exc
            raise SoftenError("Gemini вернул неожиданный ответ") from exc
        if not text:
            raise SoftenError("Gemini вернул пустой текст")
        return text, f"переписано Gemini ({self.model})"


def _api_error(body: str) -> str:
    """Короткое сообщение об ошибке API без утечки тела целиком."""
    try:
        import json

        err = json.loads(body).get("error", {})
        msg = str(err.get("message", ""))[:180]
        status = err.get("status", "")
        return f"{status}: {msg}" if status else msg or body[:180]
    except Exception:  # noqa: BLE001
        return body[:180]


class ClaudeSoftener:
    """Переписывание через Anthropic Claude API (платный, опционально)."""

    name = "claude"

    def __init__(self, cfg: Config) -> None:
        self.model = str(cfg.get("moderation.soften.claude_model", "claude-opus-5"))
        self.key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()

    @property
    def available(self) -> bool:
        if not self.key:
            return False
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return False
        return True

    def soften(self, prompt: str, attempt: int) -> tuple[str, str]:
        try:
            import anthropic
        except ImportError as exc:
            raise SoftenError("пакет anthropic не установлен (pip install anthropic)") from exc
        if not self.key:
            raise SoftenError("нет ANTHROPIC_API_KEY")
        harder = (
            "\nЭто уже НЕ ПЕРВАЯ попытка — предыдущее смягчение модерация тоже "
            "отклонила. Переписывай агрессивнее." if attempt >= 2 else ""
        )
        try:
            client = anthropic.Anthropic(api_key=self.key)
            resp = client.messages.create(
                model=self.model,
                max_tokens=8000,
                system=LLM_INSTRUCTION + harder,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # noqa: BLE001
            raise SoftenError(f"Claude недоступен: {type(exc).__name__}") from exc
        if resp.stop_reason == "refusal":
            raise SoftenError("Claude отказался переписывать этот промпт")
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        if not text:
            raise SoftenError("Claude вернул пустой текст")
        return text, f"переписано Claude ({self.model})"


class Softener:
    """Композиция: выбранный LLM-бэкенд с фоллбэком на правила."""

    def __init__(self, primary, rules: RuleSoftener, log: Callable[[str], None] | None = None) -> None:
        self.primary = primary  # None | GeminiSoftener | ClaudeSoftener
        self.rules = rules
        self._log = log or (lambda s: None)

    @property
    def name(self) -> str:
        return self.primary.name if self.primary is not None else self.rules.name

    def soften(self, prompt: str, attempt: int) -> tuple[str, str]:
        if self.primary is not None:
            try:
                return self.primary.soften(prompt, attempt)
            except SoftenError as exc:
                self._log(f"смягчитель {self.primary.name} не сработал ({exc}) — падаю на правила")
        return self.rules.soften(prompt, attempt)


def build_softener(cfg: Config, log: Callable[[str], None] | None = None) -> Softener | None:
    """Собрать смягчитель по конфигу. None — смягчение выключено."""
    if not cfg.get("moderation.soften.enabled", True):
        return None
    backend = str(cfg.get("moderation.soften.backend", "auto")).strip().lower()
    rules = RuleSoftener(cfg)

    if backend == "rules":
        return Softener(None, rules, log)

    gemini = GeminiSoftener(cfg)
    claude = ClaudeSoftener(cfg)
    if backend == "gemini":
        return Softener(gemini if gemini.available else None, rules, log)
    if backend == "claude":
        return Softener(claude if claude.available else None, rules, log)
    # auto: бесплатный Gemini в приоритете, Claude вторым, правила всегда в запасе.
    primary = gemini if gemini.available else (claude if claude.available else None)
    return Softener(primary, rules, log)
