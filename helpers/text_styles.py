"""Layered text styles: a client's signature text looks, built from primitives.

A style is a recipe — gradient fill, outline, extrude, glow, a plate (pill)
with an offset back plate, light streaks — composed in Python over the alpha
of the shaped text (CoreText or Pillow+raqm), so it renders the same with
either engine. Recipes live in the project's ``brand_kit.json``::

    "text_styles": {
      "bubble": {"fill": ["#3D55E8", "#7088FF"], "outline": "#FFFFFF",
                 "extrude": {"color": "#2236B8", "depth": 0.05},
                 "plate": {"colors": ["#FFFFFF", "#E3ECFF"]},
                 "back_plate": {"colors": ["#5B5FE6", "#8ED8F8"], "offset": [-0.03, 0.12], "tilt": -2}},
      "streak": {"fill": ["#A9D2FF", "#3A5BFF"], "glow": "#3F7BFFCC", "stretch": 3,
                 "streaks": {"color": "#7FB2FF", "opacity": 0.85}}
    }

and an EDL graphic picks one with ``"text_style": "bubble"``. The built-in
recipes below are brand-neutral starting points that read the kit's colours.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter

# Letters that never join to the next one: a kashida after them would float.
NON_JOINING = set("اأإآءدذرزوؤةىـ ")
ARABIC = range(0x0600, 0x0700)


def _rgba(value: str) -> tuple[int, int, int, int]:
    v = value.lstrip("#")
    if len(v) == 6:
        v += "FF"
    return tuple(int(v[k:k + 2], 16) for k in (0, 2, 4, 6))  # type: ignore[return-value]


def stretch_arabic(text: str, amount: int) -> str:
    """Kashida (ـ) elongation: ``amount`` tatweels after the middle joining
    letter of each Arabic word. Latin words and non-joining letters untouched."""
    if amount <= 0:
        return text
    out = []
    for word in text.split(" "):
        joinable = [k for k, ch in enumerate(word[:-1])
                    if ord(ch) in ARABIC and ch not in NON_JOINING and ord(word[k + 1]) in ARABIC
                    and word[k + 1] not in "ءـ" and ch.isalpha()]
        if len(word) >= 3 and joinable:
            k = joinable[len(joinable) // 2]
            word = word[:k + 1] + "ـ" * amount + word[k + 1:]
        out.append(word)
    return " ".join(out)


def builtin_styles(tokens) -> dict[str, dict]:
    accent = tokens.accent if tokens.branded else "#3FB7FF"
    primary = tokens.primary if tokens.branded else "#2B4BFF"
    return {
        "bubble": {"fill": [primary, accent], "outline": "#FFFFFF", "outline_ratio": 0.07,
                   "extrude": {"color": "#1B2A8F", "depth": 0.045},
                   "plate": {"colors": ["#FFFFFF", "#E4ECFF"]},
                   "back_plate": {"colors": [primary, accent], "offset": [-0.03, 0.14], "grow": [0.04, 0.1], "tilt": -2},
                   "animation": "pop"},
        "streak": {"fill": ["#B6DAFF", primary], "glow": f"{accent}CC", "stretch": 3, "size": 0.15,
                   "streaks": {"color": accent, "opacity": 0.8}, "animation": "wipe"},
        "outline": {"fill": ["#FFFFFF", "#FFFFFF"], "outline": primary, "outline_ratio": 0.06,
                    "extrude": {"color": primary, "depth": 0.035}, "size": 0.07, "animation": "rise"},
    }


def resolve_style(name: str, tokens) -> dict | None:
    styles = builtin_styles(tokens)
    styles.update(tokens.kit.get("text_styles") or {})
    return styles.get(name)


def _dilate(mask: Image.Image, radius: int) -> Image.Image:
    out = mask
    for _ in range(max(0, radius)):
        out = out.filter(ImageFilter.MaxFilter(3))
    return out


def _gradient(size: tuple[int, int], colors: list[str], vertical: bool = True) -> Image.Image:
    w, h = size
    c0, c1 = _rgba(colors[0]), _rgba(colors[-1])
    span = h if vertical else w
    strip = Image.new("RGBA", (1, span) if vertical else (span, 1))
    for k in range(span):
        t = k / max(1, span - 1)
        strip.putpixel((0, k) if vertical else (k, 0), tuple(round(a + (b - a) * t) for a, b in zip(c0, c1)))
    return strip.resize((w, h), Image.BILINEAR)


def style_line(mask: Image.Image, recipe: dict, size: float) -> Image.Image:
    """Glow → extrude → outline → gradient fill, over one line's text alpha."""
    outline_r = round(size * float(recipe.get("outline_ratio", 0.07))) if recipe.get("outline") else 0
    ex = recipe.get("extrude") or {}
    depth = round(size * float(ex.get("depth", 0))) if ex else 0
    glow = recipe.get("glow")
    glow_r = min(round(size * 0.25), 60) if glow else 0  # capped: a huge glyph needs no huge halo
    pad = outline_r + depth + glow_r * 2 + 4
    w, h = mask.width + 2 * pad, mask.height + 2 * pad
    base = Image.new("L", (w, h), 0)
    base.paste(mask, (pad, pad))
    shell = _dilate(base, outline_r) if outline_r else base
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    if glow:
        halo = Image.new("RGBA", (w, h), _rgba(glow)[:3] + (0,))
        halo.putalpha(shell.filter(ImageFilter.GaussianBlur(glow_r)).point(lambda v: round(v * _rgba(glow)[3] / 255)))
        canvas.alpha_composite(halo)
    if depth:
        dx, dy = float(ex.get("dx", 0.0)), float(ex.get("dy", 1.0))
        colour = Image.new("RGBA", (w, h), _rgba(ex.get("color", "#1B2A8F")))
        for k in range(depth, 0, -1):
            layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            layer.paste(colour, (round(dx * k), round(dy * k)), ImageChops.offset(shell, 0, 0))
            canvas.alpha_composite(layer)
    if outline_r:
        ring = Image.new("RGBA", (w, h), _rgba(recipe["outline"]))
        ring.putalpha(shell)
        canvas.alpha_composite(ring)
    fill = _gradient((w, h), recipe.get("fill") or ["#FFFFFF", "#FFFFFF"])
    fill.putalpha(ImageChops.multiply(fill.getchannel("A"), base))
    canvas.alpha_composite(fill)
    return canvas.crop(canvas.getbbox() or (0, 0, w, h))


def add_plates(text: Image.Image, recipe: dict, unit: float) -> Image.Image:
    """Pill plate behind the text, and an offset, tilted back plate behind it."""
    plate = recipe.get("plate") or {}
    back = recipe.get("back_plate")
    px, py = round(text.height * 0.55), round(text.height * 0.22)
    pw, ph = text.width + 2 * px, text.height + 2 * py
    gx = round(pw * float((back or {}).get("grow", [0, 0])[0]))
    gy = round(ph * float((back or {}).get("grow", [0, 0])[1]))
    ox = round(pw * float((back or {}).get("offset", [0, 0])[0]))
    oy = round(ph * float((back or {}).get("offset", [0, 0])[1]))
    margin = max(abs(ox), abs(oy)) + max(gx, gy) + round(ph * 0.3)
    W, H = pw + 2 * margin, ph + 2 * margin
    out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    if back:
        bw, bh = pw + 2 * gx, ph + 2 * gy
        shape = Image.new("L", (bw, bh), 0)
        ImageDraw.Draw(shape).rounded_rectangle((0, 0, bw - 1, bh - 1), radius=bh // 2, fill=255)
        layer = _gradient((bw, bh), back.get("colors") or ["#5B5FE6", "#8ED8F8"], vertical=False)
        layer.putalpha(shape)
        if back.get("tilt"):
            layer = layer.rotate(float(back["tilt"]), resample=Image.BICUBIC, expand=True)
        out.alpha_composite(layer, (margin - gx + ox - (layer.width - bw) // 2, margin - gy + oy - (layer.height - bh) // 2))
    shape = Image.new("L", (pw, ph), 0)
    ImageDraw.Draw(shape).rounded_rectangle((0, 0, pw - 1, ph - 1), radius=ph // 2, fill=255)
    face = _gradient((pw, ph), plate.get("colors") or ["#FFFFFF", "#E4ECFF"])
    face.putalpha(shape)
    out.alpha_composite(face, (margin, margin))
    out.alpha_composite(text, (margin + px, margin + py))
    return out.crop(out.getbbox())


def streak_bars(line: Image.Image, recipe: dict, canvas_w: int) -> Image.Image:
    """Light bars from the text out to both frame edges, at the line's height."""
    cfg = recipe.get("streaks") or {}
    if line.width > canvas_w * 0.86:  # the text itself must stay inside the frame
        k = canvas_w * 0.86 / line.width
        line = line.resize((round(line.width * k), max(1, round(line.height * k))), Image.LANCZOS)
    colour = _rgba(cfg.get("color", "#7FB2FF"))
    gap = round(line.height * 0.18)
    bar_h = round(line.height * float(cfg.get("height", 0.42)))
    W = max(canvas_w, line.width + 4 * gap)
    out = Image.new("RGBA", (W, line.height), (0, 0, 0, 0))
    x0 = (W - line.width) // 2
    y = (line.height - bar_h) // 2 + round(line.height * 0.06)
    draw = ImageDraw.Draw(out)
    strong = round(255 * float(cfg.get("opacity", 0.8)))
    draw.rectangle((x0 + line.width + gap, y, W, y + bar_h), fill=colour[:3] + (strong,))
    draw.rectangle((0, y, max(0, x0 - gap), y + bar_h), fill=colour[:3] + (round(strong * 0.45),))
    soft = out.filter(ImageFilter.GaussianBlur(max(1, bar_h * 0.08)))
    soft.alpha_composite(line, (x0, 0))
    return soft


def render_styled(element: dict, recipe: dict, tokens, canvas: tuple[int, int], workdir: Path, rasterize) -> list[tuple[Image.Image, float]]:
    """Styled parts with stagger delays, ready for ``stack_layers``."""
    w, h = canvas
    unit = min(w, h)
    lines = str(element.get("text", "")).split("\n")
    scales = element.get("line_scales") or recipe.get("line_scales") or [1.0] * len(lines)
    stretch = int(element.get("stretch", recipe.get("stretch", 0)))
    base = unit * float(recipe.get("size", 0.085)) * float(element.get("scale", 1))
    font = recipe.get("font")
    weight = recipe.get("weight", "black")
    cards = []
    for k, line in enumerate(lines):
        text = stretch_arabic(line, stretch)
        size = base * float(scales[k] if k < len(scales) else 1)
        card = {"lines": [{"text": text, "font": font or tokens.font_for(text), "size": size, "color": "#FFFFFF",
                           "weight": weight}], "padX": 6, "padY": 6, "maxWidth": round(w * 3)}
        key = hashlib.sha1(json.dumps(card, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]
        card["file"] = str(workdir / f"styled_{key}.png")
        cards.append((card, size))
    rasterize([c for c, _ in cards], workdir)
    styled = []
    for card, size in cards:
        mask = Image.open(card["file"]).convert("RGBA").getchannel("A")
        mask = mask.crop(mask.getbbox() or (0, 0, mask.width, mask.height))
        styled.append((style_line(mask, recipe, size), size))
    if recipe.get("plate") or recipe.get("back_plate"):
        gap = round(base * 0.08)
        width = max(img.width for img, _ in styled)
        height = sum(img.height for img, _ in styled) + gap * (len(styled) - 1)
        block = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        y = 0
        for img, _ in styled:
            block.alpha_composite(img, ((width - img.width) // 2, y))
            y += img.height + gap
        return [(add_plates(block, recipe, unit), 0.0)]
    parts = []
    for k, (img, _size) in enumerate(styled):
        if recipe.get("streaks"):
            img = streak_bars(img, recipe, w)
        parts.append((img, 0.1 * k))
    return parts
