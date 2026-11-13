"""Make any stock overlay brand-ready: key it, recolour it, glow it, fit it.

Green-screen and blue-screen clips (and alpha MOVs) from the material folder
rarely match a brand as-is. ``prepare`` turns one into a canvas-sized alpha
MOV the renderer can lay over the picture or behind the subject::

    python helpers/overlay_fx.py "question mark ping.mp4" -o qmarks.mov \\
        --canvas 1080x1920 --recolor "#3FB7FF" --glow "#3F6BFF" --keep-out subject

In an EDL, any overlay (``overlays`` or ``behind_subject_overlays``) may carry
``"prepare": {...}`` with the same options; the renderer builds and caches it.

Options:
- ``key``: ``auto`` (alpha → keep, green/blue screen → key), ``green``, ``blue``, ``none``.
- ``recolor``: one colour, or ``[dark, light]`` mapped by brightness; the brightest
  cores go toward white so the shape keeps its depth.
- ``glow``: halo colour (``#RRGGBB[AA]``); ``glow_radius`` as a fraction of width.
- ``fit``: ``width`` (default), ``cover`` or ``contain``; ``scale`` multiplies it;
  ``offset`` [x, y] in canvas fractions.
- ``keep_out``: ``"subject"`` keeps the head-and-shoulders band of a centred
  speaker clear (a soft matte is never perfect at hair and glasses), or a list
  of ``[x0, y0, x1, y1]`` boxes in canvas fractions.
- ``play``: ``once`` (default) or ``loop`` to ``duration``; ``mirror`` flips it.
- ``fade_frames``: soft in/out so the layer never pops.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageFilter


def _rgb(value: str) -> np.ndarray:
    v = value.lstrip("#")
    return np.array([int(v[k:k + 2], 16) for k in (0, 2, 4)], np.float32)


def _alpha_of(value: str, default: float = 1.0) -> float:
    v = value.lstrip("#")
    return int(v[6:8], 16) / 255 if len(v) == 8 else default


def detect_key(path: Path) -> str:
    """'alpha', 'green', 'blue' or 'none' for a clip."""
    sys_path()
    from material import probe, screen_colour
    info = probe(path)
    if info.get("alpha"):
        return "alpha"
    return screen_colour(path, float(info.get("duration_s") or 0)) or "none"


def sys_path() -> None:
    import sys
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)


def keyed_rgba(frame: np.ndarray, key: str) -> np.ndarray:
    """RGBA float frame from RGB(A) input with the screen colour removed."""
    rgb = frame[..., :3].astype(np.float32)
    if frame.shape[-1] == 4:
        alpha = frame[..., 3].astype(np.float32) / 255
    else:
        r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
        if key == "green":
            spill = np.clip((g - np.maximum(r, b)) / 255.0, 0, 1)
            alpha = np.clip(1.0 - spill * 2.4, 0, 1)
            rgb[..., 1] = np.minimum(g, np.maximum(r, b) + 10)  # despill
        elif key == "blue":
            spill = np.clip((b - np.maximum(r, g)) / 255.0, 0, 1)
            alpha = np.clip(1.0 - spill * 2.4, 0, 1)
            rgb[..., 2] = np.minimum(b, np.maximum(r, g) + 10)
        else:
            alpha = np.ones(rgb.shape[:2], np.float32)
    return np.dstack([rgb, alpha])


def recolor(rgba: np.ndarray, colors: str | list[str]) -> np.ndarray:
    lum = rgba[..., :3].mean(-1) / 255
    dark, light = (_rgb(colors), _rgb(colors)) if isinstance(colors, str) else (_rgb(colors[0]), _rgb(colors[-1]))
    t = np.clip((lum - 0.25) / 0.6, 0, 1)[..., None]
    rgb = dark * (1 - t) + light * t
    core = np.clip((lum - 0.8) / 0.2, 0, 1)[..., None]  # hot cores toward white: keeps the shape's depth
    rgb = rgb * (1 - core * 0.7) + 255 * core * 0.7
    return np.dstack([rgb, rgba[..., 3]])


def keep_out_matte(size: tuple[int, int], keep_out: Any) -> np.ndarray | None:
    if not keep_out:
        return None
    w, h = size
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    matte = np.ones((h, w), np.float32)
    boxes = [[0.5 - 0.215, 0.0, 0.5 + 0.215, 0.40]] if keep_out == "subject" else keep_out
    feather = 0.03 * w
    for x0, y0, x1, y1 in boxes:
        dx = np.maximum(x0 * w - xx, xx - x1 * w)
        dy = np.maximum(y0 * h - yy, yy - y1 * h)
        outside = np.clip(np.maximum(dx, dy) / feather, 0, 1)
        matte = np.minimum(matte, outside)
    return matte


def prepare(src: Path, out: Path, canvas: tuple[int, int], fps: float = 30, key: str = "auto",
            recolor_to: str | list[str] | None = None, glow: str | None = None, glow_radius: float = 0.013,
            fit: str = "width", scale: float = 1.0, offset: tuple[float, float] = (0.0, 0.0), keep_out: Any = None,
            play: str = "once", duration: float | None = None, mirror: bool = False, fade_frames: int = 4) -> Path:
    W, H = canvas
    key = detect_key(src) if key == "auto" else key
    sys_path()
    from material import probe
    info = probe(src)
    sw, sh = int(info.get("width") or W), int(info.get("height") or H)
    k = {"width": W / sw, "cover": max(W / sw, H / sh), "contain": min(W / sw, H / sh)}.get(fit, W / sw) * scale
    tw, th = max(2, round(sw * k) // 2 * 2), max(2, round(sh * k) // 2 * 2)
    pix = "rgba" if key == "alpha" else "rgb24"
    chans = 4 if pix == "rgba" else 3
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(src), "-vf", f"fps={fps},scale={tw}:{th}", "-pix_fmt", pix,
                          "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    frames = np.frombuffer(raw, np.uint8).reshape(-1, th, tw, chans)
    n_src = len(frames)
    total = n_src if play == "once" or not duration else round(duration * fps)
    if play == "once" and duration:
        total = min(total, round(duration * fps))
    matte = keep_out_matte((W, H), keep_out)
    ox = round((W - tw) / 2 + offset[0] * W)
    oy = round((H - th) / 2 + offset[1] * H) if fit != "width" else round(offset[1] * H)
    proc = subprocess.Popen(["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{W}x{H}",
                             "-r", str(fps), "-i", "-", "-c:v", "png", "-pix_fmt", "rgba", str(out)], stdin=subprocess.PIPE)
    cache: dict[int, Image.Image] = {}
    try:
        for i in range(total):
            j = i % n_src
            if j not in cache:
                rgba = keyed_rgba(frames[j], "none" if key in ("alpha", "none") else key)
                if recolor_to:
                    rgba = recolor(rgba, recolor_to)
                layer = Image.fromarray(np.clip(rgba * [1, 1, 1, 255], 0, 255).astype(np.uint8), "RGBA")
                if mirror:
                    layer = layer.transpose(Image.FLIP_LEFT_RIGHT)
                if glow:
                    halo = Image.new("RGBA", layer.size, tuple(int(v) for v in _rgb(glow)) + (0,))
                    strength = 2.2 * _alpha_of(glow)
                    halo.putalpha(layer.getchannel("A").filter(ImageFilter.GaussianBlur(max(1, glow_radius * W)))
                                  .point(lambda v, s=strength: min(255, int(v * s))))
                    halo.alpha_composite(layer)
                    layer = halo
                cache[j] = layer
            canvas_img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            canvas_img.alpha_composite(cache[j], (ox, oy)) if ox >= 0 and oy >= 0 else canvas_img.paste(cache[j], (ox, oy), cache[j])
            a = np.asarray(canvas_img.getchannel("A"), np.float32)
            if matte is not None:
                a = a * matte
            edge = min(1.0, (i + 1) / max(1, fade_frames), (total - i) / max(1, fade_frames)) if fade_frames else 1.0
            canvas_img.putalpha(Image.fromarray((a * edge).astype(np.uint8), "L"))
            proc.stdin.write(canvas_img.tobytes())
    finally:
        proc.stdin.close()
        proc.wait()
    return out


def prepared_overlay(ov: dict, edit_dir: Path, canvas: tuple[int, int], rate: str) -> dict:
    """Resolve an EDL overlay's ``prepare`` block into a cached alpha MOV."""
    spec = ov.get("prepare")
    if not spec:
        return ov
    from fractions import Fraction
    raw = str(ov.get("source") or ov["file"])
    if raw.startswith("material:"):  # an id from `material.py search`
        sys_path()
        from library import load
        raw = load()["items"][raw.split(":", 1)[1]]["file"]
    src = Path(raw).expanduser()
    if not src.is_absolute():
        src = edit_dir / src
    key = hashlib.sha1(json.dumps([str(src), src.stat().st_mtime, spec, canvas, rate], sort_keys=True).encode()
                       + Path(__file__).read_bytes()).hexdigest()[:12]
    out = edit_dir / "overlays_fx" / f"{src.stem[:30]}_{key}.mov"
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.exists():
        prepare(src, out, canvas, float(Fraction(rate)), key=spec.get("key", "auto"), recolor_to=spec.get("recolor"),
                glow=spec.get("glow"), glow_radius=float(spec.get("glow_radius", 0.013)), fit=spec.get("fit", "width"),
                scale=float(spec.get("scale", 1.0)), offset=tuple(spec.get("offset", (0.0, 0.0))),
                keep_out=spec.get("keep_out"), play=spec.get("play", "once"),
                duration=float(ov["duration"]) if ov.get("duration") else None, mirror=bool(spec.get("mirror")),
                fade_frames=int(spec.get("fade_frames", 4)))
        print(f"  overlay fx → {out.name}")
    probe_out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out)],
                               capture_output=True, text=True).stdout.strip()
    new = {k: v for k, v in ov.items() if k not in ("prepare", "source")}
    new.update(file=str(out), x=0, y=0)
    if probe_out:
        new["duration"] = min(float(ov.get("duration") or 1e9), float(probe_out))
    return new


def main() -> None:
    parser = argparse.ArgumentParser(description="Key / recolour / glow a stock overlay into a brand-ready alpha MOV")
    parser.add_argument("src", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--canvas", default="1080x1920")
    parser.add_argument("--fps", type=float, default=30)
    parser.add_argument("--key", default="auto", choices=["auto", "green", "blue", "alpha", "none"])
    parser.add_argument("--recolor", nargs="+")
    parser.add_argument("--glow")
    parser.add_argument("--fit", default="width", choices=["width", "cover", "contain"])
    parser.add_argument("--scale", type=float, default=1.0)
    parser.add_argument("--offset", type=float, nargs=2, default=(0.0, 0.0))
    parser.add_argument("--keep-out", default=None, help='"subject" or a JSON list of [x0,y0,x1,y1] boxes')
    parser.add_argument("--play", default="once", choices=["once", "loop"])
    parser.add_argument("--duration", type=float)
    parser.add_argument("--mirror", action="store_true")
    args = parser.parse_args()
    w, h = (int(v) for v in args.canvas.lower().split("x"))
    keep = args.keep_out if args.keep_out in (None, "subject") else json.loads(args.keep_out)
    colors = None if not args.recolor else (args.recolor[0] if len(args.recolor) == 1 else args.recolor)
    out = prepare(args.src, args.output, (w, h), args.fps, args.key, colors, args.glow, fit=args.fit, scale=args.scale,
                  offset=tuple(args.offset), keep_out=keep, play=args.play, duration=args.duration, mirror=args.mirror)
    print(f"done: {out}")


if __name__ == "__main__":
    main()
