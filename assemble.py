#!/usr/bin/env python3
"""assemble.py — автоматический монтаж вертикальных ИИ-роликов.

Делает то, что в конвейере REVYLINE иначе делается руками в CapCut:
  • склейка нескольких 9:16 клипов по порядку;
  • ХУК-текст в первые секунды ("КОГО БЫ ТЫ ВЫБРАЛ?");
  • ПЛАШКА в конце ("Продолжение следует…" + призыв/промокод);
  • опционально фоновая музыка, приглушённая под голос.

Текст рисуется как PNG (Pillow) и накладывается через ffmpeg overlay —
не зависит от сборки ffmpeg с drawtext и даёт полный контроль над дизайном.
Озвучка внутри клипов (вариант RPA/Omni Flash) сохраняется как есть.

Пример:
  python assemble.py --clips out/v3_cliff_anim.mp4 out/v3_byte_anim.mp4 \
      out/v3_ad_anim.mp4 out/v3_final_anim.mp4 --out out/episode_final.mp4 \
      --hook "КОГО БЫ ТЫ ВЫБРАЛ?" \
      --endcard-title "Продолжение следует…" \
      --endcard-cta "Пиши «сикс севен», если ждёшь следующую!" \
      --promo "Промокод KIRKA — −20% на revyline.ru"
"""
from __future__ import annotations

import argparse
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 720, 1280
FONT = "/System/Library/Fonts/Supplemental/Arial.ttf"
FONT_BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = FONT_BOLD if bold and Path(FONT_BOLD).exists() else FONT
    return ImageFont.truetype(path, size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textlength(trial, font=font) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _draw_center(draw, lines, font, y, fill, stroke_fill="black", stroke_w=6, gap=14):
    for ln in lines:
        tw = draw.textlength(ln, font=font)
        x = (W - tw) / 2
        draw.text((x, y), ln, font=font, fill=fill, stroke_width=stroke_w, stroke_fill=stroke_fill)
        y += font.size + gap
    return y


def make_hook_png(text: str, path: Path) -> None:
    """Хук вверху кадра: жирный белый текст с обводкой в TikTok-стиле."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    font = _font(64, bold=True)
    lines = _wrap(d, text.upper(), font, int(W * 0.86))
    _draw_center(d, lines, font, int(H * 0.10), fill=(255, 255, 255, 255),
                 stroke_fill=(0, 0, 0, 230), stroke_w=8)
    img.save(path)


def make_endcard_png(title: str, cta: str, promo: str | None, path: Path) -> None:
    """Финальная плашка: затемнение снизу + заголовок + призыв + промо."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # мягкое затемнение нижней половины
    panel_top = int(H * 0.42)
    for i in range(panel_top, H):
        a = int(210 * (i - panel_top) / (H - panel_top))
        d.line([(0, i), (W, i)], fill=(8, 10, 20, a))
    y = int(H * 0.50)
    tfont = _font(58, bold=True)
    y = _draw_center(d, _wrap(d, title, tfont, int(W * 0.9)), tfont, y,
                     fill=(255, 224, 130, 255), stroke_fill=(60, 30, 0, 230), stroke_w=5, gap=10)
    y += 24
    cfont = _font(40, bold=True)
    y = _draw_center(d, _wrap(d, cta, cfont, int(W * 0.86)), cfont, y,
                     fill=(255, 255, 255, 255), stroke_fill=(0, 0, 0, 220), stroke_w=5, gap=10)
    if promo:
        y += 30
        pfont = _font(38, bold=True)
        # плашка-пилюля под промо
        lines = _wrap(d, promo, pfont, int(W * 0.8))
        for ln in lines:
            tw = pfont.getbbox(ln)[2]
            pad = 26
            x0 = (W - tw) / 2 - pad
            d.rounded_rectangle([x0, y - 8, W - x0, y + pfont.size + 12], radius=18,
                                fill=(230, 60, 90, 235))
            d.text(((W - tw) / 2, y), ln, font=pfont, fill=(255, 255, 255, 255))
            y += pfont.size + 22
    img.save(path)


def probe_duration(path: Path) -> float:
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nk=1:nw=1", str(path)])
    return float(out.strip())


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, capture_output=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clips", nargs="+", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--hook", default=None)
    ap.add_argument("--hook-secs", type=float, default=2.0)
    ap.add_argument("--endcard-title", default="Продолжение следует…")
    ap.add_argument("--endcard-cta", default="")
    ap.add_argument("--promo", default=None)
    ap.add_argument("--endcard-secs", type=float, default=4.0)
    ap.add_argument("--music", default=None)
    ap.add_argument("--music-vol", type=float, default=0.12)
    args = ap.parse_args()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        # 1) склейка (клипы однородны -> concat demuxer без перекодирования)
        listfile = tdp / "list.txt"
        listfile.write_text("".join(f"file '{Path(c).resolve()}'\n" for c in args.clips))
        base = tdp / "base.mp4"
        run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listfile),
             "-c", "copy", str(base)])
        total = probe_duration(base)

        # 2) оверлеи
        inputs = ["-i", str(base)]
        filters, last = [], "0:v"
        idx = 1
        if args.hook:
            hp = tdp / "hook.png"
            make_hook_png(args.hook, hp)
            inputs += ["-i", str(hp)]
            filters.append(
                f"[{last}][{idx}:v]overlay=0:0:enable='between(t,0,{args.hook_secs})'[v{idx}]")
            last = f"v{idx}"
            idx += 1
        ep = tdp / "endcard.png"
        make_endcard_png(args.endcard_title, args.endcard_cta, args.promo, ep)
        inputs += ["-i", str(ep)]
        start = max(0.0, total - args.endcard_secs)
        filters.append(
            f"[{last}][{idx}:v]overlay=0:0:enable='between(t,{start},{total})'[v{idx}]")
        last = f"v{idx}"
        idx += 1

        # 3) аудио: голос из клипов (+ опц. музыка под него)
        audio_map = ["-map", "0:a?"]
        if args.music:
            inputs += ["-stream_loop", "-1", "-i", str(args.music)]
            filters.append(
                f"[0:a][{idx}:a]amix=inputs=2:duration=first:weights=1 {args.music_vol}[aout]")
            audio_map = ["-map", "[aout]"]

        cmd = ["ffmpeg", "-y", *inputs,
               "-filter_complex", ";".join(filters),
               "-map", f"[{last}]", *audio_map,
               "-c:v", "libx264", "-preset", "medium", "-crf", "19",
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
               "-t", f"{total}", str(out)]
        run(cmd)
    print(f"OK: {out}  ({total:.1f}s)")


if __name__ == "__main__":
    main()
