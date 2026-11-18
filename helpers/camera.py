"""Virtual camera moves: keyframed zoom in/out, pans, and face tracking.

A range can carry a smooth move::

    "zoom": {"from": 1.0, "to": 1.15, "ease": "smooth"}            # push in
    "zoom": {"from": 1.18, "to": 1.0, "start": 0.4, "duration": 1.2} # pull out
    "camera": [{"t": 0, "scale": 1.0},
               {"t": 1.5, "scale": 1.12, "ease": "in_out"},
               {"t": 3.0, "scale": 1.0, "x": 0.45, "ease": "smooth"}]
    "track": "face"                                                 # follow the face

Keyframe times are seconds from the range start; ``x``/``y`` are the window
centre (0–1 of the source). Each segment eases from the previous key to its
own key. The move is rendered with ffmpeg ``perspective`` evaluated per frame
with cubic interpolation, so motion is subpixel-smooth (no zoompan jitter),
and the crop comes from source pixels before the final lanczos scale.
"""

from __future__ import annotations

from typing import Any

EASES = {"linear", "in", "out", "in_out", "smooth"}


def ease_expr(p: str, ease: str) -> str:
    """ffmpeg expression for an easing curve of progress ``p`` (0..1)."""
    if ease == "linear":
        return p
    if ease == "in":
        return f"pow({p},3)"
    if ease == "out":
        return f"(1-pow(1-{p},3))"
    if ease == "in_out":
        return f"if(lt({p},0.5),4*pow({p},3),1-pow(2-2*{p},3)/2)"
    # smootherstep: zero velocity *and* acceleration at both ends
    return f"(pow({p},3)*({p}*({p}*6-15)+10))"


def ease_value(p: float, ease: str) -> float:
    """Python twin of :func:`ease_expr` (tests, previews)."""
    p = min(1.0, max(0.0, p))
    if ease == "linear":
        return p
    if ease == "in":
        return p ** 3
    if ease == "out":
        return 1 - (1 - p) ** 3
    if ease == "in_out":
        return 4 * p ** 3 if p < 0.5 else 1 - (2 - 2 * p) ** 3 / 2
    return p ** 3 * (p * (p * 6 - 15) + 10)


def channel_expr(keys: list[tuple[float, float, str]], time: str) -> str:
    """Piecewise eased curve through (t, value, ease) keys as one expression."""
    if not keys:
        raise ValueError("no keys")
    keys = sorted(keys, key=lambda k: k[0])
    expr = f"{keys[-1][1]:.6f}"
    for (t0, v0, _e0), (t1, v1, e1) in reversed(list(zip(keys, keys[1:]))):
        span = max(t1 - t0, 1e-3)
        p = f"clip(({time}-{t0:.4f})/{span:.4f},0,1)"
        seg = f"({v0:.6f}+({v1 - v0:.6f})*{ease_expr(p, e1)})"
        expr = f"if(lt({time},{t1:.4f}),{seg},{expr})"
    return expr


def channel_value(keys: list[tuple[float, float, str]], t: float) -> float:
    keys = sorted(keys, key=lambda k: k[0])
    if t <= keys[0][0]:
        return keys[0][1]
    for (t0, v0, _e0), (t1, v1, e1) in zip(keys, keys[1:]):
        if t < t1:
            return v0 + (v1 - v0) * ease_value((t - t0) / max(t1 - t0, 1e-3), e1)
    return keys[-1][1]


def normalize_keys(r: dict, base_center: tuple[float, float], duration: float,
                   track: list[tuple[float, float, float]] | None = None) -> dict[str, list] | None:
    """Range move spec → per-channel keys {scale, x, y}; None when static."""
    scale: list[tuple[float, float, str]] = []
    xs: list[tuple[float, float, str]] = []
    ys: list[tuple[float, float, str]] = []
    zoom = r.get("zoom")
    if isinstance(zoom, dict):
        start = float(zoom.get("start", 0.0))
        length = float(zoom.get("duration") or max(0.1, duration - start))
        ease = zoom.get("ease", "smooth")
        scale = [(0.0, float(zoom.get("from", 1.0)), "linear"), (start, float(zoom.get("from", 1.0)), "linear"),
                 (start + length, float(zoom.get("to", 1.1)), ease)]
    for key in r.get("camera") or []:
        t, ease = float(key.get("t", 0.0)), key.get("ease", "smooth")
        if "scale" in key:
            scale.append((t, float(key["scale"]), ease))
        if "x" in key:
            xs.append((t, float(key["x"]), ease))
        if "y" in key:
            ys.append((t, float(key["y"]), ease))
    if track:
        xs = [(t, x, "linear") for t, x, _y in track]
        ys = [(t, y, "linear") for t, _x, y in track]
    if not (scale or xs or ys):
        return None
    base_scale = float((r.get("punch_in") or {}).get("scale", 1.0))
    return {
        "scale": scale or [(0.0, base_scale, "linear")],
        "x": xs or [(0.0, base_center[0], "linear")],
        "y": ys or [(0.0, base_center[1], "linear")],
    }


def camera_filter(canvas: tuple[int, int], source: tuple[int, int], keys: dict[str, list], fps: float,
                  t_offset: float = 0.0, headroom: float = 0.36) -> str:
    """Per-frame eased crop window → ``perspective`` (cubic) → lanczos scale.

    The window has the canvas aspect at every scale; ``perspective`` maps it
    onto the full source frame and the final scale restores the true aspect,
    so the result is an exact, subpixel crop.
    """
    W, H = canvas
    sw, sh = source
    target = W / H
    cw0, ch0 = (sh * target, float(sh)) if sw / sh > target else (float(sw), sw / target)
    time = f"(in/{fps:.6f}+{t_offset:.4f})"
    s = channel_expr(keys["scale"], time)
    cx = channel_expr(keys["x"], time)
    cy = channel_expr(keys["y"], time)
    ww = f"({cw0:.3f}/({s}))"
    wh = f"({ch0:.3f}/({s}))"
    left = f"clip({cx}*{sw}-{ww}/2,0,{sw}-{ww})"
    top = f"clip({cy}*{sh}-{wh}*{headroom},0,{sh}-{wh})"
    right, bottom = f"({left}+{ww})", f"({top}+{wh})"
    corners = {"x0": left, "y0": top, "x1": right, "y1": top, "x2": left, "y2": bottom, "x3": right, "y3": bottom}
    args = ":".join(f"{k}='{v}'" for k, v in corners.items())
    return f"perspective={args}:interpolation=cubic:eval=frame,scale={W}:{H}:flags=lanczos,setsar=1"


def smooth_track(samples: list[tuple[float, float | None, float | None]], time_constant: float = 0.7,
                 deadzone: float = 0.03, step: float = 0.25) -> list[tuple[float, float, float]] | None:
    """Face samples → a calm camera path, or None when a locked shot is better.

    Gaps are filled from neighbours, the path is low-passed forward and
    backward (no lag), and if the face never strays further than
    ``deadzone`` the shot stays locked off — a camera that drifts for no
    reason reads as amateur.
    """
    known = [(t, x, y) for t, x, y in samples if x is not None and y is not None]
    if len(known) < 2:
        return None
    filled = []
    for t, x, y in samples:
        if x is None or y is None:
            near = min(known, key=lambda k: abs(k[0] - t))
            x, y = near[1], near[2]
        filled.append((t, x, y))
    xs = [f[1] for f in filled]
    ys = [f[2] for f in filled]
    if max(xs) - min(xs) < deadzone and max(ys) - min(ys) < deadzone:
        return None

    def zero_phase(values: list[float]) -> list[float]:
        dt = (filled[-1][0] - filled[0][0]) / max(1, len(filled) - 1) or step
        a = dt / (time_constant + dt)
        out = list(values)
        for i in range(1, len(out)):
            out[i] = out[i - 1] + a * (out[i] - out[i - 1])
        for i in range(len(out) - 2, -1, -1):
            out[i] = out[i + 1] + a * (out[i] - out[i + 1])
        return out

    sx, sy = zero_phase(xs), zero_phase(ys)
    path, last_t = [], None
    for (t, _x, _y), x, y in zip(filled, sx, sy):
        if last_t is None or t - last_t >= step - 1e-6 or t == filled[-1][0]:
            path.append((round(t, 3), round(x, 4), round(y, 4)))
            last_t = t
    return path


def validate_motion(r: dict[str, Any], path: str) -> list[tuple[str, str, str, str]]:
    issues = []
    zoom = r.get("zoom")
    if zoom is not None:
        try:
            if not isinstance(zoom, dict):
                raise ValueError
            for key in ("from", "to"):
                if not 1.0 <= float(zoom.get(key, 1.0)) <= 2.0:
                    raise ValueError
            if zoom.get("ease", "smooth") not in EASES:
                raise ValueError
        except (TypeError, ValueError):
            issues.append(("error", "invalid_zoom", f"zoom needs from/to 1.0–2.0 and ease in {sorted(EASES)}.", f"{path}.zoom"))
    for k, key in enumerate(r.get("camera") or []):
        try:
            if not isinstance(key, dict) or float(key.get("t", 0)) < 0:
                raise ValueError
            if "scale" in key and not 1.0 <= float(key["scale"]) <= 2.0:
                raise ValueError
            for axis in ("x", "y"):
                if axis in key and not 0.0 <= float(key[axis]) <= 1.0:
                    raise ValueError
            if key.get("ease", "smooth") not in EASES:
                raise ValueError
        except (TypeError, ValueError):
            issues.append(("error", "invalid_camera_key", "camera keys need t ≥ 0, scale 1.0–2.0, x/y 0–1, a known ease.", f"{path}.camera[{k}]"))
    if r.get("track") not in (None, "face"):
        issues.append(("error", "invalid_track", 'track must be "face".', f"{path}.track"))
    return issues
