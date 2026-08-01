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
    "автоматическая модерация Google.\n"
    "\n"
    "ГЛАВНОЕ ПРАВИЛО: меняй МИНИМУМ. Трогай только то, что реально могло "
    "смутить модерацию. Всё остальное копируй дословно, слово в слово.\n"
    "\n"
    "НЕЛЬЗЯ МЕНЯТЬ НИ ПРИ КАКИХ УСЛОВИЯХ:\n"
    "- реплики персонажей (текст в кавычках «...») — дословно;\n"
    "- ИМЕНА И ОБОЗНАЧЕНИЯ ПЕРСОНАЖЕЙ (THE BONE, VALERA, SHESTYORKA, "
    "THE TONGUE и любые другие) — и в описаниях действий, и в строках диалога. "
    "Не заменяй их на обезличенное вроде SECOND CHARACTER: по этим меткам "
    "модель понимает, кто говорит и кто в кадре;\n"
    "- бренд и модели устройств (Revyline, RL 230, RL 410 и т.п.);\n"
    "- тайминги (0.0 to 2.0 и т.д.), структуру блоков, названия секций "
    "(CAMERA, ACTION TIMING, DIALOGUE, SOUND, CHARACTER LOCK, ABSOLUTE RULE);\n"
    "- запреты и ограничения (NO MUSIC, no on-screen text, язык вывесок);\n"
    "- лексику мира сцены, если она сама по себе безобидна (enamel, crevice, "
    "molar, mine bunker) — это визуальный сеттинг, а не проблема.\n"
    "\n"
    "МЕНЯЙ ТОЛЬКО ЭТО: насилие, травмы, кровь, оружие, жестокость, пугающее, "
    "анатомическое, суггестивное — заменяй на безобидные мультяшные аналоги, "
    "сохраняя смысл действия.\n"
    "\n"
    "Не добавляй пояснений, преамбул и комментариев. "
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


class OllamaSoftener:
    """Переписывание локальной моделью через Ollama.

    Бесплатно и без сети наружу: ни квот, ни ключей, ни утечки промптов
    на чужие серверы. Цена — время: модель считает на твоей видеокарте,
    поэтому она должна целиком влезать в VRAM, иначе часть слоёв уходит
    в оперативку и скорость падает в разы.
    """

    name = "ollama"

    def __init__(self, cfg: Config) -> None:
        self.model = str(cfg.get("moderation.soften.ollama_model", "gemma4:12b"))
        self.host = str(
            cfg.get("moderation.soften.ollama_host", "") or os.getenv("OLLAMA_HOST", "")
            or "http://localhost:11434"
        ).rstrip("/")
        if not self.host.startswith(("http://", "https://")):
            self.host = f"http://{self.host}"
        self.timeout = int(cfg.get("moderation.soften.ollama_timeout_sec", 300))
        self.num_ctx = int(cfg.get("moderation.soften.ollama_num_ctx", 8192))
        self.num_predict = int(cfg.get("moderation.soften.ollama_num_predict", 4096))

    @property
    def available(self) -> bool:
        """Сервер отвечает И нужная модель скачана."""
        try:
            return self.model in self.list_models()
        except SoftenError:
            return False

    def list_models(self) -> list[str]:
        try:
            r = httpx.get(f"{self.host}/api/tags", timeout=10)
        except Exception as exc:  # noqa: BLE001
            raise SoftenError(
                f"Ollama не отвечает на {self.host} ({type(exc).__name__}) — "
                "запусти `ollama serve`"
            ) from exc
        if r.status_code != 200:
            raise SoftenError(f"Ollama HTTP {r.status_code}")
        return sorted(m.get("name", "") for m in r.json().get("models", []))

    def _payload(self, prompt: str, harder: str, think: bool) -> dict:
        # num_ctx: промпты бывают по 4000+ символов, а дефолтное окно Ollama
        # мало — длинный промпт молча обрезался бы вместе с инструкцией.
        # num_predict: ответ примерно равен входу, нужен запас.
        body = {
            "model": self.model,
            "stream": False,
            "messages": [
                {"role": "system", "content": LLM_INSTRUCTION + harder},
                {"role": "user", "content": prompt},
            ],
            # Низкая температура: нужна точечная правка, а не творческий
            # пересказ — иначе поедут реплики и имена персонажей.
            "options": {
                "temperature": 0.3,
                "num_ctx": self.num_ctx,
                "num_predict": self.num_predict,
            },
        }
        if not think:
            # «Думающие» модели (gemma4, qwen3) тратят бюджет токенов на
            # рассуждения и могут вернуть пустой content. Нам рассуждения
            # не нужны — это переписывание, а не задача на логику.
            body["think"] = False
        return body

    def soften(self, prompt: str, attempt: int) -> tuple[str, str]:
        harder = (
            "\nЭто уже НЕ ПЕРВАЯ попытка — предыдущее смягчение модерация тоже "
            "отклонила. Переписывай агрессивнее." if attempt >= 2 else ""
        )
        data = None
        for think in (False, True):  # без размышлений; если модель против — с ними
            try:
                r = httpx.post(
                    f"{self.host}/api/chat",
                    json=self._payload(prompt, harder, think),
                    timeout=self.timeout,
                )
            except Exception as exc:  # noqa: BLE001
                raise SoftenError(f"Ollama недоступна: {type(exc).__name__}") from exc
            if r.status_code == 200:
                data = r.json()
                break
            # Модель не поддерживает отключение размышлений — пробуем с ними.
            if think is False and "think" in r.text.lower():
                continue
            raise SoftenError(f"Ollama HTTP {r.status_code}: {r.text[:180]}")

        msg = (data or {}).get("message") or {}
        text = str(msg.get("content", "")).strip()
        if not text:
            if str(msg.get("thinking", "")).strip():
                raise SoftenError(
                    f"{self.model} израсходовала весь бюджет ответа на размышления — "
                    "подними moderation.soften.ollama_num_predict или возьми "
                    "не-«думающую» модель"
                )
            raise SoftenError("Ollama вернула пустой текст")
        return _strip_fence(text), f"переписано Ollama ({self.model})"


def _strip_fence(text: str) -> str:
    """Снять ```-обёртку, которую локальные модели любят добавлять."""
    lines = text.splitlines()
    if lines and lines[0].lstrip().startswith("```"):
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return text


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

    ollama = OllamaSoftener(cfg)
    gemini = GeminiSoftener(cfg)
    claude = ClaudeSoftener(cfg)
    if backend == "ollama":
        return Softener(ollama if ollama.available else None, rules, log)
    if backend == "gemini":
        return Softener(gemini if gemini.available else None, rules, log)
    if backend == "claude":
        return Softener(claude if claude.available else None, rules, log)

    # auto: локальная Ollama первой — бесплатно, без квот и без отправки
    # промптов наружу; затем Gemini, затем Claude; правила всегда в запасе.
    for candidate in (ollama, gemini, claude):
        if candidate.available:
            return Softener(candidate, rules, log)
    return Softener(None, rules, log)


def backend_status(cfg: Config) -> list[dict[str, object]]:
    """Состояние всех бэкендов — для панели и soften-test."""
    out: list[dict[str, object]] = [
        {"id": "rules", "title": "Правила (офлайн)", "available": True,
         "detail": "работает всегда, без сети и ключей"},
    ]
    ollama = OllamaSoftener(cfg)
    try:
        models = ollama.list_models()
        if ollama.model in models:
            detail = f"модель {ollama.model} готова"
            ok = True
        else:
            ok = False
            detail = (f"сервер есть, но модели {ollama.model} нет"
                      + (f" (скачаны: {', '.join(models[:4])})" if models else " (ничего не скачано)"))
    except SoftenError as exc:
        ok, detail = False, str(exc)
    out.append({"id": "ollama", "title": f"Ollama ({ollama.model})", "available": ok, "detail": detail})

    gem = GeminiSoftener(cfg)
    out.append({"id": "gemini", "title": f"Gemini ({gem.model})", "available": gem.available,
                "detail": "ключ в .env" if gem.available else "нет GEMINI_API_KEY"})
    cl = ClaudeSoftener(cfg)
    out.append({"id": "claude", "title": f"Claude ({cl.model})", "available": cl.available,
                "detail": "ключ в .env" if cl.available else "нет ANTHROPIC_API_KEY или пакета anthropic"})
    return out
