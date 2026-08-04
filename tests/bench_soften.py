"""Сравнение моделей смягчения на РЕАЛЬНОМ продакшн-промпте.

Запуск:  python tests/bench_soften.py [модель ...]

Критерий не «красиво переписала», а «не сломала то, что ломать нельзя»:
реплики кириллицей, метки персонажей, бренд, тайминги, секции, запреты.
Плюс проверяем, что жестокое всё-таки убрано, и меряем время.
"""

import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from flowbatch.config import Config
from flowbatch.promptfile import parse
from flowbatch.soften import GeminiSoftener, OllamaSoftener, SoftenError

# Что ОБЯЗАНО уцелеть дословно. Ломается — модель не годится, как бы
# хорошо ни звучал остальной текст.
MUST_KEEP = {
    "реплика 1": "Тогда по закону.",
    "реплика 2": "Ревилайн Эр Эль двести тридцать.",
    "реплика 3": "Это чего?",
    "реплика 4": "Выселение. Десять уровней давления.",
    "бренд": "Revyline",
    "модель продукта": "RL 230",
    "метка THE BONE": "THE BONE",
    "метка VALERA": "VALERA",
    "мир: enamel": "enamel",
    "секция CAMERA": "CAMERA",
    "секция DIALOGUE": "DIALOGUE",
    "запрет музыки": "NO MUSIC",
}

# Промпт, который Flow уже заворачивал, — на нём проверяем, что смягчение
# реально произошло, а не «вернул как было».
HARSH = (
    "Vertical 9:16 frame. Photoreal gore: a mutilated corpse in a pool of fresh blood, "
    "deep stab wounds, severed limbs, a bloody butcher knife in the chest. "
    "Extreme graphic violence, torture aftermath."
)
# Ищем по границам слов, а не подстрокой: наивный поиск считал бы «several»
# за «sever», а «stable» — за «stab», и модель штрафовалась бы ни за что.
# Основы с * допускают окончания (mutilat* -> mutilated/mutilation).
HARSH_WORDS = [
    "gore", "gory", "corpse", "blood", "bloody", "stab", "stabbed", "stabbing",
    "sever", "severed", "butcher", "torture", "mutilate", "mutilated", "mutilation",
    "entrails", "wound", "wounds",
]


def _found_words(text: str, words: list[str]) -> list[str]:
    """Какие из слов реально встречаются как отдельные слова."""
    low = text.lower()
    return [w for w in words if re.search(rf"\b{re.escape(w)}\b", low)]


def _context(text: str, word: str, span: int = 55) -> str:
    """Кусок текста вокруг слова — чтобы глазами проверить, не ложное ли срабатывание."""
    m = re.search(rf"\b{re.escape(word)}\b", text, re.IGNORECASE)
    if not m:
        return ""
    a, b = max(0, m.start() - span), min(len(text), m.end() + span)
    return "…" + " ".join(text[a:b].split()) + "…"


def load_real_prompt() -> str:
    src = Path(__file__).resolve().parents[1] / "prompts" / "ZASTRYALO_S01E02.flow.txt"
    jobs, errors, _ = parse(src.read_text(encoding="utf-8-sig"), "out", "products")
    if errors:
        raise SystemExit(f"файл очереди не разобрался: {errors[:2]}")
    return next(j.prompt for j in jobs if j.id == "S01E02_VID_04")


def score(backend, prompt: str, harsh: str) -> dict:
    res: dict = {"name": getattr(backend, "model", backend.name)}

    t0 = time.time()
    try:
        out, _ = backend.soften(prompt, 1)
    except SoftenError as exc:
        res["error"] = str(exc)[:70]
        return res
    res["sec_long"] = round(time.time() - t0, 1)
    res["kept"] = sum(1 for v in MUST_KEEP.values() if v in out)
    res["lost"] = [k for k, v in MUST_KEEP.items() if v not in out]
    res["len_ratio"] = round(len(out) / len(prompt), 2)
    res["changed"] = out.strip() != prompt.strip()

    t0 = time.time()
    try:
        harsh_out, _ = backend.soften(harsh, 1)
        res["sec_short"] = round(time.time() - t0, 1)
        res["harsh_left"] = _found_words(harsh_out, HARSH_WORDS)
        res["harsh_ctx"] = {w: _context(harsh_out, w) for w in res["harsh_left"]}
        res["softened"] = harsh_out.strip() != harsh.strip() and not res["harsh_left"]
    except SoftenError as exc:
        res["harsh_error"] = str(exc)[:50]
    return res


def main() -> int:
    load_dotenv()
    cfg = Config.load(Path(__file__).resolve().parents[1] / "config.yaml")
    prompt = load_real_prompt()
    print(f"Промпт: S01E02_VID_04, {len(prompt)} символов, "
          f"{len(MUST_KEEP)} критичных элементов\n")

    names = sys.argv[1:]
    if not names:
        try:
            names = [m for m in OllamaSoftener(cfg).list_models()]
        except SoftenError as exc:
            print(f"Ollama недоступна: {exc}")
            names = []

    rows = []
    for name in names:
        b = OllamaSoftener(cfg)
        b.model = name
        print(f"  … {name}", flush=True)
        rows.append(score(b, prompt, HARSH))

    gem = GeminiSoftener(cfg)
    if gem.available:
        print(f"  … Gemini {gem.model} (для сравнения)", flush=True)
        r = score(gem, prompt, HARSH)
        r["name"] = f"Gemini {gem.model}"
        rows.append(r)

    print(f"\n{'модель':<26} {'цело':>6} {'смягч':>6} {'длин':>6} {'сек':>6} {'сек':>6}")
    print(f"{'':<26} {'из ' + str(len(MUST_KEEP)):>6} {'':>6} {'ратио':>6} {'длин':>6} {'корот':>6}")
    print("-" * 62)
    for r in rows:
        if "error" in r:
            print(f"{r['name']:<26} ОШИБКА: {r['error']}")
            continue
        soft = "да" if r.get("softened") else "НЕТ"
        print(f"{r['name']:<26} {r['kept']:>6} {soft:>6} "
              f"{r['len_ratio']:>6} {r.get('sec_long','?'):>6} {r.get('sec_short','?'):>6}")
        if r["lost"]:
            print(f"{'':<26} потеряно: {', '.join(r['lost'])}")
        for w in r.get("harsh_left", []):
            print(f"{'':<26} осталось «{w}»: {r.get('harsh_ctx', {}).get(w, '')}")

    ok = [r for r in rows if "error" not in r and r["kept"] == len(MUST_KEEP) and r.get("softened")]
    print()
    if ok:
        best = min(ok, key=lambda r: r.get("sec_long", 1e9))
        print(f"ПОБЕДИТЕЛЬ: {best['name']} — всё цело, смягчает, "
              f"{best.get('sec_long')}с на длинном промпте")
    else:
        print("Ни одна модель не прошла оба критерия одновременно.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
