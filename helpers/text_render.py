"""Cross-platform card renderer (Pillow + libraqm) — the Linux twin of
``render_graphic_card.swift``.

Same card spec, same output: transparent PNGs for stickers, numbers,
headlines, lower thirds, and caption states. Arabic needs Pillow's RAQM
layout (``apt install libraqm0`` on Linux) for letter joining; bidi
ordering is done here per word, so a highlighted word never breaks the
reading order of a mixed Arabic/Latin line.
"""

from __future__ import annotations

import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, features

WEIGHTS = {"black": "black", "heavy": "extrabold", "bold": "bold", "semibold": "semibold", "medium": "medium",
           "regular": "regular"}


def raqm_available() -> bool:
    return bool(features.check("raqm"))


def is_arabic(text: str) -> bool:
    return any("֐" <= ch <= "ࣿ" or "יִ" <= ch <= "﻿" for ch in text)


@lru_cache(maxsize=64)
def font_file(spec: str | None, weight: str | None, arabic: bool) -> str:
    """Family name / path → a font file via fontconfig."""
    if spec and Path(spec).expanduser().is_file():
        return str(Path(spec).expanduser())
    if not shutil.which("fc-match"):
        raise RuntimeError("fontconfig (fc-match) is required to find fonts by name")
    family = spec or ("Noto Sans Arabic" if arabic else "sans-serif")
    pattern = f"{family}:weight={WEIGHTS.get(weight or 'bold', 'bold')}"
    if arabic:
        pattern += ":lang=ar"
    out = subprocess.run(["fc-match", "-f", "%{file}", pattern], capture_output=True, text=True).stdout.strip()
    if not out:
        raise RuntimeError(f"no font matches {pattern!r}")
    return out


@lru_cache(maxsize=128)
def load_font(spec: str | None, size: int, weight: str | None, arabic: bool) -> ImageFont.FreeTypeFont:
    engine = ImageFont.Layout.RAQM if raqm_available() else ImageFont.Layout.BASIC
    return ImageFont.truetype(font_file(spec, weight, arabic), size, layout_engine=engine)


def rgba(hex_color: str) -> tuple[int, int, int, int]:
    s = hex_color.lstrip("#")
    if len(s) == 6:
        s += "FF"
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16), int(s[6:8], 16)


# ---------------------------------------------------------------- layout

def tokens_with_colors(line: dict) -> list[tuple[str, str]]:
    """Split a line into words, each with its colour (spans are UTF-16 offsets)."""
    text, base = line["text"], line["color"]
    out, offset = [], 0
    for word in text.split(" "):
        length = len(word.encode("utf-16-le")) // 2
        colour = base
        for span in line.get("spans") or []:
            if span["start"] < offset + length and offset < span["start"] + span["length"]:
                colour = span["color"]
        if word:
            out.append((word, colour))
        offset += length + 1
    return out


def visual_order(words: list[tuple[str, str]], rtl: bool) -> list[tuple[str, str]]:
    """Word-level bidi: runs of one script keep their own direction; in an RTL
    paragraph the runs themselves go right-to-left."""
    runs: list[list[tuple[str, str]]] = []
    for word in words:
        arabic = is_arabic(word[0])
        if runs and is_arabic(runs[-1][0][0]) == arabic:
            runs[-1].append(word)
        else:
            runs.append([word])
    ordered: list[tuple[str, str]] = []
    for run in (reversed(runs) if rtl else runs):
        ordered += list(reversed(run)) if is_arabic(run[0][0]) else run
    return ordered  # left → right


def wrap(words: list[tuple[str, str]], measure, max_width: float) -> list[list[tuple[str, str]]]:
    lines, current = [], []
    for word in words:
        trial = current + [word]
        if current and measure(trial) > max_width:
            lines.append(current)
            current = [word]
        else:
            current = trial
    if current:
        lines.append(current)
    return lines


# ---------------------------------------------------------------- paint

def gradient(size: tuple[int, int], colors: list[str], angle: float = 90.0) -> Image.Image:
    w, h = size
    stops = [rgba(c) for c in colors] or [(255, 255, 255, 255)]
    if len(stops) == 1:
        return Image.new("RGBA", size, stops[0])
    horizontal = abs(angle % 180) < 45
    n = w if horizontal else h
    strip = Image.new("RGBA", (n, 1) if horizontal else (1, n))
    for i in range(n):
        p = i / max(1, n - 1)
        if not horizontal:
            p = 1 - p  # angle 90: bottom → top, like NSGradient
        seg = min(len(stops) - 2, int(p * (len(stops) - 1)))
        local = p * (len(stops) - 1) - seg
        a, b = stops[seg], stops[seg + 1]
        colour = tuple(round(a[k] + (b[k] - a[k]) * local) for k in range(4))
        strip.putpixel((i, 0) if horizontal else (0, i), colour)
    return strip.resize(size)


def rounded_mask(size: tuple[int, int], box: tuple[float, float, float, float], radius: float) -> Image.Image:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(box, radius=max(0, radius), fill=255)
    return mask


def coloured_shadow(alpha: Image.Image, colour: str, blur: float, dx: float, dy: float) -> Image.Image:
    r, g, b, a = rgba(colour)
    layer = Image.new("RGBA", alpha.size, (r, g, b, 0))
    layer.putalpha(alpha.point(lambda v: v * a // 255))
    if blur > 0:
        layer = layer.filter(ImageFilter.GaussianBlur(blur / 2))
    shifted = Image.new("RGBA", alpha.size, (0, 0, 0, 0))
    shifted.alpha_composite(layer, (round(dx), round(dy)))
    return shifted


def render_card(card: dict[str, Any]) -> None:
    pad_x, pad_y = card["padX"], card["padY"]
    gap = card.get("lineGap") or 0
    rtl = any(is_arabic(line["text"]) for line in card["lines"])
    laid: list[tuple[dict, ImageFont.FreeTypeFont, list[list[tuple[str, str]]]]] = []
    for line in card["lines"]:
        arabic = is_arabic(line["text"])
        font = load_font(line.get("font"), round(line["size"]), line.get("weight"), arabic)
        space = font.getlength(" ")
        measure = lambda ws, f=font, sp=space: sum(f.getlength(w) for w, _c in ws) + sp * max(0, len(ws) - 1)
        rows = wrap(tokens_with_colors(line), measure, card["maxWidth"] - 2 * pad_x)
        laid.append((line, font, rows))

    rows_px = []
    for line, font, rows in laid:
        ascent, descent = font.getmetrics()
        for row in rows:
            ordered = visual_order(row, rtl)
            width = sum(font.getlength(w) for w, _c in ordered) + font.getlength(" ") * max(0, len(ordered) - 1)
            rows_px.append((font, ordered, width, ascent + descent, ascent))
    text_w = max((r[2] for r in rows_px), default=0)
    text_h = sum(r[3] for r in rows_px) + gap * max(0, len(rows_px) - 1)
    box_w, box_h = text_w + 2 * pad_x, text_h + 2 * pad_y
    margin = 4 + (card.get("strokeWidth") or 0)
    for effect in (card.get("shadow"), card.get("glow")):
        if effect:
            margin = max(margin, effect["blur"] * 2 + max(abs(effect["dx"]), abs(effect["dy"])))
    extrude = card.get("extrude")
    if extrude:
        margin = max(margin, extrude["depth"] * max(abs(extrude["dx"]), abs(extrude["dy"])) + 4)
    size = (int(box_w + 2 * margin + 1), int(box_h + 2 * margin + 1))
    box = (margin, margin, margin + box_w, margin + box_h)
    radius = min(card.get("radius") or 0, box_h / 2)

    def text_layer(override: str | None = None, stroke: tuple[int, str] | None = None) -> Image.Image:
        layer = Image.new("RGBA", size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        y = margin + pad_y
        align = card.get("align") or "center"
        for font, ordered, width, height, _asc in rows_px:
            if align == "left":
                x = margin + pad_x
            elif align == "right":
                x = margin + pad_x + text_w - width
            else:
                x = margin + pad_x + (text_w - width) / 2
            for word, colour in ordered:
                kwargs = {"direction": "rtl"} if is_arabic(word) and raqm_available() else {}
                fill = rgba(override or colour)
                if stroke:
                    kwargs.update(stroke_width=stroke[0], stroke_fill=rgba(stroke[1]))
                draw.text((x, y), word, font=font, fill=fill, **kwargs)
                x += font.getlength(word) + font.getlength(" ")
            y += height + gap
        return layer

    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    fill = card.get("fill")
    if fill:
        shape = rounded_mask(size, box, radius)
        if card.get("shadow"):
            s = card["shadow"]
            canvas.alpha_composite(coloured_shadow(shape, s["color"], s["blur"], s["dx"], s["dy"]))
        paint = gradient(size, fill["colors"], fill.get("angle", 90))
        canvas.alpha_composite(Image.composite(paint, Image.new("RGBA", size, (0, 0, 0, 0)), shape))
    stroke = card.get("stroke")
    width = card.get("strokeWidth") or 0
    if stroke and width > 0:
        outer = rounded_mask(size, box, radius)
        inner = rounded_mask(size, (box[0] + width, box[1] + width, box[2] - width, box[3] - width), max(0, radius - width))
        ring = ImageChops.subtract(outer, inner)
        canvas.alpha_composite(Image.composite(gradient(size, stroke["colors"], stroke.get("angle", 0)),
                                               Image.new("RGBA", size, (0, 0, 0, 0)), ring))
    if extrude and extrude.get("depth", 0) > 0:
        side = text_layer(override=extrude["color"])
        for step in range(int(extrude["depth"]), 0, -1):
            canvas.alpha_composite(side, (round(extrude["dx"] * step), round(extrude["dy"] * step)))
    glyphs = text_layer(stroke=(max(1, round(abs(card["textStroke"]["blur"]))), card["textStroke"]["color"])
                        if card.get("textStroke") else None)
    halo = card.get("glow") or (card.get("shadow") if not fill else None)
    if halo:
        canvas.alpha_composite(coloured_shadow(glyphs.getchannel("A"), halo["color"], halo["blur"], halo["dx"], halo["dy"]))
    canvas.alpha_composite(glyphs)
    canvas.save(card["file"])


def render_cards(cards: list[dict[str, Any]]) -> None:
    for card in cards:
        if any(is_arabic(line["text"]) for line in card["lines"]) and not raqm_available():
            raise RuntimeError("Arabic text needs Pillow with libraqm (Linux: apt install libraqm0; "
                               "macOS: brew install libraqm) so letters join correctly.")
        render_card(card)
