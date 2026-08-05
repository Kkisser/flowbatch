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
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from .config import Config
from .scrub import scrub

# Инструкция переписчику. Бренд и реплики трогать нельзя — это продакшн-промпты.
LLM_INSTRUCTION = (
    "Ты редактируешь промпт для генерации изображений/видео, который отклонила "
    "автоматическая модерация Google.\n"
    "\n"
    "ГЛАВНОЕ ПРАВИЛО: меняй МИНИМУМ. Трогай только то, что реально могло "
    "смутить модерацию. Всё остальное копируй дословно, слово в слово.\n"
    "\n"
    "НЕЛЬЗЯ МЕНЯТЬ НИ ПРИ КАКИХ УСЛОВИЯХ:\n"
    "- реплики персонажей: и текст в кавычках «...», и защищённые метки вида "
    "§R1§, §R2§ (это вырезанные реплики, они подставятся обратно "
    "автоматически) — копируй такие метки на их местах как есть;\n"
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

# Отдельная инструкция для отказа «из-за интересов сторонних поставщиков
# контента»: Flow счёл, что промпт похож на ЧУЖОЙ контент (сериал, фильм,
# знаменитость, чужой бренд). Смягчать жестокость тут бессмысленно — нужно
# убирать сходство, то есть переписывать кардинально.
THIRD_PARTY_INSTRUCTION = (
    "Ты редактируешь промпт для генерации видео/изображений, который Google "
    "отклонил с формулировкой «из-за интересов сторонних поставщиков контента». "
    "Это значит: промпт напоминает ЧУЖОЙ узнаваемый контент — сериал, фильм, "
    "мультфильм, знаменитость, чужой бренд или персонажа.\n"
    "\n"
    # Блок запретов стоит ПЕРВЫМ и сформулирован жёстче, чем «перепиши
    # кардинально» ниже: проверено на gemma3:12b — в обратном порядке модель
    # три раза из трёх «кардинально» переписывала и реплики тоже.
    "НЕЛЬЗЯ МЕНЯТЬ НИ ПРИ КАКИХ УСЛОВИЯХ:\n"
    "- реплики персонажей: и текст в кавычках «...», и защищённые метки вида "
    "§R1§, §R2§ (это вырезанные реплики, они подставятся обратно "
    "автоматически) — копируй такие метки на их местах как есть. Реплики "
    "НЕ являются чужим контентом;\n"
    "- ИМЕНА И ОБОЗНАЧЕНИЯ ПЕРСОНАЖЕЙ в метках строк (THE BONE, VALERA и "
    "другие) — по ним модель понимает, кто говорит;\n"
    "- НАШ бренд и модели устройств (Revyline, RL 230, RL 410 и т.п.) — "
    "это наш собственный бренд;\n"
    "- тайминги (0.0 to 2.0 и т.д.), структуру блоков, названия секций "
    "(CAMERA, ACTION, DIALOGUE и т.п.), запреты (NO MUSIC, no on-screen text);\n"
    "- суть происходящего в кадре (кто что делает).\n"
    "\n"
    "ВСЁ ОСТАЛЬНОЕ — ОПИСАНИЯ СЦЕНЫ — ПЕРЕПИШИ КАРДИНАЛЬНО, убрав сходство "
    "с чужим:\n"
    "- смени сеттинг, атмосферу и узнаваемые образы на собственные, "
    "непохожие на известные франшизы;\n"
    "- убери или переименуй всё, что отсылает к чужим вселенным, названиям, "
    "актёрам и медийным персонам;\n"
    "- визуальный стиль опиши нейтрально, без отсылок к конкретным "
    "фильмам/студиям («в стиле Пиксар» — нельзя).\n"
    "\n"
    "Не добавляй пояснений, преамбул и комментариев. "
    "Верни ТОЛЬКО переписанный промпт."
)

# Реплики в наших промптах — в «ёлочках». Латинские "..." не трогаем:
# в них обычно технические термины, а не диалог.
_DIALOGUE_RE = re.compile(r"«([^»]{1,500})»")
# Реплики бывают и в прямых кавычках — так их пишет часть наших очередей
# (PODMENA целиком). Такие берём ТОЛЬКО с кириллицей внутри: в английском
# тексте промпта в прямых кавычках попадаются технические строки, и защищать
# их как реплики нельзя.
_DIALOGUE_STRAIGHT_RE = re.compile(r'"([^"\n]{1,500})"')
_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")


def _dialogue_spans(text: str) -> list[tuple[int, int, str]]:
    """Куски диалога в тексте: (начало, конец, содержимое без кавычек)."""
    spans: list[tuple[int, int, str]] = [
        (m.start(), m.end(), m.group(1)) for m in _DIALOGUE_RE.finditer(text)
    ]
    spans += [
        (m.start(), m.end(), m.group(1))
        for m in _DIALOGUE_STRAIGHT_RE.finditer(text)
        if _CYRILLIC_RE.search(m.group(1))
    ]
    spans.sort()
    return spans

# Номер попытки, на которой вместо переписывания отправляются голые реплики.
DIALOGUE_ONLY_ATTEMPT = 3

# Насколько ответ модели может отличаться по длине от исходного промпта,
# чтобы считаться переписанным промптом, а не чем-то ещё.
#
# Повод конкретный: gemma-4 через Gemini API не переписывает промпт, а
# ПЕРЕСКАЗЫВАЕТ инструкцию списком — ответ выходил в 14-17 раз длиннее
# исходника. Без этой проверки такой пересказ ушёл бы во Flow как
# «смягчённый промпт» и сжёг бы генерацию. Нижняя граница ловит обрезанный
# ответ и отказ вида «I can't help with that».
#
# Настоящие переписывания на замерах укладывались в 0.97-1.03, так что
# запас здесь огромный и добросовестный вариант отбросить нельзя.
MIN_REWRITE_RATIO = 0.35
MAX_REWRITE_RATIO = 2.5


def extract_dialogue(prompt: str) -> str:
    """Достать из промпта только реплики «...» — по одной на строку.

    Попытка №3 лестницы смягчения: иногда Flow пропускает голый диалог
    без описаний сцены, на которые и ругалась модерация.
    """
    lines = [inner.strip() for _, _, inner in _dialogue_spans(prompt)]
    return "\n".join(dict.fromkeys(line for line in lines if line))


def _mask_dialogue(prompt: str) -> tuple[str, dict[str, str]]:
    """Спрятать реплики за плейсхолдеры §R1§, §R2§…, сохранив вид кавычек.

    Инструкция «сохрани реплики дословно» — это надежда на дисциплину модели,
    и на third_party-инструкции gemma3:12b её не оправдала (0/3 на смоуке:
    «Выселение…» → «Переезд…»). Маска — гарантия: текст реплик в LLM вообще
    не уходит, портить нечего.
    """
    reps: dict[str, str] = {}
    out: list[str] = []
    last = 0
    for start, end, inner in _dialogue_spans(prompt):
        if start < last:
            continue  # перекрытие кавычек — пропускаем, чтобы не порвать текст
        key = f"§R{len(reps) + 1}§"
        reps[key] = inner
        open_q = prompt[start]
        close_q = "»" if open_q == "«" else '"'
        out.append(prompt[last:start])
        out.append(f"{open_q}{key}{close_q}")
        last = end
    out.append(prompt[last:])
    return "".join(out), reps


def _unmask_dialogue(text: str, reps: dict[str, str]) -> tuple[str, list[str]]:
    """Вернуть реплики на место. Потерянные моделью — дописать в конец."""
    for key, line in reps.items():
        text = text.replace(key, line)
    lost = [line for key, line in reps.items() if line not in text]
    if lost:
        text = text.rstrip() + "\n\nDIALOGUE (verbatim):\n" + "\n".join(
            f"«{line}»" for line in lost
        )
    return text, lost


def moderation_category(cfg: Config, detail: str) -> str:
    """unusual | third_party | policy — по тексту ошибки со страницы.

    unusual проверяется первым: «подозрительная активность» — это не про
    содержание промпта, переписывать там нечего, задача просто ждёт и
    перезапускается как есть.
    """
    low = (detail or "").lower()
    for p in cfg.get("moderation.phrases_unusual", []) or []:
        if str(p).strip() and str(p).lower() in low:
            return "unusual"
    for p in cfg.get("moderation.phrases_third_party", []) or []:
        if str(p).strip() and str(p).lower() in low:
            return "third_party"
    return "policy"


def _temp(attempt: int) -> float:
    """Температура: 0.2 на первых двух попытках, дальше растёт до 0.9.

    Первые правки должны быть точечными, без творчества — низкая температура
    держит модель близко к исходному тексту. Но если один и тот же промпт
    отклоняют раз за разом, детерминированная модель выдавала бы почти
    одинаковые варианты — с попытки 4 подмешиваем разнообразие.
    """
    if attempt <= 2:
        return 0.2
    return round(min(0.9, 0.1 * attempt), 2)


def _instruction(attempt: int, category: str) -> str:
    """Собрать системную инструкцию с эскалацией по номеру попытки."""
    base = THIRD_PARTY_INSTRUCTION if category == "third_party" else LLM_INSTRUCTION
    if attempt < 2:
        return base
    extra = (
        f"\nЭто попытка №{attempt}: предыдущие {attempt - 1} вариант(а) модерация "
        "тоже отклонила. "
    )
    if attempt <= 4:
        extra += "Переписывай агрессивнее."
    elif attempt <= 6:
        extra += (
            "Переписывай значительно смелее: упрощай сцену, убирай спорные "
            "детали целиком, а не подбирай им синонимы."
        )
    else:
        extra += (
            "Действуй радикально: сохрани реплики, бренд и суть кадра, а всё "
            "остальное перескажи максимально нейтрально и коротко — как "
            "безобидную бытовую сцену."
        )
    return base + extra


class SoftenError(RuntimeError):
    """Бэкенд не смог переписать промпт (сеть, лимит, отказ)."""


class RuleSoftener:
    """Офлайн-смягчение по правилам из конфига. Эскалация по попыткам."""

    name = "rules"

    def __init__(self, cfg: Config) -> None:
        self.replacements: dict[str, str] = dict(cfg.get("moderation.soften.replacements", {}) or {})
        self.suffix: str = str(cfg.get("moderation.soften.suffix", "") or "").strip()

    def soften(self, prompt: str, attempt: int, category: str = "policy") -> tuple[str, str]:
        """Вернуть (новый промпт, описание что сделано).

        Попытка 1: добавить смягчающую добавку в конец.
        Попытка 2+: ещё и применить замены слов.
        category правила игнорируют: словарные замены не умеют убирать
        сходство с чужим контентом.
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

    # Коды, при которых модель не виновата и надо просто взять следующую:
    # 429 — кончилась квота, 503 — модель перегружена, 404 — снята.
    SWITCH_CODES = (404, 429, 503)

    def __init__(self, cfg: Config) -> None:
        models = [
            str(m).strip()
            for m in (cfg.get("moderation.soften.gemini_models", []) or [])
            if str(m).strip()
        ]
        if not models:
            models = [str(cfg.get("moderation.soften.gemini_model", "gemini-flash-latest"))]
        self.models = models
        # Индекс текущей модели. Двигается вперёд при 429/503/404 и там и
        # остаётся: если у первой кончилась дневная квота, долбиться в неё
        # каждым промптом бессмысленно.
        self._idx = 0
        self.key = (os.getenv("GEMINI_API_KEY") or "").strip()

    @property
    def model(self) -> str:
        return self.models[min(self._idx, len(self.models) - 1)]

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

    def soften(self, prompt: str, attempt: int, category: str = "policy",
               log: Callable[[str], None] | None = None) -> tuple[str, str]:
        """Переписать промпт, при необходимости перебрав цепочку моделей."""
        if not self.key:
            raise SoftenError("нет GEMINI_API_KEY")
        say = log or (lambda s: None)
        last = ""
        # Начинаем с текущей модели: если предыдущая уже выбыла по квоте,
        # возвращаться к ней смысла нет до конца прогона.
        while self._idx < len(self.models):
            model = self.models[self._idx]
            try:
                r = httpx.post(
                    f"{self.BASE}/models/{model}:generateContent",
                    headers=self._headers(),
                    json={
                        "contents": [{
                            "parts": [{
                                "text": f"{_instruction(attempt, category)}\n\nПромпт:\n{prompt}"
                            }],
                        }],
                        # Прогрев с попытками: на поздних нужен другой текст,
                        # а не тот же самый отказ слово в слово.
                        "generationConfig": {"temperature": _temp(attempt)},
                    },
                    timeout=90,
                )
            except Exception as exc:  # noqa: BLE001
                raise SoftenError(f"Gemini недоступен: {type(exc).__name__}") from exc

            if r.status_code in self.SWITCH_CODES and self._idx + 1 < len(self.models):
                why = {404: "снята с обслуживания", 429: "кончилась квота",
                       503: "перегружена"}.get(r.status_code, f"HTTP {r.status_code}")
                self._idx += 1
                say(f"Gemini: {model} — {why}, перехожу на {self.models[self._idx]}")
                continue
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
                # Пустой ответ бывает у «думающих» моделей, съевших лимит на
                # рассуждения. Следующая модель обычно справляется.
                last = f"{model} вернула пустой текст"
                if self._idx + 1 < len(self.models):
                    self._idx += 1
                    say(f"Gemini: {last}, перехожу на {self.models[self._idx]}")
                    continue
                raise SoftenError(last)
            return text, f"переписано Gemini ({model})"

        raise SoftenError(last or "цепочка моделей Gemini исчерпана")


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

    def _payload(self, prompt: str, attempt: int, category: str, think: bool) -> dict:
        # num_ctx: промпты бывают по 4000+ символов, а дефолтное окно Ollama
        # мало — длинный промпт молча обрезался бы вместе с инструкцией.
        # num_predict: ответ примерно равен входу, нужен запас.
        body = {
            "model": self.model,
            "stream": False,
            "messages": [
                {"role": "system", "content": _instruction(attempt, category)},
                {"role": "user", "content": prompt},
            ],
            # Температура низкая на первой попытке (точечная правка) и растёт
            # с номером — см. _temp().
            "options": {
                "temperature": _temp(attempt),
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

    def soften(self, prompt: str, attempt: int, category: str = "policy") -> tuple[str, str]:
        data = None
        for think in (False, True):  # без размышлений; если модель против — с ними
            try:
                r = httpx.post(
                    f"{self.host}/api/chat",
                    json=self._payload(prompt, attempt, category, think),
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

    def soften(self, prompt: str, attempt: int, category: str = "policy") -> tuple[str, str]:
        try:
            import anthropic
        except ImportError as exc:
            raise SoftenError("пакет anthropic не установлен (pip install anthropic)") from exc
        if not self.key:
            raise SoftenError("нет ANTHROPIC_API_KEY")
        try:
            client = anthropic.Anthropic(api_key=self.key)
            resp = client.messages.create(
                model=self.model,
                max_tokens=8000,
                system=_instruction(attempt, category),
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


def find_claude_exe() -> str | None:
    """Где лежит claude CLI: PATH, затем обычные места установки на Windows.

    ВНИМАНИЕ: десктопное приложение Claude (MSIX в WindowsApps) — это НЕ CLI,
    у него нет неинтерактивного режима. Нужен отдельно поставленный
    Claude Code CLI.
    """
    exe = shutil.which("claude")
    if exe:
        return exe
    home = Path.home()
    for p in (
        home / ".local" / "bin" / "claude.exe",
        home / ".local" / "bin" / "claude",
        Path(os.getenv("LOCALAPPDATA", "")) / "Programs" / "claude" / "claude.exe",
        Path(os.getenv("APPDATA", "")) / "npm" / "claude.cmd",
    ):
        try:
            if p.is_file():
                return str(p)
        except OSError:
            continue
    return None


class ClaudeCliSoftener:
    """Переписывание через Claude Code CLI в неинтерактивном режиме (-p).

    Главное отличие от ClaudeSoftener: работает на ПОДПИСКЕ, а не на
    API-кредитах — ANTHROPIC_API_KEY не нужен вовсе. Предельная стоимость
    нулевая, пока не упёрся в лимиты подписки, поэтому в цепочке он стоит
    после бесплатных Ollama и Gemini, но перед платным API.

    Цена — время: каждый вызов поднимает отдельный процесс, это заметно
    медленнее HTTP-запроса к локальной Ollama.
    """

    name = "claude-cli"

    def __init__(self, cfg: Config) -> None:
        self.exe = str(cfg.get("moderation.soften.claude_cli_path", "") or "").strip() \
            or find_claude_exe()
        self.model = str(cfg.get("moderation.soften.claude_cli_model", "opus") or "opus")
        self.effort = str(cfg.get("moderation.soften.claude_cli_effort", "") or "").strip()
        self.fallback = str(cfg.get("moderation.soften.claude_cli_fallback", "") or "").strip()
        self.timeout = int(cfg.get("moderation.soften.claude_cli_timeout_sec", 300))
        self.extra_args = [
            str(a) for a in (cfg.get("moderation.soften.claude_cli_args", []) or [])
        ]

    def _flags(self, attempt: int) -> list[str]:
        """Флаги CLI. Проверены на 2.1.222: --model, --effort, --fallback-model."""
        flags = ["--model", self.model]
        # На поздних ступенях сцену надо ломать радикально — там усилие
        # оправдано. На ранних оно только жжёт лимиты подписки.
        effort = self.effort
        if effort and attempt >= 7 and effort in ("low", "medium"):
            effort = "high"
        if effort:
            flags += ["--effort", effort]
        if self.fallback:
            flags += ["--fallback-model", self.fallback]
        return flags + self.extra_args

    @property
    def available(self) -> bool:
        return bool(self.exe)

    def _run(self, args: list[str], text: str | None) -> subprocess.CompletedProcess:
        return subprocess.run(
            args, input=text, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=self.timeout,
        )

    def soften(self, prompt: str, attempt: int, category: str = "policy") -> tuple[str, str]:
        if not self.exe:
            raise SoftenError(
                "claude CLI не найден. Десктопное приложение не подходит — нужен "
                "Claude Code CLI; путь можно задать в moderation.soften.claude_cli_path"
            )
        text = f"{_instruction(attempt, category)}\n\nПромпт:\n{prompt}"
        flags = self._flags(attempt)
        base = [self.exe, "-p", *flags]
        # Две формы вызова: сначала текст через stdin (не упирается в предел
        # длины командной строки Windows — наши промпты бывают по 6000+
        # символов вместе с инструкцией), при пустом ответе — аргументом.
        try:
            res = self._run(base, text)
            out = (res.stdout or "").strip()
            if not out:
                res = self._run([self.exe, "-p", text, *flags], None)
                out = (res.stdout or "").strip()
        except subprocess.TimeoutExpired as exc:
            raise SoftenError(f"claude CLI не ответил за {self.timeout}с") from exc
        except OSError as exc:
            raise SoftenError(f"claude CLI не запустился: {type(exc).__name__}") from exc

        if not out:
            err = scrub((res.stderr or "").strip())[:200] or f"код возврата {res.returncode}"
            raise SoftenError(f"claude CLI вернул пустой ответ ({err})")

        # CLI печатает свои ошибки в stdout обычным текстом и с кодом 0 —
        # «Not logged in · Please run /login» прошло бы дальше как готовый
        # промпт. Ловим такие ответы по маркерам и по явной краткости.
        low = out.lower()
        for marker in ("not logged in", "please run /login", "invalid api key",
                       "credit balance is too low", "usage limit reached"):
            if marker in low:
                raise SoftenError(f"claude CLI: {out.splitlines()[0][:160]}")
        if len(out) < min(200, len(prompt) // 4):
            raise SoftenError(
                f"claude CLI вернул подозрительно короткий ответ "
                f"({len(out)} симв. против {len(prompt)}): {out[:120]}"
            )
        return _strip_fence(out), f"переписано Claude CLI ({self.model})"


class Softener:
    """Композиция: выбранный LLM-бэкенд с фоллбэком на правила.

    spares — очередь запасных бэкендов на ВТОРОЙ заход по лестнице. Если
    девять ступеней одной моделью модерацию не пробили, дело может быть не
    в формулировках, а в самой модели: локальная gemma и облачный Gemini
    переписывают по-разному. next_backend() переключает на следующий.
    """

    def __init__(self, primary, rules: RuleSoftener,
                 log: Callable[[str], None] | None = None, spares: Any = ()) -> None:
        self.primary = primary  # None | OllamaSoftener | GeminiSoftener | ClaudeSoftener
        self.rules = rules
        self.spares = [s for s in spares]
        self._log = log or (lambda s: None)

    @property
    def name(self) -> str:
        return self.primary.name if self.primary is not None else self.rules.name

    def next_backend(self) -> str | None:
        """Переключиться на следующий запасной бэкенд. None — их больше нет.

        Доступность проверяется здесь, а не при сборке: Ollama могли поднять
        или уронить уже посреди прогона.
        """
        while self.spares:
            cand = self.spares.pop(0)
            try:
                ok = bool(cand.available)
            except Exception:  # noqa: BLE001 — недоступность не должна ронять задачу
                ok = False
            if ok:
                self.primary = cand
                return cand.name
        # Правила — последний рубеж: они всегда работают и хоть что-то меняют.
        if self.primary is not None:
            self.primary = None
            return self.rules.name
        return None

    def soften(self, prompt: str, attempt: int, category: str = "policy") -> tuple[str, str]:
        # Попытка №3 — особая: вместо переписывания отправляем ГОЛЫЕ реплики
        # из промпта, без описаний сцены. Модерация ругается на описания,
        # а диалог сам по себе Flow нередко пропускает.
        if attempt == DIALOGUE_ONLY_ATTEMPT:
            dialogue = extract_dialogue(prompt)
            if dialogue:
                return dialogue, "оставлены только реплики — без описаний сцены"
            self._log("в промпте нет реплик «...» — попытка №3 идёт обычным смягчением")

        if self.primary is not None:
            # Реплики уезжают в LLM плейсхолдерами и возвращаются на место
            # после — их дословность гарантирована механикой, а не инструкцией.
            masked, reps = _mask_dialogue(prompt)
            try:
                # Gemini умеет рассказать, что перешёл на другую модель.
                if isinstance(self.primary, GeminiSoftener):
                    out, what = self.primary.soften(masked, attempt, category, log=self._log)
                else:
                    out, what = self.primary.soften(masked, attempt, category)
            except SoftenError as exc:
                self._log(f"смягчитель {self.primary.name} не сработал ({exc}) — падаю на правила")
            else:
                # Ответ должен быть переписанным промптом, а не пересказом
                # инструкции и не отказом — см. MIN/MAX_REWRITE_RATIO.
                ratio = len(out.strip()) / max(1, len(masked.strip()))
                if not MIN_REWRITE_RATIO <= ratio <= MAX_REWRITE_RATIO:
                    self._log(
                        f"{self.primary.name} вернул текст в {ratio:.1f}x от исходного — "
                        "это не переписанный промпт, отбрасываю и падаю на правила"
                    )
                else:
                    out, lost = _unmask_dialogue(out, reps)
                    if lost:
                        self._log(
                            f"модель выкинула {len(lost)} реплик(и) вместе с плейсхолдерами — "
                            "дописаны блоком DIALOGUE в конец"
                        )
                    return out, what
        return self.rules.soften(prompt, attempt, category)


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
    claude_cli = ClaudeCliSoftener(cfg)
    claude = ClaudeSoftener(cfg)
    # Порядок — по цене. Ollama бесплатна и локальна; у Gemini бесплатный
    # тариф; Claude CLI идёт по подписке (предельная цена нулевая, но ест
    # общие лимиты); Claude по API — единственный за деньги.
    chain = [ollama, gemini, claude_cli, claude]

    def compose(primary: Any) -> Softener:
        """Собрать смягчитель, отдав остальные бэкенды в запас на второй заход."""
        spares = [c for c in chain if c is not primary]
        return Softener(primary, rules, log, spares=spares)

    if backend == "ollama":
        return compose(ollama if ollama.available else None)
    if backend == "gemini":
        return compose(gemini if gemini.available else None)
    if backend == "claude-cli":
        return compose(claude_cli if claude_cli.available else None)
    if backend == "claude":
        return compose(claude if claude.available else None)

    # auto: первый доступный из цепочки; правила всегда в запасе.
    for candidate in chain:
        if candidate.available:
            return compose(candidate)
    return Softener(None, rules, log)


def find_ollama_exe() -> str | None:
    """Где лежит ollama.exe: PATH, затем стандартная установка на Windows."""
    exe = shutil.which("ollama")
    if exe:
        return exe
    if os.name == "nt":
        cand = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
        if cand.exists():
            return str(cand)
    return None


def ensure_ollama(cfg: Config, wait_sec: int = 30, on_step: Any = None) -> dict[str, Any]:
    """Поднять сервер Ollama, если он не запущен. Модель НЕ качает.

    «Выбрал Ollama перед генерацией — дальше само»: панель зовёт это при
    выборе бэкенда и на старте прогона. Сервер поднимается как отдельный
    процесс и живёт после выхода панели. Скачивание модели сюда сознательно
    не входит: 7+ ГБ молча в фоне — это сюрприз, а не сервис.
    """
    say = on_step or (lambda _s: None)
    o = OllamaSoftener(cfg)

    def _status(running: bool, started: bool, models: list[str] | None) -> dict[str, Any]:
        present = bool(models) and o.model in (models or [])
        if not running:
            note = f"Ollama не отвечает на {o.host}"
        elif present:
            note = f"Ollama работает, модель {o.model} на месте"
        else:
            note = (f"Ollama работает, но модели {o.model} нет — скачай: "
                    f"ollama pull {o.model}")
        return {"running": running, "started": started, "model": o.model,
                "model_present": present, "note": note}

    try:
        return _status(True, False, o.list_models())
    except SoftenError:
        pass  # сервер не отвечает — пробуем поднять

    exe = find_ollama_exe()
    if not exe:
        return {"running": False, "started": False, "model": o.model, "model_present": False,
                "note": "Ollama не установлена (ollama.exe не найден) — поставь её или выбери Gemini"}

    say(f"Ollama не запущена — поднимаю сервер ({Path(exe).name} serve)")
    env = dict(os.environ)
    host = o.host.removeprefix("http://").removeprefix("https://")
    if host not in ("localhost:11434", "127.0.0.1:11434"):
        env["OLLAMA_HOST"] = host  # нестандартный порт — сервер должен слушать там же
    flags = 0
    if os.name == "nt":
        # Сервер должен пережить закрытие панели.
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen([exe, "serve"], creationflags=flags, close_fds=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    except Exception as exc:  # noqa: BLE001
        return {"running": False, "started": False, "model": o.model, "model_present": False,
                "note": f"не смог запустить ollama serve: {type(exc).__name__}"}

    deadline = time.time() + wait_sec
    while time.time() < deadline:
        try:
            models = o.list_models()
            say("сервер Ollama ответил")
            return _status(True, True, models)
        except SoftenError:
            time.sleep(1.0)
    return {"running": False, "started": True, "model": o.model, "model_present": False,
            "note": f"ollama serve не ответил за {wait_sec}с"}


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
    cli = ClaudeCliSoftener(cfg)
    out.append({
        "id": "claude-cli", "title": f"Claude CLI ({cli.model})", "available": cli.available,
        "detail": (f"по подписке, без ключа: {cli.exe}" if cli.available else
                   "claude CLI не найден — десктопное приложение не подходит, "
                   "нужен Claude Code CLI"),
    })
    cl = ClaudeSoftener(cfg)
    out.append({"id": "claude", "title": f"Claude API ({cl.model})", "available": cl.available,
                "detail": "ключ в .env (платно)" if cl.available
                else "нет ANTHROPIC_API_KEY или пакета anthropic"})
    return out
