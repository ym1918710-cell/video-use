"""Sound for every visual effect.

Every graphic entrance, transition and camera move gets a matching sound, so
nothing on screen moves in silence. The sounds are synthesised here (no
licences, no downloads) into ``<edit>/sfx/auto_<name>.wav``; a project or
brand kit can map any kind to its own licensed file instead::

    brand_kit.json / edl.audio:  "sfx_map": {"pop": "sfx/my_pop.wav", "light": "sfx/flash.wav"}

Opt out per element with ``"sfx": false`` (or name a sound: ``"sfx": "impact"``)
and for the whole edit with ``edl.audio.auto_sfx: false``.
"""

from __future__ import annotations

import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any

import numpy as np

RATE = 48000
SOUNDS = ("pop", "whoosh", "swoosh", "impact", "tick", "shimmer", "riser", "soft")

# A library file's role, from words in its name (checked in order). Files
# that match nothing — swords, engines, horror pads, memes — are never picked
# automatically; place them by hand when a moment calls for them.
ROLE_KEYWORDS = [
    ("error", ("error", "wrong", "buzzer", "fail", "lose game", "loser")),
    ("riser", ("riser", "rise ", "uplifter")),
    ("tick", ("typewriter", "teletype", "keyboard", "typing", "ticking", "tick")),
    ("shimmer", ("shine", "glitter", "sparkle", "twinkle", "magic", "camera-flash", "camera flash", "flash")),
    ("zoom", ("zoom",)),
    ("impact", ("stamp", "boom", "hit", "impact", "punch", "thud", "braam")),
    ("pop", ("pop", "bubble", "notification", "click", "ping", "digits", "digital pop")),
    ("soft", ("soft slide", "paper_away", "soft")),
    ("swoosh", ("swoosh", "slide")),
    ("whoosh", ("whoosh", "woosh", "swish")),
]
# Integrated loudness each role should sit at before the final mix (LUFS):
# effects support the voice, they never compete with it.
ROLE_LUFS = {"pop": -27, "impact": -25, "tick": -30, "whoosh": -27, "swoosh": -27, "shimmer": -29,
             "soft": -31, "riser": -28, "zoom": -27, "error": -27}


def classify(name: str) -> str | None:
    low = name.lower()
    for role, words in ROLE_KEYWORDS:
        if any(w in low for w in words):
            return role
    return None


def _library_items() -> list[dict[str, Any]]:
    try:
        sys_path_helpers()
        from library import load
        return list(load().get("items", {}).values())
    except Exception:
        return []


def library_choices(role: str) -> list[dict[str, Any]]:
    """Library SFX for a role, shortest first, wav preferred over its mp3 twin."""
    try:
        sys_path_helpers()
        from library import load
        items = [i for i in load().get("items", {}).values() if i.get("type") == "sfx"]
    except Exception:
        return []
    picks = [i for i in items if (i.get("role") or classify(i.get("name", ""))) == role
             and 0.05 < float(i.get("duration_s") or 0) <= (4.0 if role == "riser" else 3.0 if role == "shimmer" else 2.5)]
    by_name: dict[str, dict] = {}
    for item in sorted(picks, key=lambda i: (not str(i["file"]).lower().endswith(".wav"), i["duration_s"])):
        by_name.setdefault(item["name"].lower(), item)
    return sorted(by_name.values(), key=lambda i: (i["duration_s"], i["name"]))

# Which sound an effect makes, and how far before the visual beat it starts
# (a whoosh peaks a little after it begins; a pop hits at once).
ANIMATION_SOUND = {
    "pop": "pop", "stamp": "impact", "drop": "impact", "rise": "swoosh", "zoom": "swoosh",
    "slide": "whoosh", "wipe": "whoosh", "fade": "soft", "blur": "soft", "typewriter": "tick",
    "words": "tick", "none": None,
}
TRANSITION_SOUND = {
    "light": "shimmer", "fade": "soft", "dissolve": "soft", "dip_black": "soft", "dip_white": "shimmer",
    "whip": "whoosh", "slide": "whoosh", "wipe": "whoosh", "zoom": "swoosh",
}
LEAD = {"zoom": 0.1, "pop": 0.0, "impact": 0.0, "tick": 0.0, "whoosh": 0.14, "swoosh": 0.1, "shimmer": 0.2,
        "soft": 0.05, "riser": 0.55}
GAIN = {"zoom": -17, "pop": -14, "impact": -12, "tick": -20, "whoosh": -17, "swoosh": -17, "shimmer": -19,
        "soft": -21, "riser": -18}


def _env(n: int, attack: float, decay: float) -> np.ndarray:
    t = np.arange(n) / RATE
    return np.minimum(1, t / max(attack, 1e-4)) * np.exp(-np.maximum(0, t - attack) / max(decay, 1e-4))


def _smooth(x: np.ndarray, k: int) -> np.ndarray:
    k = max(1, int(k))
    return np.convolve(x, np.ones(k) / k, mode="same")


def synth(name: str, seed: int = 3) -> np.ndarray:
    """Mono float32 samples for one stock sound."""
    rng = np.random.default_rng(seed)
    if name == "pop":
        n = int(0.18 * RATE)
        t = np.arange(n) / RATE
        f = 380 + 900 * np.exp(-t * 40)
        return (np.sin(2 * np.pi * np.cumsum(f) / RATE) * np.exp(-t * 24) * np.minimum(1, t / 0.002)).astype(np.float32)
    if name in ("whoosh", "swoosh", "soft", "riser"):
        dur = {"whoosh": 0.42, "swoosh": 0.3, "soft": 0.5, "riser": 0.7}[name]
        n = int(dur * RATE)
        t = np.linspace(0, 1, n)
        noise = rng.normal(size=n)
        low, high = _smooth(noise, 40), noise - _smooth(noise, 6)
        if name == "riser":
            bright, shape = t ** 1.5, t ** 2.2 * (1 - np.clip((t - 0.96) / 0.04, 0, 1))
        else:
            peak = 0.35 if name != "soft" else 0.45
            shape = np.where(t < peak, (t / peak) ** 2, np.exp(-(t - peak) / (0.18 if name == "swoosh" else 0.25)))
            bright = np.sin(np.pi * np.clip(t / (2 * peak), 0, 1)) if name != "soft" else 0.25 * shape
        out = (low * 3.0 * (1 - bright) + high * 0.5 * bright) * shape
        return (out / (np.abs(out).max() + 1e-9) * (0.5 if name == "soft" else 0.8)).astype(np.float32)
    if name == "impact":
        n = int(0.45 * RATE)
        t = np.arange(n) / RATE
        f = 48 + 70 * np.exp(-t * 18)
        thump = np.sin(2 * np.pi * np.cumsum(f) / RATE) * np.exp(-t * 7)
        crack = _smooth(rng.normal(size=n), 3) * np.exp(-t * 60) * 0.5
        return ((thump + crack) / 1.3 * np.minimum(1, t / 0.001)).astype(np.float32)
    if name == "tick":
        n = int(0.03 * RATE)
        t = np.arange(n) / RATE
        return (np.sin(2 * np.pi * 2600 * t) * np.exp(-t * 260) * 0.8).astype(np.float32)
    if name == "shimmer":
        n = int(0.7 * RATE)
        t = np.arange(n) / RATE
        out = np.zeros(n)
        for k, f in enumerate((2093, 2637, 3136, 4186, 5274)):
            out += np.sin(2 * np.pi * f * t + k) * (0.6 + 0.4 * np.sin(2 * np.pi * (7 + k) * t))
        air = (rng.normal(size=n) - _smooth(rng.normal(size=n), 4)) * 0.4
        env = np.where(t < 0.2, (t / 0.2) ** 2, np.exp(-(t - 0.2) / 0.16))
        out = (out / 5 + air) * env
        return (out / (np.abs(out).max() + 1e-9) * 0.7).astype(np.float32)
    raise ValueError(f"unknown stock sound {name!r}; choose from {SOUNDS}")


def stock_file(name: str, edit_dir: Path) -> Path:
    path = edit_dir / "sfx" / f"auto_{name}.wav"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        mono = synth(name)
        stereo = np.stack([mono, mono], 1).astype(np.float32)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "f32le", "-ar", str(RATE), "-ac", "2", "-i", "-",
                        "-c:a", "pcm_s24le", str(path)], input=stereo.tobytes(), check=True)
    return path


def effect_events(edl: dict, rate: str, brand: dict | None = None, edit_dir: Path | None = None) -> list[dict[str, Any]]:
    """Every visual effect on the output timeline: (time, kind, sound, label).

    Graphic entrances, transitions, keyframed camera moves and zooms. Times are
    the visual beat (the moment the effect lands).
    """
    sys_path_helpers()
    from graphics import DEFAULT_ANIMATION, output_time
    from render import resolve_transitions, segment_timing

    events: list[dict[str, Any]] = []
    for g in edl.get("graphics") or []:
        if not isinstance(g, dict) or g.get("type") not in DEFAULT_ANIMATION:
            continue
        try:
            from graphics import Tokens, graphic_start, load_brand_kit
            start = graphic_start(g, edl, Tokens(brand or {}, Path(".")), rate)  # the sound lands when it appears
        except Exception:
            continue
        anim = g.get("animation") or DEFAULT_ANIMATION[g["type"]]
        sound = {"end_card": "riser", "logo": "shimmer"}.get(g["type"]) or ANIMATION_SOUND.get(anim, "pop")
        events.append({"time": start, "kind": f"graphic:{g['type']}", "sound": sound, "opt": g.get("sfx"),
                       "label": str(g.get("text") or g.get("title") or g["type"])[:30],
                       "words": len(str(g.get("text") or "").split()) if anim in ("typewriter", "words") else 0,
                       "duration": float(g.get("duration", 1))})
    ranges = edl.get("ranges") or []
    timing = segment_timing(ranges, rate)
    from render import outro_index
    oi = outro_index(edl, edit_dir)
    for i, spec in enumerate(resolve_transitions(edl, brand)):
        if spec and i != oi:  # an outro brings its own sound: no whoosh on its dissolve
            events.append({"time": timing[i][1], "kind": f"transition:{spec.get('type')}",
                           "sound": TRANSITION_SOUND.get(spec.get("type"), "whoosh"), "opt": spec.get("sfx"),
                           "label": f"cut {i}"})
    for i, r in enumerate(ranges):
        keys = r.get("camera") if isinstance(r.get("camera"), list) else []
        for a, b in zip(keys, keys[1:]):
            # A slow drift is invisible camera work, not an effect: only a
            # deliberate move (≥ 0.15 scale/s or a fast pan) earns a sound.
            span = max(0.05, float(b.get("t", 0)) - float(a.get("t", 0)))
            dscale = abs(float(b.get("scale", a.get("scale", 1))) - float(a.get("scale", 1)))
            dx = abs(float(b.get("x", a.get("x", 0.5))) - float(a.get("x", 0.5)))
            # Only a strong, fast move earns a sound; slow drifts and gentle
            # pushes stay silent (a whoosh on every zoom sounds cheap).
            push = float(b.get("scale", a.get("scale", 1))) > float(a.get("scale", 1))
            # a pull back to normal framing is never an effect (only a push-in or a fast pan)
            if (push and dscale / span >= 0.35) or dx / span >= 0.25:
                events.append({"time": timing[i][1] + float(a.get("t", 0)), "kind": "camera",
                               "sound": "zoom", "opt": b.get("sfx", r.get("sfx")), "label": f"range {i} move"})
        zoom = r.get("zoom")
        zoom_span = max(0.1, float((zoom or {}).get("duration") or (float(r["end"]) - float(r["start"]))) if isinstance(zoom, dict) else 1)
        if isinstance(zoom, dict) and abs(float(zoom.get("to", 1)) - float(zoom.get("from", 1))) / zoom_span >= 0.35:
            events.append({"time": timing[i][1], "kind": "zoom", "sound": "soft", "opt": zoom.get("sfx", r.get("sfx")),
                           "label": f"range {i} zoom"})
    return sorted(events, key=lambda e: e["time"])


def sys_path_helpers() -> None:
    import sys
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)


def auto_sfx(edl: dict, edit_dir: Path, rate: str, brand: dict | None = None,
             resolve_at=None) -> list[dict[str, Any]]:
    """SFX items (``start_in_output``, ``file``, ``gain_db``) for every effect
    that has no sound yet. Hand-placed SFX within 0.15 s of an effect count."""
    audio = edl.get("audio") or {}
    if audio.get("auto_sfx") in (False, "first_cut"):
        return []
    mapping = dict((brand or {}).get("sfx_map") or {})
    mapping.update(audio.get("sfx_map") or {})
    taken = [hand_time(s, resolve_at) for s in audio.get("sfx") or []]
    taken = [t for t in taken if t is not None]
    items: list[dict[str, Any]] = []
    used: dict[str, int] = {}
    for event in effect_events(edl, rate, brand, edit_dir):
        opt = event.get("opt")
        if opt is False or event["sound"] is None and not isinstance(opt, str):
            continue
        chosen_file = opt if isinstance(opt, str) and Path(opt).suffix else None  # an element may name its own file
        sound = event["sound"] if chosen_file or not isinstance(opt, str) else opt
        if any(abs(t - event["time"]) < 0.15 for t in taken):
            continue
        source = chosen_file or mapping.get(sound) or mapping.get(event["kind"].split(":")[-1])
        gain, lead = GAIN.get(sound, -16), LEAD.get(sound, 0.0)
        if chosen_file:
            known = [i for i in _library_items() if i.get("file") == str(Path(chosen_file).resolve())]
            if known and known[0].get("lufs") is not None:
                gain = ROLE_LUFS.get(sound, -28) - float(known[0]["lufs"])
            lead = 0.0
        if not source and audio.get("sfx_library", True):
            # The user's own library first, rotating so repeats don't sound canned.
            choices = library_choices(sound) or (library_choices("whoosh") if sound in ("zoom", "swoosh") else [])
            if choices:
                used[sound] = used.get(sound, -1) + 1
                pick = choices[used[sound] % min(len(choices), 3)]
                source = pick["file"]
                if pick.get("lufs") is not None:
                    gain = ROLE_LUFS.get(sound, -28) - float(pick["lufs"])
                lead = peak_offset(Path(source))  # listened: the sound lands on the event
        file = str(source) if source else str(stock_file(sound if sound in SOUNDS else "swoosh", edit_dir))
        hits = [event["time"]]
        if event.get("words", 0) > 1:  # one tick per word as it types on
            step = min(0.12, event["duration"] * 0.45 / event["words"])
            hits = [event["time"] + k * step for k in range(event["words"])]
        for hit in hits:
            items.append({"start_in_output": max(0.0, hit - lead), "file": file,
                          "gain_db": round(max(-40.0, min(12.0, gain)), 1), "auto": event["kind"], "label": event["label"]})
        taken.append(event["time"])
    return items


_PEAKS: dict[str, float] = {}


def peak_offset(path: Path) -> float:
    """Seconds from a sound's start to where it *lands* (listen.py hit point):
    a hit's onset after any leading silence, a whoosh's peak, a riser's top."""
    try:
        from listen import listen
        return min(float(listen(str(path)).get("hit_point_s", 0.0)), 4.5)
    except Exception:
        pass
    key = str(path)
    if key not in _PEAKS:
        raw = subprocess.run(["ffmpeg", "-v", "error", "-t", "3", "-i", key, "-ac", "1", "-ar", "8000", "-f", "f32le", "-"],
                             capture_output=True).stdout
        x = np.frombuffer(raw, np.float32)
        hop = 160
        if len(x) < hop:
            _PEAKS[key] = 0.0
        else:
            env = np.sqrt((x[: len(x) // hop * hop].reshape(-1, hop) ** 2).mean(1))
            _PEAKS[key] = round(float(np.argmax(env)) * hop / 8000, 3)
    return min(_PEAKS[key], 0.8)


def hand_time(item: dict, resolve_at=None) -> float | None:
    if "start_in_output" in item:
        return float(item["start_in_output"])
    if "at" in item and resolve_at is not None:
        try:
            return float(resolve_at(item["at"]))
        except Exception:
            return None
    return None


def silent_effects(edl: dict, rate: str, brand: dict | None = None, resolve_at=None) -> list[dict[str, Any]]:
    """Effects with no sound within 0.15 s — for QC (empty when auto SFX is on)."""
    audio = edl.get("audio") or {}
    taken = [hand_time(s, resolve_at) for s in audio.get("sfx") or []]
    taken = [t for t in taken if t is not None]
    if audio.get("auto_sfx") is not False:  # on, or a first cut (sound comes later)
        return []
    return [e for e in effect_events(edl, rate, brand)
            if e.get("opt") is not False and e["sound"] and not any(abs(t - e["time"]) < 0.15 for t in taken)]
