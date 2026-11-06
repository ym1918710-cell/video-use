"""Brand-driven motion graphics for Video Use.

An EDL may carry a ``graphics`` list of semantic elements::

    {"type": "sticker", "text": "ولفهم الـTools", "start_in_output": 3.2,
     "duration": 2.0, "position": "upper_third", "behind_subject": false}

The element says *what* and *when*; the project's ``brand_kit.json`` says how
it looks (colours, fonts, shapes, logo, motion energy). With an empty brand kit
every element renders brand-neutral, so the same EDL can be re-dressed for any
client by swapping the kit.

Text is rasterized by CoreText (``render_graphic_card.swift``) for correct
Arabic shaping; animation is composed here with Pillow and encoded as an alpha
``.mov`` that render.py overlays like any other overlay file.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import sys
import tempfile
from fractions import Fraction
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter

GRAPHIC_TYPES = {"sticker", "title_card", "number", "headline", "lower_third", "logo", "end_card"}
ANIMATIONS = {"pop", "rise", "slide", "fade", "drop", "stamp", "zoom", "wipe", "blur", "typewriter", "none"}
STYLES = {"stacked", "two_tone", "boxed", "kicker", "underline"}
NAMED_POSITIONS = {
    "top": (0.5, 0.12), "upper_third": (0.5, 0.28), "center": (0.5, 0.5),
    "lower_third": (0.5, 0.70), "bottom": (0.5, 0.80),
    "top_left": (0.2, 0.1), "top_right": (0.8, 0.1), "top_center": (0.5, 0.07),
}
DEFAULT_ANIMATION = {
    "sticker": "pop", "title_card": "pop", "number": "pop", "headline": "rise",
    "lower_third": "slide", "logo": "fade", "end_card": "fade",
}
DEFAULT_POSITION = {
    "sticker": "upper_third", "title_card": "upper_third", "number": "center",
    "headline": "upper_third", "lower_third": "lower_third", "logo": "top_center",
}
SWIFT_SOURCE = Path(__file__).with_name("render_graphic_card.swift")
SWIFT_BINARY = Path(tempfile.gettempdir()) / "video-use-graphic-card"


# ---------------------------------------------------------------- brand kit

def load_brand_kit(edl: dict, edit_dir: Path) -> dict:
    """The EDL's ``brand_kit`` path wins; otherwise ``<edit>/brand_kit.json``."""
    raw = edl.get("brand_kit")
    if isinstance(raw, dict):
        return raw
    path = Path(raw) if raw else edit_dir / "brand_kit.json"
    if not path.is_absolute():
        path = edit_dir / path
    if path.is_file():
        try:
            kit = json.loads(path.read_text(encoding="utf-8"))
            kit["_dir"] = str(path.parent)
            return kit
        except json.JSONDecodeError as exc:
            raise ValueError(f"brand kit is not valid JSON: {path}: {exc}") from exc
    return {}


def _rgb(hex_color: str) -> tuple[int, int, int]:
    s = hex_color.lstrip("#")
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def shade(hex_color: str, factor: float) -> str:
    """factor < 1 darkens, > 1 lightens toward white."""
    r, g, b = _rgb(hex_color)
    if factor <= 1:
        r, g, b = (round(c * factor) for c in (r, g, b))
    else:
        t = min(1.0, factor - 1)
        r, g, b = (round(c + (255 - c) * t) for c in (r, g, b))
    return f"#{r:02X}{g:02X}{b:02X}"


def with_alpha(hex_color: str, alpha: float) -> str:
    return f"{hex_color[:7]}{round(max(0, min(1, alpha)) * 255):02X}"


def resolve_font_name(value: str) -> str:
    """A family/full name ("Cairo Black") → the file in the user's material
    folder when it is there; otherwise the name, for the system to resolve."""
    try:
        from material import find_font
        return find_font(value) or value
    except Exception:
        return value


class Tokens:
    """Brand tokens with explicit brand-neutral fallbacks."""

    def __init__(self, kit: dict, edit_dir: Path):
        self.kit = kit or {}
        self.base = Path(self.kit.get("_dir") or edit_dir)
        colors = self.kit.get("colors") or {}
        self.branded = bool(colors.get("primary"))
        self.primary = colors.get("primary") or "#FFFFFF"
        self.secondary = colors.get("secondary") or self.primary
        self.accent = colors.get("accent") or self.secondary
        self.text = colors.get("text") or "#FFFFFF"
        self.background = colors.get("background") or ("#0B0B12" if not self.branded else shade(self.primary, 0.25))
        gradients = self.kit.get("gradients") or []
        self.gradient = gradients[0] if gradients and isinstance(gradients[0], list) else (
            [self.primary, self.secondary] if self.branded else None)
        fonts = self.kit.get("fonts") or {}
        self.headline_font = self._font(fonts.get("headline"))
        self.body_font = self._font(fonts.get("body")) or self.headline_font
        self.arabic_font = self._font(fonts.get("arabic"))
        self.shapes = self.kit.get("shapes") or {}
        self.motion = self.kit.get("motion") or {}
        self.logo = self.kit.get("logo") or {}
        self.end_card = self.kit.get("end_card") or {}
        shadow = self.kit.get("text_shadow")
        self.shadow = None if shadow is False else {
            "color": "#000000", "opacity": 0.55, "blur_ratio": 0.014, "offset_ratio": 0.009, **(shadow or {})}

    def _font(self, value: str | None) -> str | None:
        if not value:
            return None
        candidate = Path(value).expanduser()
        if not candidate.is_absolute() and (self.base / candidate).exists():
            candidate = self.base / candidate
        if candidate.suffix.lower() in {".ttf", ".otf", ".ttc"}:
            return str(candidate)
        return resolve_font_name(value)

    def font_for(self, text: str, headline: bool = True) -> str | None:
        if self.arabic_font and any("֐" <= ch <= "ࣿ" for ch in text):
            return self.arabic_font
        return self.headline_font if headline else self.body_font

    def asset(self, value: str | None) -> Path | None:
        if not value:
            return None
        path = Path(value).expanduser()
        return path if path.is_absolute() else self.base / path

    @property
    def in_ms(self) -> float:
        energy = self.motion.get("energy")
        default = {"calm": 420, "medium": 300, "high": 220}.get(energy, 300)
        return float(self.motion.get("pop_in_ms") or default)


# ---------------------------------------------------------------- card specs

def highlight_spans(line: str, words: list[str], color: str) -> list[dict]:
    """Colour every occurrence of the highlighted words (UTF-16 offsets)."""
    spans = []
    for word in words or []:
        start = line.find(word)
        while word and start >= 0:
            spans.append({"start": len(line[:start].encode("utf-16-le")) // 2,
                          "length": len(word.encode("utf-16-le")) // 2, "color": color})
            start = line.find(word, start + len(word))
    return spans


def apply_style(spec: dict, element: dict, tokens: Tokens, canvas: tuple[int, int]) -> dict:
    """Compositions that change the card itself (``two_tone``, ``boxed``) and
    word highlights. ``kicker`` / ``underline`` are layered in build_element."""
    style = element.get("style", "stacked")
    accent = (tokens.shapes.get("headline") or {}).get("accent") or (
        tokens.accent if tokens.branded and tokens.accent.upper() != tokens.text.upper() else "#FFD23F")
    lines = spec.get("lines") or []
    for line in lines:
        spans = highlight_spans(line["text"], element.get("highlight") or [], accent)
        if spans:
            line["spans"] = spans
    if style == "two_tone" and len(lines) > 1:
        lines[-1]["color"] = accent
        lines[-1]["size"] *= 1.12
    if style == "boxed":
        u = min(canvas)
        size = lines[0]["size"] if lines else u * 0.08
        spec.update({"fill": {"colors": [tokens.primary if tokens.branded else "#000000CC"]},
                     "radius": u * 0.02, "padX": round(size * 0.45), "padY": round(size * 0.25)})
        for key in ("extrude", "glow"):
            spec.pop(key, None)
    return spec


def card_spec(element: dict, tokens: Tokens, canvas: tuple[int, int]) -> dict:
    """Translate a semantic element + brand tokens into a CoreText card."""
    w, h = canvas
    u = min(w, h)
    kind = element["type"]
    text = str(element.get("text", ""))
    max_width = round(w * 0.86)
    shadow = {"color": "#00000073", "blur": round(u * 0.016), "dx": 0, "dy": round(u * 0.007)}

    if kind in {"sticker", "title_card"}:
        shape = tokens.shapes.get("sticker") or {}
        size = u * (0.076 if kind == "title_card" else 0.07) * float(element.get("scale", 1))
        fill = shape.get("fill") or "#FFFFFF"
        text_color = shape.get("text") or (tokens.primary if tokens.branded else "#111111")
        stroke = shape.get("stroke") or tokens.gradient
        radius = shape.get("radius", "pill")
        return {
            "lines": [{"text": line, "font": tokens.font_for(line), "size": size, "color": text_color, "weight": "heavy"}
                      for line in text.split("\n")],
            "fill": {"colors": [fill]},
            "stroke": {"colors": stroke, "angle": 0} if stroke else None,
            "strokeWidth": round(u * float(shape.get("stroke_ratio", 0.0075))) if stroke else 0,
            "radius": 9999 if radius == "pill" else float(radius) * u,
            "padX": round(size * 0.7), "padY": round(size * 0.36), "lineGap": round(size * 0.1),
            "maxWidth": max_width, "shadow": shadow,
        }
    if kind == "number":
        shape = tokens.shapes.get("number") or {}
        size = u * 0.27 * float(element.get("scale", 1))
        color = shape.get("color") or tokens.text
        lines = [{"text": text, "font": tokens.font_for(text), "size": size, "color": color, "weight": "black"}]
        if element.get("label"):
            label = str(element["label"])
            lines.append({"text": label, "font": tokens.font_for(label), "size": size * 0.3, "color": color, "weight": "bold"})
        spec: dict[str, Any] = {"lines": lines, "padX": 8, "padY": 8, "lineGap": round(-size * 0.12), "maxWidth": max_width}
        if tokens.branded and shape.get("extrude", True):
            spec["extrude"] = {"depth": max(4, round(u * 0.009)), "color": shade(tokens.primary, 0.45), "dx": 1.4, "dy": 1.8}
            spec["glow"] = {"color": with_alpha(tokens.accent, 0.8), "blur": round(u * 0.028), "dx": 0, "dy": 0}
        else:
            spec["shadow"] = shadow
        return spec
    if kind == "headline":
        shape = tokens.shapes.get("headline") or {}
        size = u * 0.105 * float(element.get("scale", 1))
        color = shape.get("color") or tokens.text
        spec = {"lines": [{"text": line, "font": tokens.font_for(line), "size": size, "color": color, "weight": "black"}
                          for line in text.split("\n")],
                "padX": 8, "padY": 8, "lineGap": round(-size * 0.05), "maxWidth": max_width}
        if tokens.branded and shape.get("extrude", True):
            spec["extrude"] = {"depth": max(5, round(u * 0.011)), "color": shade(tokens.primary, 0.4), "dx": 1.2, "dy": 2.0}
            spec["glow"] = {"color": with_alpha(tokens.accent, 0.7), "blur": round(u * 0.03), "dx": 0, "dy": 0}
        else:
            spec["shadow"] = shadow
        return spec
    if kind == "lower_third":
        shape = tokens.shapes.get("lower_third") or {}
        size = u * 0.062
        title = str(element.get("title") or text)
        lines = [{"text": title, "font": tokens.font_for(title), "size": size, "color": shape.get("text") or tokens.text, "weight": "bold"}]
        if element.get("subtitle"):
            sub = str(element["subtitle"])
            lines.append({"text": sub, "font": tokens.font_for(sub, headline=False), "size": size * 0.74,
                          "color": with_alpha(shape.get("text") or tokens.text, 0.85), "weight": "medium"})
        fill = shape.get("fill") or (tokens.primary if tokens.branded else "#000000B3")
        rtl = any("֐" <= ch <= "ࣿ" for ch in title)
        return {"lines": lines, "fill": {"colors": [fill] if isinstance(fill, str) else fill, "angle": 0},
                "radius": u * float(shape.get("radius_ratio", 0.022)), "padX": round(size * 0.7), "padY": round(size * 0.45),
                "lineGap": round(size * 0.12), "maxWidth": round(w * 0.8), "shadow": shadow,
                "align": "right" if rtl else "left"}
    raise ValueError(f"no card spec for graphic type {kind!r}")


def _ensure_renderer() -> Path:
    if not SWIFT_BINARY.exists() or SWIFT_BINARY.stat().st_mtime < SWIFT_SOURCE.stat().st_mtime:
        subprocess.run(["swiftc", str(SWIFT_SOURCE), "-o", str(SWIFT_BINARY)], check=True, capture_output=True)
    return SWIFT_BINARY


def text_engine() -> str:
    """``coretext`` on macOS, else ``pil`` (Pillow + libraqm). Override with
    VIDEO_USE_TEXT_ENGINE=pil|coretext."""
    import os
    forced = os.environ.get("VIDEO_USE_TEXT_ENGINE")
    if forced in {"pil", "coretext"}:
        return forced
    return "coretext" if sys.platform == "darwin" and shutil.which("swiftc") else "pil"


def rasterize_cards(cards: list[dict], workdir: Path) -> None:
    """Render every card whose PNG is missing (CoreText, or Pillow on Linux)."""
    todo = [c for c in cards if not Path(c["file"]).exists()]
    if not todo:
        return
    if text_engine() == "pil":
        from text_render import render_cards
        render_cards([{k: v for k, v in card.items() if v is not None} for card in todo])
        return
    payload = workdir / "cards.json"
    clean = [{k: v for k, v in card.items() if v is not None} for card in todo]
    payload.write_text(json.dumps({"cards": clean}, ensure_ascii=False), encoding="utf-8")
    subprocess.run([str(_ensure_renderer()), str(payload)], check=True)


# ---------------------------------------------------------------- motion

def premiere_shadow(spec: dict) -> dict:
    """Premiere Drop Shadow settings (distance / softness in px at 1080 wide,
    opacity 0–1 or 0–255) → add_drop_shadow's ratios."""
    opacity = float(spec.get("opacity", 1.0))
    return {"color": spec.get("color", "#000000"), "opacity": opacity / 255 if opacity > 1 else opacity,
            "blur_ratio": float(spec.get("softness", 40)) * 0.4 / 1080, "offset_ratio": float(spec.get("distance", 5)) / 1080}


def add_drop_shadow(image: Image.Image, shadow: dict | None, unit: float, angle_deg: float = 90.0) -> Image.Image:
    """A soft drop shadow cast from the card's own alpha, so text and pills
    read on any picture. Grows the canvas to fit the blur and offset."""
    if not shadow:
        return image
    blur = max(1.0, unit * float(shadow.get("blur_ratio", 0.014)))
    dist = unit * float(shadow.get("offset_ratio", 0.009))
    dx = round(dist * math.cos(math.radians(angle_deg)) * 0.35)
    dy = round(dist * math.sin(math.radians(angle_deg)))
    pad = math.ceil(blur * 2.5) + max(abs(dx), abs(dy))
    out = Image.new("RGBA", (image.width + 2 * pad, image.height + 2 * pad), (0, 0, 0, 0))
    alpha = image.getchannel("A").point(lambda v: round(v * float(shadow.get("opacity", 0.55))))
    shade_layer = Image.new("RGBA", image.size, _rgb(shadow.get("color", "#000000")) + (0,))
    shade_layer.putalpha(alpha)
    blurred = Image.new("RGBA", out.size, (0, 0, 0, 0))
    blurred.alpha_composite(shade_layer, (pad + dx, pad + dy))
    out.alpha_composite(blurred.filter(ImageFilter.GaussianBlur(blur)))
    out.alpha_composite(image, (pad, pad))
    return out


def stack_layers(parts: list[tuple[Image.Image, float]], gap: int) -> tuple[Image.Image, list[tuple[Image.Image, tuple[int, int], float]]]:
    """Centre images vertically in one box: (composite, [(image, (x, y), delay)])."""
    width = max(img.width for img, _ in parts)
    height = sum(img.height for img, _ in parts) + gap * (len(parts) - 1)
    layers, y = [], 0
    for img, delay in parts:
        layers.append((img, ((width - img.width) // 2, y), delay))
        y += img.height + gap
    composite = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    for img, pos, _ in layers:
        composite.alpha_composite(img, pos)
    return composite, layers


def underline_bar(width: int, unit: float, color: str) -> Image.Image:
    bar_h = max(4, round(unit * 0.012))
    bar = Image.new("RGBA", (width, bar_h), (0, 0, 0, 0))
    ImageDraw.Draw(bar).rounded_rectangle((0, 0, width - 1, bar_h - 1), radius=bar_h // 2, fill=_rgb(color) + (255,))
    return bar


def reveal_at(anim: str, t: float, in_s: float, steps: int = 1) -> tuple[float, float]:
    """(revealed fraction 0–1, blur 0–1) for wipe / typewriter / blur entrances."""
    p = 1.0 if in_s <= 0 else max(0.0, min(1.0, t / in_s))
    if anim == "wipe":
        return ease_out_cubic(p), 0.0
    if anim == "typewriter":
        steps = max(1, steps)
        return min(1.0, math.ceil(p * steps) / steps) if p > 0 else 0.0, 0.0
    if anim == "blur":
        return 1.0, 1 - ease_out_cubic(p)
    return 1.0, 0.0


def ease_out_back(t: float, overshoot: float = 1.70158) -> float:
    t -= 1
    return 1 + (overshoot + 1) * t ** 3 + overshoot * t ** 2


def ease_out_cubic(t: float) -> float:
    return 1 - (1 - t) ** 3


def transform_at(anim: str, t: float, duration: float, in_s: float, out_s: float, unit: float,
                 direction: int = 1) -> tuple[float, float, float, float]:
    """(scale, dx, dy, alpha) for time ``t`` inside the element's window."""
    p_in = 1.0 if in_s <= 0 else max(0.0, min(1.0, t / in_s))
    p_out = 0.0 if out_s <= 0 else max(0.0, min(1.0, (t - (duration - out_s)) / out_s))
    scale, dx, dy, alpha = 1.0, 0.0, 0.0, 1.0
    if anim == "pop":
        scale = 0.55 + 0.45 * ease_out_back(p_in, 2.2)
        alpha = min(1.0, p_in / 0.35)
        scale *= 1 - 0.06 * p_out
    elif anim == "rise":
        dy = unit * 0.045 * (1 - ease_out_cubic(p_in))
        alpha = ease_out_cubic(p_in)
    elif anim == "slide":
        dx = direction * unit * 0.12 * (1 - ease_out_cubic(p_in))
        alpha = min(1.0, p_in / 0.5)
    elif anim == "fade":
        alpha = ease_out_cubic(p_in)
    elif anim == "drop":
        dy = -unit * 0.07 * (1 - ease_out_back(p_in, 1.6))
        alpha = min(1.0, p_in / 0.25)
    elif anim == "stamp":
        scale = 1.55 - 0.55 * ease_out_cubic(min(1.0, p_in * 1.25))
        alpha = min(1.0, p_in / 0.2)
    elif anim == "zoom":
        scale = 0.82 + 0.18 * ease_out_cubic(p_in)
        alpha = ease_out_cubic(p_in)
    elif anim in ("blur", "wipe", "typewriter"):
        alpha = 1.0 if anim != "blur" else ease_out_cubic(p_in)
    alpha *= 1 - p_out
    return scale, dx, dy, alpha


def _frame_count(duration: float, rate: str) -> int:
    return max(1, round(duration * float(Fraction(rate))))


def encode_alpha_mov(frames, size: tuple[int, int], rate: str, out: Path) -> None:
    """Pipe RGBA frames into a PNG-in-MOV file that keeps the alpha channel."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.mov")
    proc = subprocess.Popen([
        "ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgba",
        "-s", f"{size[0]}x{size[1]}", "-r", rate, "-i", "-",
        "-c:v", "png", "-pix_fmt", "rgba", str(tmp),
    ], stdin=subprocess.PIPE)
    assert proc.stdin
    try:
        for frame in frames:
            proc.stdin.write(frame)
    finally:
        proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError(f"ffmpeg failed encoding {out.name}")
    tmp.replace(out)


def animate_image(image: Image.Image, anim: str, duration: float, rate: str, tokens: Tokens,
                  unit: float, direction: int = 1, out_ms: float = 160,
                  layers: list[tuple[Image.Image, tuple[int, int], float]] | None = None, steps: int = 1):
    """Yield (box_size, frames) for a card animated inside a fixed box.

    ``layers`` animates the parts of a composition one after another (a
    kicker, then the headline, then its underline) instead of as one block.
    ``direction`` is -1 for left-to-right text, 1 for right-to-left: slides
    and wipes enter from the side reading starts on.
    """
    in_s = 0.0 if anim == "none" else min(duration * 0.45, tokens.in_ms / 1000 * (1.8 if anim == "typewriter" else 1))
    out_s = 0.0 if anim == "none" else min(duration * 0.25, out_ms / 1000)
    pad_x = round(unit * 0.12) + 4 if anim == "slide" else 4
    pad_y = round(unit * 0.08) + 4 if anim in ("rise", "drop") else 4
    grow = {"pop": 1.12, "stamp": 1.6}.get(anim, 1.0)
    if layers is None and grow > 1 and image.width * grow > unit * 1.0:
        grow = max(1.0, unit * 1.0 / image.width)  # wide cards stamp in from a smaller overshoot
    box = (math.ceil(image.width * grow) + 2 * pad_x, math.ceil(image.height * grow) + 2 * pad_y)
    box = (box[0] + box[0] % 2, box[1] + box[1] % 2)
    parts = layers or [(image, (0, 0), 0.0)]
    origin = ((box[0] - image.width) / 2, (box[1] - image.height) / 2)
    total = _frame_count(duration, rate)
    fps = float(Fraction(rate))
    cache: dict[tuple, bytes] = {}

    def shaped(img: Image.Image, t: float) -> tuple[Image.Image, float, float]:
        s, dx, dy, a = transform_at(anim, t, duration, in_s, out_s, unit, direction)
        s = min(s, (box[0] - 2) / max(1, img.width), (box[1] - 2) / max(1, img.height))  # never cropped by its box
        shown, blur = reveal_at(anim, t, in_s, steps)
        out = img
        if shown < 0.999:
            keep = max(0, round(img.width * shown))
            mask = Image.new("L", img.size, 0)
            # RTL reveals from the right edge, LTR from the left.
            box_x = (img.width - keep, 0, img.width, img.height) if direction > 0 else (0, 0, keep, img.height)
            mask.paste(255, box_x)
            out = img.copy()
            out.putalpha(Image.composite(img.getchannel("A"), mask, mask))
        if blur > 0.01:
            out = out.filter(ImageFilter.GaussianBlur(unit * 0.02 * blur))
        if abs(s - 1) > 1e-4:
            out = out.resize((max(1, round(out.width * s)), max(1, round(out.height * s))), Image.LANCZOS)
        if a < 0.999:
            out = out.copy()
            out.putalpha(out.getchannel("A").point(lambda v, a=a: round(v * a)))
        return out, dx, dy

    def frames():
        for index in range(total):
            t = index / fps
            key = tuple(round(max(0.0, t - d), 3) if t - d < in_s + 0.01 else -1 for _i, _p, d in parts) \
                + ((round(t, 3),) if t > duration - out_s else ())
            if key not in cache:
                canvas = Image.new("RGBA", box, (0, 0, 0, 0))
                for img, (lx, ly), delay in parts:
                    if t < delay:
                        continue
                    sized, dx, dy = shaped(img, t - delay)
                    cx = origin[0] + lx + img.width / 2
                    cy = origin[1] + ly + img.height / 2
                    canvas.alpha_composite(sized, (round(cx - sized.width / 2 + dx), round(cy - sized.height / 2 + dy)))
                if len(cache) > 400:
                    cache.clear()
                cache[key] = canvas.tobytes()
            yield cache[key]

    return box, frames()


# ---------------------------------------------------------------- timing + layout

def output_time(edl: dict, at: dict, rate: str) -> float:
    """Map a source moment ({"source", "time"}) onto the output timeline."""
    from render import segment_timing  # local import: render imports this module
    ranges = edl.get("ranges") or []
    for item, (_frames, offset, duration) in zip(ranges, segment_timing(ranges, rate)):
        sound = item.get("audio_source") if isinstance(item.get("audio_source"), dict) else None
        source = sound["source"] if sound else item.get("source")
        start = float(sound.get("start", item["start"])) if sound else float(item["start"])
        end = start + float(item["end"]) - float(item["start"])
        if source == at.get("source") and start <= float(at["time"]) <= end:
            return round(offset + min(duration, float(at["time"]) - start), 3)
    raise ValueError(f"graphic anchor {at} is not inside any kept range")


def place(position: Any, box: tuple[int, int], canvas: tuple[int, int], bleed: bool = False) -> tuple[int, int]:
    """Centre the box on a named or normalized point, clamped to the safe area
    (``bleed``: decorative layers — e.g. big text behind the subject — may run
    off the frame edge instead)."""
    w, h = canvas
    if isinstance(position, dict):
        cx, cy = float(position.get("x", 0.5)), float(position.get("y", 0.5))
    else:
        cx, cy = NAMED_POSITIONS.get(str(position), NAMED_POSITIONS["center"])
    margin_x = round(w * 0.04)
    top = round(h * 0.04)
    bottom = h - round(h * (0.14 if h > w else 0.05))  # vertical: clear platform UI
    x = round(cx * w - box[0] / 2)
    y = round(cy * h - box[1] / 2)
    if bleed:
        return x, y
    x = min(max(x, margin_x - 4), max(margin_x - 4, w - margin_x - box[0] + 4))
    y = min(max(y, top - 4), max(top - 4, bottom - box[1] + 4))
    return x, y


# ---------------------------------------------------------------- builders

def _code_version() -> str:
    """Changes whenever the renderer code changes, so cached graphics refresh."""
    h = hashlib.sha1()
    for source in (Path(__file__), SWIFT_SOURCE, Path(__file__).with_name("text_render.py"),
                   Path(__file__).with_name("text_styles.py")):
        h.update(source.read_bytes())
    return h.hexdigest()[:8]


def _digest(*parts: Any) -> str:
    blob = json.dumps([_code_version(), text_engine(), *parts], sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def _load_logo(tokens: Tokens, element: dict) -> Image.Image:
    path = tokens.asset(element.get("file") or tokens.logo.get("file"))
    if path is None or not path.is_file():
        raise ValueError("logo graphic needs brand_kit.logo.file (PNG with transparency) or element.file")
    image = Image.open(path).convert("RGBA")
    bbox = image.getchannel("A").getbbox()  # drop transparent padding
    return image.crop(bbox) if bbox else image


# Fraction of the entrance at which a graphic becomes readable (opacity ~60%,
# blur mostly gone, half a wipe revealed). Measured from transform_at/reveal_at.
READABLE_AT = {"pop": 0.35, "stamp": 0.2, "drop": 0.25, "rise": 0.3, "fade": 0.3, "zoom": 0.3, "blur": 0.45,
               "slide": 0.3, "wipe": 0.5, "typewriter": 0.1, "none": 0.0}


def readable_lead(anim: str, duration: float, tokens: "Tokens") -> float:
    """Seconds from a graphic's first frame until it can be read."""
    if anim == "none":
        return 0.0
    in_s = min(duration * 0.45, tokens.in_ms / 1000 * (1.8 if anim == "typewriter" else 1))
    return round(in_s * READABLE_AT.get(anim, 0.3), 3)


def graphic_start(element: dict, edl: dict, tokens: "Tokens", rate: str) -> float:
    """Output start of a graphic. One anchored to a word (``at``) starts early
    by its animation's readable lead, so it is *readable* on the word — not
    merely starting on it (``"anticipate": false`` to opt out)."""
    if "at" in element and "start_in_output" not in element:
        start = output_time(edl, element["at"], rate)
        if element.get("anticipate", True):
            kind = element.get("type", "sticker")
            anim = element.get("animation") or DEFAULT_ANIMATION.get(kind, "pop")
            if element.get("text_style"):
                try:
                    from text_styles import resolve_style
                    anim = element.get("animation") or (resolve_style(str(element["text_style"]), tokens) or {}).get("animation") or anim
                except Exception:
                    pass
            start = max(0.0, start - readable_lead(anim, float(element.get("duration", 1)), tokens))
        return start
    return float(element["start_in_output"])


def build_element(element: dict, edl: dict, tokens: Tokens, canvas: tuple[int, int], rate: str,
                  workdir: Path) -> dict:
    """Render one graphic element and return an overlay entry for render.py."""
    kind = element["type"]
    w, h = canvas
    unit = min(w, h)
    start = graphic_start(element, edl, tokens, rate)
    duration = float(element["duration"])
    anim = element.get("animation") or DEFAULT_ANIMATION[kind]
    key = _digest(element, {k: v for k, v in tokens.kit.items() if k != "_dir"}, canvas, rate)
    out = workdir / f"{kind}_{key}.mov"

    if kind == "end_card":
        if not out.exists():
            bg = Image.new("RGBA", canvas, _rgb(element.get("background") or tokens.background) + (255,))
            logo = None
            try:
                logo = _load_logo(tokens, {"file": element.get("logo") or tokens.end_card.get("file") or tokens.logo.get("file")})
            except ValueError:
                pass
            layers = []
            if logo is not None:
                target = round(w * float(element.get("logo_width", 0.42)))
                logo = logo.resize((target, max(1, round(logo.height * target / logo.width))), Image.LANCZOS)
                layers.append(logo)
            if element.get("text"):
                card = {"lines": [{"text": str(element["text"]), "font": tokens.font_for(str(element["text"])),
                                   "size": unit * 0.05, "color": tokens.text, "weight": "bold"}],
                        "padX": 8, "padY": 8, "maxWidth": round(w * 0.86), "file": str(workdir / f"endtext_{key}.png")}
                rasterize_cards([card], workdir)
                layers.append(Image.open(card["file"]).convert("RGBA"))
            gap = round(unit * 0.04)
            stack_h = sum(img.height for img in layers) + gap * max(0, len(layers) - 1)
            fps = float(Fraction(rate))
            in_s = min(duration * 0.4, tokens.in_ms / 1000 * 1.6)

            def frames():
                for index in range(_frame_count(duration, rate)):
                    t = index / fps
                    frame = bg.copy()
                    y = (h - stack_h) // 2
                    for layer_index, img in enumerate(layers):
                        delay = 0.12 * layer_index
                        s, _dx, dy, a = transform_at("pop" if layer_index == 0 else "rise", max(0.0, t - delay),
                                                     duration, in_s, 0, unit)
                        sized = img if abs(s - 1) < 1e-4 else img.resize((max(1, round(img.width * s)), max(1, round(img.height * s))), Image.LANCZOS)
                        if a < 0.999:
                            sized = sized.copy()
                            sized.putalpha(sized.getchannel("A").point(lambda v, a=a: round(v * a)))
                        frame.alpha_composite(sized, ((w - sized.width) // 2, round(y + (img.height - sized.height) / 2 + dy)))
                        y += img.height + gap
                    # Whole card fades in from the picture underneath.
                    fade = min(1.0, t / 0.2) if anim != "none" else 1.0
                    if fade < 0.999:
                        frame.putalpha(frame.getchannel("A").point(lambda v, f=fade: round(v * f)))
                    yield frame.tobytes()

            encode_alpha_mov(frames(), canvas, rate, out)
        return {"file": str(out), "start_in_output": start, "duration": duration, "x": 0, "y": 0,
                "w": w, "h": h, "type": kind}

    if kind == "logo":
        image = _load_logo(tokens, element)
        target = round(w * float(element.get("width", tokens.logo.get("width_ratio", 0.2))))
        image = image.resize((target, max(1, round(image.height * target / image.width))), Image.LANCZOS)
        opacity = float(element.get("opacity", tokens.logo.get("opacity", 1.0)))
        if opacity < 1:
            image.putalpha(image.getchannel("A").point(lambda v: round(v * opacity)))
        # A logo can carry its own Drop Shadow, in Premiere's numbers
        # (brand kit logo.shadow = {"distance": 5, "softness": 40, "direction": 135, "opacity": 1}).
        logo_shadow = element.get("shadow", tokens.logo.get("shadow"))
        if isinstance(logo_shadow, dict):
            image = add_drop_shadow(image, premiere_shadow(logo_shadow), unit,
                                    float(logo_shadow.get("direction", 135)) - 90)
        position = element.get("position") or tokens.logo.get("placement") or DEFAULT_POSITION["logo"]
    elif element.get("text_style"):
        from text_styles import render_styled, resolve_style
        recipe = resolve_style(str(element["text_style"]), tokens)
        if recipe is None:
            raise ValueError(f"unknown text_style {element['text_style']!r}: add it to the brand kit's text_styles")
        anim = element.get("animation") or recipe.get("animation") or anim
        shadow = None if element.get("shadow") is False else tokens.shadow
        parts = render_styled(element, recipe, tokens, canvas, workdir, rasterize_cards)
        full_width = bool(recipe.get("streaks"))
        limit = w * (1.0 if full_width else 0.9)
        parts = [(img if img.width <= limit or full_width else img.resize(
            (round(limit), max(1, round(img.height * limit / img.width))), Image.LANCZOS), d) for img, d in parts]
        parts = [(add_drop_shadow(img, shadow, unit), d) for img, d in parts]
        tight = -round(unit * float(recipe.get("line_overlap", 0.03)))  # glow/shadow pads would space lines apart
        image, layers = (stack_layers(parts, tight) if len(parts) > 1 else (parts[0][0], None))
        rtl = any("֐" <= ch <= "ࣿ" for ch in str(element.get("text") or ""))
        box, frames = animate_image(image, anim, duration, rate, tokens, unit, direction=1 if rtl else -1, layers=layers)
        if not out.exists():
            encode_alpha_mov(frames, box, rate, out)
        position = element.get("position") or DEFAULT_POSITION.get(kind, "upper_third")
        x, y = place(position, box, canvas, bleed=bool(element.get("bleed", element.get("behind_subject"))))
        if full_width:
            x = (w - box[0]) // 2
        return {"file": str(out), "start_in_output": start, "duration": duration, "x": x, "y": y,
                "w": box[0], "h": box[1], "type": kind, "full_width": full_width}
    else:
        shadow = None if element.get("shadow") is False else tokens.shadow
        spec = apply_style(card_spec(element, tokens, canvas), element, tokens, canvas)
        if shadow:
            spec.pop("shadow", None)  # one shadow, drawn below for every engine
        spec["file"] = str(workdir / f"card_{key}.png")
        cards = [spec]
        if element.get("style") == "kicker" and element.get("kicker"):
            kicker = card_spec({"type": "sticker", "text": str(element["kicker"]), "scale": 0.8}, tokens, canvas)
            kicker.pop("shadow", None)
            kicker["file"] = str(workdir / f"kicker_{key}.png")
            cards.append(kicker)
        rasterize_cards(cards, workdir)
        image = Image.open(spec["file"]).convert("RGBA")
        tilt = float(element.get("tilt", (tokens.shapes.get("sticker") or {}).get("tilt", -3) if kind == "sticker" else 0))
        if tilt:
            image = image.rotate(tilt, resample=Image.BICUBIC, expand=True)
        parts = [(image, 0.0)]
        if len(cards) > 1:
            parts.insert(0, (Image.open(cards[1]["file"]).convert("RGBA"), 0.0))
            parts[1] = (image, 0.12)
        if element.get("style") == "underline":
            accent = (tokens.shapes.get("headline") or {}).get("accent") or tokens.accent
            parts.append((underline_bar(round(image.width * 0.7), unit, accent), 0.12 * len(parts)))
        # Never wider than the safe area: a long line or a big style scales down
        # instead of running off the frame.
        limit = w * 0.9
        parts = [(img if img.width <= limit else img.resize((round(limit), max(1, round(img.height * limit / img.width))),
                                                             Image.LANCZOS), d) for img, d in parts]
        parts = [(add_drop_shadow(img, shadow, unit), d) for img, d in parts]
        if len(parts) > 1:
            image, layers = stack_layers(parts, round(unit * 0.012))
        else:
            image, layers = parts[0][0], None
        position = element.get("position") or DEFAULT_POSITION[kind]
        rtl = any("֐" <= ch <= "ࣿ" for ch in str(element.get("text") or element.get("title") or ""))
        steps = len(str(element.get("text") or "").split()) if anim == "typewriter" else 1
        box, frames = animate_image(image, anim, duration, rate, tokens, unit, direction=1 if rtl else -1,
                                    layers=layers, steps=steps)
        if not out.exists():
            encode_alpha_mov(frames, box, rate, out)
        x, y = place(position, box, canvas)
        return {"file": str(out), "start_in_output": start, "duration": duration, "x": x, "y": y,
                "w": box[0], "h": box[1], "type": kind}

    rtl = any("֐" <= ch <= "ࣿ" for ch in str(element.get("text") or element.get("title") or ""))
    box, frames = animate_image(image, anim, duration, rate, tokens, unit, direction=1 if rtl else -1)
    if not out.exists():
        encode_alpha_mov(frames, box, rate, out)
    x, y = place(position, box, canvas)
    return {"file": str(out), "start_in_output": start, "duration": duration, "x": x, "y": y,
            "w": box[0], "h": box[1], "type": kind}


def _overlap(a0: float, a1: float, b0: float, b1: float) -> bool:
    return a0 < b1 and b0 < a1


def clear_of_faces(entry: dict, faces: list[dict], canvas: tuple[int, int], placed: list[dict] | None = None) -> dict:
    """Move a graphic to the nearest position that covers no face and no
    graphic already on screen at the same time.

    Rule: text never sits on anyone's face. Searches vertical then horizontal
    offsets inside the safe area; full-frame cards are left alone.
    """
    gap = canvas[1] * 0.008
    others = [{**g, "x": g["x"] - gap, "y": g["y"] - gap, "w": g["w"] + 2 * gap, "h": g["h"] + 2 * gap}
              for g in (placed or []) if g.get("w") and g.get("type") != "end_card"]
    faces = list(faces) + others
    if entry.get("type") in {"end_card", "captions"} or not faces:
        return entry
    w, h = canvas
    t0, t1 = float(entry["start_in_output"]), float(entry["start_in_output"]) + float(entry["duration"])
    active = [f for f in faces if _overlap(t0, t1, f["start_in_output"], f["start_in_output"] + f["duration"])]
    ew, eh = float(entry.get("w", 0)), float(entry.get("h", 0))

    def hits(x: float, y: float) -> bool:
        return any(_overlap(x, x + ew, f["x"], f["x"] + f["w"]) and _overlap(y, y + eh, f["y"], f["y"] + f["h"]) for f in active)

    x0, y0 = float(entry["x"]), float(entry["y"])
    if not hits(x0, y0):
        return entry
    top, bottom = h * 0.04, h * (0.86 if h > w else 0.95) - eh
    left, right = w * 0.03, w * 0.97 - ew
    step = h * 0.01
    candidates = []
    for k in range(1, 200):
        for y in (y0 - k * step, y0 + k * step):
            if top <= y <= bottom:
                candidates.append((abs(y - y0), x0, y))
    for k in range(1, 0 if entry.get("full_width") else 100):  # full-width bands only move vertically
        for x in (x0 - k * step, x0 + k * step):
            if left <= x <= right:
                candidates.append((abs(x - x0) * 1.5, x, y0))
    for _cost, x, y in sorted(candidates):
        if not hits(x, y):
            print(f"    moved {entry.get('type')} clear of faces/graphics: ({x0:.0f},{y0:.0f}) → ({x:.0f},{y:.0f})")
            return {**entry, "x": round(x), "y": round(y)}
    print(f"    warning: no face-free spot for {entry.get('type')} at {t0:.2f}s; check this frame")
    return entry


def build_graphics(edl: dict, edit_dir: Path, canvas: tuple[int, int], rate: str,
                   faces: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    """Render every EDL graphic. Returns (top_overlays, behind_subject_overlays)."""
    elements = edl.get("graphics") or []
    if not elements:
        return [], []
    tokens = Tokens(load_brand_kit(edl, edit_dir), edit_dir)
    workdir = edit_dir / "graphics"
    workdir.mkdir(parents=True, exist_ok=True)
    top: list[dict] = []
    behind: list[dict] = []
    for element in elements:
        if element.get("type") == "logo" and not (element.get("file") or tokens.logo.get("file")):
            print("  graphic logo skipped: the brand kit has no logo file (brand-neutral edit)")
            continue
        entry = build_element(element, edl, tokens, canvas, rate, workdir)
        if "at" in element and "start_in_output" not in element:
            entry["word_time"] = output_time(edl, element["at"], rate)  # when the words are spoken
        entry["text"] = element.get("covers") or element.get("text") or element.get("title") or ""
        if not element.get("behind_subject"):  # behind the person, the person itself keeps the face clear
            entry = clear_of_faces(entry, faces or [], canvas, top + behind)
        print(f"  graphic {element['type']:<11} @ {entry['start_in_output']:.2f}s → {Path(entry['file']).name}")
        (behind if element.get("behind_subject") else top).append(entry)
    return top, behind


def validate_graphics(edl: dict, total: float) -> list[tuple[str, str, str, str]]:
    """Return (level, code, message, path) issues for the EDL validator."""
    issues: list[tuple[str, str, str, str]] = []
    for index, element in enumerate(edl.get("graphics") or []):
        path = f"graphics[{index}]"
        if not isinstance(element, dict) or element.get("type") not in GRAPHIC_TYPES:
            issues.append(("error", "invalid_graphic_type", f"type must be one of {sorted(GRAPHIC_TYPES)}.", path))
            continue
        kind = element["type"]
        if kind in {"sticker", "title_card", "number", "headline"} and not str(element.get("text", "")).strip():
            issues.append(("error", "graphic_missing_text", f"{kind} needs text.", f"{path}.text"))
        if kind == "lower_third" and not (element.get("title") or element.get("text")):
            issues.append(("error", "graphic_missing_text", "lower_third needs title.", f"{path}.title"))
        if element.get("text_style") is not None and not isinstance(element.get("text_style"), str):
            issues.append(("error", "invalid_text_style", "text_style must be a style name.", f"{path}.text_style"))
        style = element.get("style")
        if style is not None and style not in STYLES:
            issues.append(("error", "invalid_graphic_style", f"style must be one of {sorted(STYLES)}.", f"{path}.style"))
        anim = element.get("animation")
        if anim is not None and anim not in ANIMATIONS:
            issues.append(("error", "invalid_graphic_animation", f"animation must be one of {sorted(ANIMATIONS)}.", f"{path}.animation"))
        try:
            duration = float(element["duration"])
            start = float(element["start_in_output"]) if "start_in_output" in element else None
        except (KeyError, TypeError, ValueError):
            issues.append(("error", "invalid_graphic_time", "duration (and start_in_output or at) must be numbers.", path))
            continue
        if start is None and not isinstance(element.get("at"), dict):
            issues.append(("error", "invalid_graphic_time", "Give start_in_output or at: {source, time}.", path))
            continue
        if duration <= 0 or (start is not None and (start < 0 or start + duration > total + 0.03)):
            issues.append(("error", "graphic_outside_output", "Graphic must fall within the output timeline.", path))
        if start is not None and kind in {"sticker", "number", "headline"} and duration < 0.8:
            issues.append(("warning", "graphic_too_short", "Under 0.8 s is hard to read.", f"{path}.duration"))
        if kind == "end_card" and element.get("behind_subject"):
            issues.append(("error", "invalid_behind_subject", "end_card is full-frame and cannot sit behind the subject.", path))
    return issues
