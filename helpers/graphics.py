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
import subprocess
import sys
import tempfile
from fractions import Fraction
from pathlib import Path
from typing import Any

from PIL import Image

GRAPHIC_TYPES = {"sticker", "title_card", "number", "headline", "lower_third", "logo", "end_card"}
ANIMATIONS = {"pop", "rise", "slide", "fade", "none"}
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

    def _font(self, value: str | None) -> str | None:
        if not value:
            return None
        candidate = Path(value).expanduser()
        if not candidate.is_absolute() and (self.base / candidate).exists():
            candidate = self.base / candidate
        return str(candidate) if candidate.suffix.lower() in {".ttf", ".otf", ".ttc"} else value

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


def rasterize_cards(cards: list[dict], workdir: Path) -> None:
    """Render every card whose PNG is missing in one CoreText process."""
    todo = [c for c in cards if not Path(c["file"]).exists()]
    if not todo:
        return
    if sys.platform != "darwin":
        raise RuntimeError("graphics text rendering uses macOS CoreText; run on macOS")
    payload = workdir / "cards.json"
    clean = [{k: v for k, v in card.items() if v is not None} for card in todo]
    payload.write_text(json.dumps({"cards": clean}, ensure_ascii=False), encoding="utf-8")
    subprocess.run([str(_ensure_renderer()), str(payload)], check=True)


# ---------------------------------------------------------------- motion

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
                  unit: float, direction: int = 1, out_ms: float = 160):
    """Yield (box_size, frames) for a card animated inside a fixed box."""
    in_s = 0.0 if anim == "none" else min(duration * 0.45, tokens.in_ms / 1000)
    out_s = 0.0 if anim == "none" else min(duration * 0.25, out_ms / 1000)
    pad_x = round(unit * 0.12) + 4 if anim == "slide" else 4
    pad_y = round(unit * 0.05) + 4 if anim == "rise" else 4
    grow = 1.12 if anim == "pop" else 1.0
    box = (math.ceil(image.width * grow) + 2 * pad_x, math.ceil(image.height * grow) + 2 * pad_y)
    box = (box[0] + box[0] % 2, box[1] + box[1] % 2)
    total = _frame_count(duration, rate)
    fps = float(Fraction(rate))
    cache: dict[tuple, bytes] = {}

    def frames():
        for index in range(total):
            s, dx, dy, a = transform_at(anim, index / fps, duration, in_s, out_s, unit, direction)
            key = (round(s, 4), round(dx, 1), round(dy, 1), round(a, 3))
            if key not in cache:
                canvas = Image.new("RGBA", box, (0, 0, 0, 0))
                sized = image if abs(s - 1) < 1e-4 else image.resize(
                    (max(1, round(image.width * s)), max(1, round(image.height * s))), Image.LANCZOS)
                if a < 0.999:
                    alpha = sized.getchannel("A").point(lambda v, a=a: round(v * a))
                    sized = sized.copy()
                    sized.putalpha(alpha)
                x = round((box[0] - sized.width) / 2 + dx)
                y = round((box[1] - sized.height) / 2 + dy)
                canvas.alpha_composite(sized, (x, y))
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
        if item.get("source") == at.get("source") and float(item["start"]) <= float(at["time"]) <= float(item["end"]):
            return round(offset + min(duration, float(at["time"]) - float(item["start"])), 3)
    raise ValueError(f"graphic anchor {at} is not inside any kept range")


def place(position: Any, box: tuple[int, int], canvas: tuple[int, int]) -> tuple[int, int]:
    """Centre the box on a named or normalized point, clamped to the safe area."""
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
    x = min(max(x, margin_x - 4), max(margin_x - 4, w - margin_x - box[0] + 4))
    y = min(max(y, top - 4), max(top - 4, bottom - box[1] + 4))
    return x, y


# ---------------------------------------------------------------- builders

def _code_version() -> str:
    """Changes whenever the renderer code changes, so cached graphics refresh."""
    h = hashlib.sha1()
    for source in (Path(__file__), SWIFT_SOURCE):
        h.update(source.read_bytes())
    return h.hexdigest()[:8]


def _digest(*parts: Any) -> str:
    blob = json.dumps([_code_version(), *parts], sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def _load_logo(tokens: Tokens, element: dict) -> Image.Image:
    path = tokens.asset(element.get("file") or tokens.logo.get("file"))
    if path is None or not path.is_file():
        raise ValueError("logo graphic needs brand_kit.logo.file (PNG with transparency) or element.file")
    image = Image.open(path).convert("RGBA")
    bbox = image.getchannel("A").getbbox()  # drop transparent padding
    return image.crop(bbox) if bbox else image


def build_element(element: dict, edl: dict, tokens: Tokens, canvas: tuple[int, int], rate: str,
                  workdir: Path) -> dict:
    """Render one graphic element and return an overlay entry for render.py."""
    kind = element["type"]
    w, h = canvas
    unit = min(w, h)
    if "at" in element and "start_in_output" not in element:
        start = output_time(edl, element["at"], rate)
    else:
        start = float(element["start_in_output"])
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
        return {"file": str(out), "start_in_output": start, "duration": duration, "x": 0, "y": 0}

    if kind == "logo":
        image = _load_logo(tokens, element)
        target = round(w * float(element.get("width", tokens.logo.get("width_ratio", 0.2))))
        image = image.resize((target, max(1, round(image.height * target / image.width))), Image.LANCZOS)
        opacity = float(element.get("opacity", tokens.logo.get("opacity", 1.0)))
        if opacity < 1:
            image.putalpha(image.getchannel("A").point(lambda v: round(v * opacity)))
        position = element.get("position") or tokens.logo.get("placement") or DEFAULT_POSITION["logo"]
    else:
        spec = card_spec(element, tokens, canvas)
        spec["file"] = str(workdir / f"card_{key}.png")
        rasterize_cards([spec], workdir)
        image = Image.open(spec["file"]).convert("RGBA")
        tilt = float(element.get("tilt", (tokens.shapes.get("sticker") or {}).get("tilt", -3) if kind == "sticker" else 0))
        if tilt:
            image = image.rotate(tilt, resample=Image.BICUBIC, expand=True)
        position = element.get("position") or DEFAULT_POSITION[kind]

    rtl = any("֐" <= ch <= "ࣿ" for ch in str(element.get("text") or element.get("title") or ""))
    box, frames = animate_image(image, anim, duration, rate, tokens, unit, direction=1 if rtl else -1)
    if not out.exists():
        encode_alpha_mov(frames, box, rate, out)
    x, y = place(position, box, canvas)
    return {"file": str(out), "start_in_output": start, "duration": duration, "x": x, "y": y}


def build_graphics(edl: dict, edit_dir: Path, canvas: tuple[int, int], rate: str) -> tuple[list[dict], list[dict]]:
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
