"""Build the edit inside Premiere Pro, the way an editor would.

Drives a running Premiere through the Video Use Bridge panel
(``premiere/video-use-bridge``, a CEP panel listening on 127.0.0.1:7788)::

    python helpers/premiere.py install            # copy the panel into Premiere's extensions folder
    python helpers/premiere.py ping               # is Premiere + the panel up?
    python helpers/premiere.py learn "Master Seq" --edit edit/   # read your own sequence → house style
    python helpers/premiere.py build edit/edl.json [--project out.prproj] [--plan-only]

``learn`` reads a sequence you cut by hand and records *how* you work, into
the project's brand kit (``premiere_house``): where the zoom lives (an
Adjustment Layer with a Transform effect, or Motion on the clip), every MOGRT
you used (its project item, text fields and sample text, track, typical
length), and which audio tracks hold SFX and music. ``build`` then follows it:
zoom keys on Adjustment Layers with Transform, and graphics as copies of your
own MOGRTs with the new text (map styles → templates in
``premiere_house.templates``, e.g. ``{"bubble": "Pill Blue", "headline": "Title 3D"}``).

``build`` makes one sequence named after the project:

    V1  the source clips, cut at every edit; zoom = Motion Scale keyframes
        around the face (anchor point), rotation for sideways camera files
    V2  light-flash overlays at B-roll cuts        V3  layers behind the subject
    V4  subject cut-out (person matte)             V5  graphics — a MOGRT with
        editable text when the brand kit maps the style to one, else the
        rendered alpha clip
    Captions track from the edit's .srt (editable in Premiere)
    A1  dialogue from the right source (B-roll keeps the interview audio)
    A2+ one clip per sound effect at its level, spread so none overlap
    Cross Dissolve / Dip to Black where the EDL has dissolves / fades

Needs one prior render (``render_manifest.json``) for graphics, the person
matte and the SFX list. Loudness normalisation and the dialogue chain stay a
render-side step; set them in Premiere's export or Essential Sound.

A brand kit maps a text style to a MOGRT like this::

    "text_styles": {"bubble": {..., "mogrt": {"file": "/path/Bubble.mogrt", "params": {"Text": "{text}"}}}}
"""

from __future__ import annotations

import argparse
import json
import subprocess
import shutil
import sys
import urllib.request
from fractions import Fraction
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

PORT = 7788
PANEL = Path(__file__).resolve().parents[1] / "premiere" / "video-use-bridge"
CEP_DIR = Path.home() / "Library" / "Application Support" / "Adobe" / "CEP" / "extensions"


class Bridge:
    def __init__(self, port: int = PORT, dry: bool = False):
        self.url, self.dry, self.log = f"http://127.0.0.1:{port}", dry, []

    def ping(self) -> dict:
        with urllib.request.urlopen(self.url + "/ping", timeout=3) as r:
            return json.loads(r.read())

    sequence: str | None = None  # every call after the sequence exists names it explicitly

    fps: float | None = None  # set once the sequence is known: times snap to its frame grid

    def __call__(self, fn: str, **args: Any) -> dict:
        if self.fps:  # Premiere snaps every edit to a frame; send frame times so look-ups match
            for key in ("start", "fromStart"):
                if isinstance(args.get(key), (int, float)) and not (key == "fromStart" and args.get("fromSequence") != self.sequence):
                    args[key] = round(round(args[key] * self.fps) / self.fps, 6)
        if self.sequence and "sequence" not in args and fn not in ("importFiles", "newSequence", "sequences", "ping", "save"):
            args["sequence"] = self.sequence
        self.log.append({"fn": fn, "args": args})
        if self.dry:
            return {"ok": True, "dry": True}
        req = urllib.request.Request(self.url, data=json.dumps({"fn": fn, "args": args}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as r:
            out = json.loads(r.read() or b"{}", strict=False)  # MOGRT text may carry raw line breaks
        if out.get("error"):
            raise RuntimeError(f"Premiere: {fn}: {out['error']}")
        return out


def install() -> Path:
    dest = CEP_DIR / "video-use-bridge"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(PANEL, dest)
    return dest


def camera_keys(r: dict) -> list[dict]:
    """The range's zoom as (t, scale, ease) keys, relative to the cover framing."""
    if isinstance(r.get("camera"), list) and r["camera"]:
        keys = [{"t": float(k.get("t", 0)), "scale": float(k.get("scale", 1)), "ease": k.get("ease", "smooth")} for k in r["camera"]]
    elif isinstance(r.get("zoom"), dict):
        z = r["zoom"]
        dur = float(r["end"]) - float(r["start"])
        a = float(z.get("start", 0))
        b = min(dur, a + float(z.get("duration", dur - a)))
        keys = [{"t": 0, "scale": float(z.get("from", 1))}, {"t": a, "scale": float(z.get("from", 1))},
                {"t": b, "scale": float(z.get("to", 1)), "ease": z.get("ease", "smooth")}]
    elif isinstance(r.get("punch_in"), dict):
        keys = [{"t": 0, "scale": float(r["punch_in"].get("scale", 1))}]
    else:
        keys = [{"t": 0, "scale": 1.0}]
    out: list[dict] = []
    for k in sorted(keys, key=lambda k: k["t"]):
        if out and abs(out[-1]["t"] - k["t"]) < 1e-3:
            out[-1] = k
        else:
            out.append(k)
    return out


def light_overlay(color: str, rate: str, out: Path, canvas: tuple[int, int], duration_ms: float = 470) -> tuple[Path, int]:
    """The brand light flash as its own alpha clip (peaks on frame ``pre``)."""
    import subprocess
    import numpy as np
    from render import light_curve
    fps = float(Fraction(rate))
    total = max(5, int(round(duration_ms / 1000 * fps)))
    pre = int(round(total * 0.43))
    curve = light_curve(pre, total - 1 - pre)
    if not out.exists():
        w, h = canvas
        rgb = np.array([int(color.lstrip("#")[k:k + 2], 16) for k in (0, 2, 4)], np.uint8)
        fall = (0.75 + 0.25 * np.linspace(0, 1, h, dtype=np.float32))[:, None]
        proc = subprocess.Popen(["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{w}x{h}", "-r", rate,
                                 "-i", "-", "-c:v", "png", "-pix_fmt", "rgba", str(out)], stdin=subprocess.PIPE)
        for k, s in enumerate(curve):
            tint = rgb if k != pre else (rgb + (255 - rgb.astype(int)) * 0.35).astype(np.uint8)
            frame = np.empty((h, w, 4), np.uint8)
            frame[..., :3] = tint
            frame[..., 3] = np.clip(255 * s * 0.8 * fall, 0, 255).astype(np.uint8)
            proc.stdin.write(frame.tobytes())
        proc.stdin.close()
        proc.wait()
    return out, pre


def spread(items: list[tuple[float, float]]) -> list[int]:
    """Lane per (start, end) so no two overlapping clips share a track."""
    ends: list[float] = []
    lanes = []
    for start, end in items:
        for k, e in enumerate(ends):
            if e <= start + 1e-3:
                ends[k] = end
                lanes.append(k)
                break
        else:
            ends.append(end)
            lanes.append(len(ends) - 1)
    return lanes


def _text_param(params: list[dict]) -> str | None:
    """The MOGRT field that holds the words (JSON textEditValue or plain text)."""
    for p in params or []:  # the field whose value is the text JSON wins
        if isinstance(p.get("value"), str) and "textEditValue" in p["value"]:
            return p["name"]
    for p in params or []:
        v = p.get("value")
        if isinstance(v, str) and v and not v.replace(".", "").isdigit() and len(v) < 200 and ";" not in v:
            return p["name"]
    return None


def _text_of(params: list[dict], name: str | None) -> str | None:
    for p in params or []:
        if p["name"] == name:
            v = p.get("value")
            if isinstance(v, str) and "textEditValue" in v:
                try:
                    return json.loads(v).get("textEditValue")
                except json.JSONDecodeError:
                    continue
            if isinstance(v, str) and ";" not in v:
                return v
    return None


def learn(sequence: str, bridge: Bridge) -> dict:
    """Read a hand-cut sequence and describe the editor's working method."""
    return learn_from(bridge("inspect", sequence=sequence, props=True))


def learn_from(seq: dict) -> dict:
    """The editor's working method, from one inspected sequence.

    - zoom: where it lives (Adjustment Layer + Transform, or Motion on the
      clip), its usual peak scale, ramp time and the position lift that keeps
      the face in frame
    - text: per track, the entrance keyframes (e.g. blur 15→0, rise, fade in
      over 0.33 s) and drop-shadow settings applied to text graphics
    - mogrts: every template used (project item, text field, examples)
    - light leaks / transitions placed as graphics
    - sfx_favourites: the sound files used most, by role
    """
    from collections import Counter, defaultdict
    house: dict[str, Any] = {"learned_from": seq["name"], "canvas": [seq.get("width"), seq.get("height")], "fps": seq.get("fps")}

    def rel_keys(clip: dict, keys: list[dict]) -> list[dict]:
        return [{"t": round(k["t"] - clip["inPoint"], 3), "v": k["v"]} for k in keys]

    # ---- zoom
    zoom_tracks: Counter = Counter()
    peaks: Counter = Counter()
    ramps: Counter = Counter()
    lifts: Counter = Counter()
    adj_item = None
    motion_zoom = 0
    for track in seq["video"]:
        for clip in track["clips"]:
            adjustment = (clip.get("item") or {}).get("adjustment")
            for comp in clip.get("components") or []:
                props = {p["name"]: p for p in comp["props"]}
                scale = props.get("Scale Height") or props.get("Scale")
                if not scale or not scale.get("keys"):
                    continue
                if comp["name"] == "Motion" and not adjustment:
                    motion_zoom += 1
                    continue
                if comp["name"] == "Transform" and adjustment:
                    zoom_tracks[track["index"]] += 1
                    zoom_exemplar = locals().get("zoom_exemplar") or {"sequence": seq["name"], "track": track["index"], "start": clip["start"]}
                    adj_item = adj_item or (clip.get("item") or {}).get("nodeId")
                    ks = scale["keys"]
                    peaks[round(max(k["v"] for k in ks))] += 1
                    if len(ks) > 1:
                        ramps[round(ks[1]["t"] - ks[0]["t"], 2)] += 1
                    pos = props.get("Position")
                    if pos and pos.get("keys"):
                        lifts[round(min(k["v"][1] for k in pos["keys"]), 3)] += 1
    if zoom_tracks:
        house["zoom_exemplar"] = locals().get("zoom_exemplar")
        house["zoom"] = {"method": "adjustment_transform", "track": zoom_tracks.most_common(1)[0][0], "effect": "Transform",
                         "adjustment_item": adj_item, "peak_scale": peaks.most_common(1)[0][0],
                         "peak_scales": [p for p, _ in peaks.most_common(3)],
                         "ramp_s": ramps.most_common(1)[0][0] if ramps else 0.8,
                         "position_y": lifts.most_common(2)[-1][0] if lifts else 0.5, "uses": sum(zoom_tracks.values())}
    else:
        house["zoom"] = {"method": "clip_motion", "uses": motion_zoom}

    # ---- text graphics: entrance animation + shadow, per track
    text_tracks: dict[int, dict] = {}
    templates: dict[str, dict] = {}
    for track in seq["video"]:
        for clip in track["clips"]:
            if not clip.get("mgt"):
                continue
            item = clip.get("item") or {}
            comps = {c["name"]: c for c in clip.get("components") or []}
            if "Text" in comps:  # a native Premiere text graphic
                t = text_tracks.setdefault(track["index"], {"track": track["index"], "count": 0, "durations": [],
                                                            "exemplar": {"sequence": seq["name"], "track": track["index"], "start": clip["start"]}})
                t["count"] += 1
                t["durations"].append(round(clip["end"] - clip["start"], 2))
                if "entrance" not in t:
                    anim = []
                    for comp in clip.get("components") or []:
                        for p in comp["props"]:
                            if p.get("keys") and comp["name"] not in ("Text", "Motion"):
                                anim.append({"component": comp["name"], "prop": p["name"], "keys": rel_keys(clip, p["keys"])})
                    if anim:
                        t["entrance"] = anim
                if "shadow" not in t and "Drop Shadow" in comps:
                    t["shadow"] = {p["name"]: p.get("value") for p in comps["Drop Shadow"]["props"]}
                continue
            name = item.get("name") or clip["name"]
            field = _text_param(clip.get("mgtParams"))
            mpos = next((p.get("value") for p in clip.get("mgtParams") or [] if p["name"] == "Position"), None)
            entry = templates.setdefault(name, {"nodeId": item.get("nodeId"), "track": track["index"], "text_param": field,
                                                "position": mpos if isinstance(mpos, list) else (json.loads(mpos) if isinstance(mpos, str) and mpos.startswith("[") else None),
                                                "exemplar": {"sequence": seq["name"], "track": track["index"], "start": clip["start"]},
                                                "params": [p["name"] for p in clip.get("mgtParams") or []],
                                                "examples": [], "durations": []})
            text = _text_of(clip.get("mgtParams"), field)
            if text and len(entry["examples"]) < 4 and not text.count("-") >= 4:
                entry["examples"].append(text)
            entry["durations"].append(round(clip["end"] - clip["start"], 2))
    for group in list(text_tracks.values()) + list(templates.values()):
        d = sorted(group.pop("durations"))
        group["typical_duration"] = d[len(d) // 2] if d else None
        group["uses"] = len(d)
    house["text_tracks"] = sorted(text_tracks.values(), key=lambda t: -t["uses"])
    # Subtitles: a track of many short native text graphics (his method: the
    # SRT as a caption track → Upgrade to Graphic → paste one clip's attributes
    # — entrance, shadow, Motion position — onto all, keeping each one's text).
    subs = [t for t in text_tracks.values() if t["uses"] >= 8]
    if subs:
        t = max(subs, key=lambda t: t["uses"])
        house["captions"] = {"track": t["track"], "exemplar": t["exemplar"], "entrance": t.get("entrance"),
                             "shadow": t.get("shadow"), "skip": ["Text"], "uses": t["uses"]}
    house["mogrts"] = templates
    leak = next(((n, t) for n, t in templates.items() if "light leak" in n.lower() or "leak" in n.lower()), None)
    if leak:
        house["light_leak"] = {"template": leak[0], "nodeId": leak[1]["nodeId"], "typical_duration": leak[1]["typical_duration"],
                               "exemplar": leak[1]["exemplar"]}

    # ---- audio: track roles + favourite sound effects
    audio, favourites = [], Counter()
    for track in seq["audio"]:
        clips = track["clips"]
        if not clips:
            continue
        lengths = sorted(c["end"] - c["start"] for c in clips)
        median = lengths[len(lengths) // 2]
        names = Counter((c.get("item") or {}).get("name") for c in clips)
        kind = "music" if median > 20 else "dialogue" if track["index"] == 0 or median > 2.5 else "sfx"
        if kind == "sfx":
            favourites.update(names)
        audio.append({"track": track["index"], "kind": kind, "clips": len(clips), "median_s": round(median, 2)})
    house["audio_tracks"] = audio
    try:
        from sfx_library import classify
        by_role: dict[str, list] = defaultdict(list)
        for name, count in favourites.most_common():
            role = classify(Path(str(name)).stem) if name else None
            if role and name:
                by_role[role].append({"name": name, "uses": count})
        house["sfx_favourites"] = dict(by_role)
    except Exception:
        pass
    house["video_tracks"] = [{"track": t["index"], "clips": len(t["clips"]),
                              "adjustment": sum(1 for c in t["clips"] if (c.get("item") or {}).get("adjustment")),
                              "graphics": sum(1 for c in t["clips"] if c.get("mgt"))} for t in seq["video"] if t["clips"]]
    return house


def house_text_lead(brand: dict) -> float:
    """Seconds from a text graphic's first frame until the editor's entrance
    makes it readable (its opacity ramp reaching ~60%)."""
    house = brand.get("premiere_house") or {}
    ramp = 0.0
    for t in house.get("text_tracks") or []:
        for anim in t.get("entrance") or []:
            if anim["prop"] == "Opacity" and len(anim["keys"]) >= 2:
                ramp = max(ramp, float(anim["keys"][-1]["t"]) - float(anim["keys"][0]["t"]))
    ramp = max(ramp, float(house.get("text_fade_s", 0.1)))
    return round(0.6 * ramp, 3) if ramp else 0.15


def text_companions(seq: dict, layers: list[dict]) -> list[dict]:
    """The sounds that come with a text graphic in the editor's sequence:
    audio clips starting just before / with its first layer, with their
    offset, trim and a location to paste their volume from."""
    if not layers:
        return []
    base = min(float(l["exemplar"]["start"]) for l in layers)
    out = []
    for track in seq["audio"]:
        for clip in track["clips"]:
            off = clip["start"] - base
            if -0.5 <= off <= 0.8 and (clip.get("item") or {}).get("path"):
                out.append({"file": clip["item"]["path"], "offset": round(off, 3), "in": round(clip.get("inPoint", 0), 3),
                            "duration": round(clip["end"] - clip["start"], 3),
                            "exemplar": {"sequence": seq["name"], "track": track["index"], "start": clip["start"]}})
    return sorted(out, key=lambda c: c["offset"])


def favourite_sfx_map(house: dict) -> dict[str, str]:
    """Role → the editor's most-used file for it, resolved in the material index."""
    try:
        from library import load
        items = [i for i in load().get("items", {}).values() if i.get("type") == "sfx"]
    except Exception:
        return {}
    out = {}
    for role, picks in (house.get("sfx_favourites") or {}).items():
        for pick in picks:
            stem = Path(pick["name"]).stem.lower()
            match = next((i for i in items if i.get("name", "").lower() == stem), None)
            if match:
                out[role] = match["file"]
                break
    return out

SECTIONS = ("picture", "broll", "zoom", "light", "behind", "graphics", "sfx", "captions", "caption_fx")


def build(edl_path: Path, bridge: Bridge, project: Path | None = None, only: set[str] | None = None) -> dict:
    from export_timeline import _cutout, _flatten, _resolve, probe
    from face_anchor import range_face_centers
    from graphics import load_brand_kit
    from render import resolve_canvas, resolve_output_rate, resolve_transitions, segment_timing
    from text_styles import resolve_style
    from graphics import Tokens

    edl_path = edl_path.resolve()
    base_dir = edl_path.parent
    edl = json.loads(edl_path.read_text(encoding="utf-8"))
    manifest_path = base_dir / "render_manifest.json"
    m = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else None
    if m is None:
        needs_assets = any(edl.get(k) for k in ("graphics", "overlays", "behind_subject_overlays", "beats")) or \
            any(r.get("transition") for r in edl.get("ranges") or [])
        if needs_assets:
            raise SystemExit("make the assets first: premiere.py build <edl> --prepare (graphics, matte and SFX list)")
        # A plain cut (picture, sound, captions): nothing to pre-render.
        m = {"canvas": list(resolve_canvas(edl, base_dir)), "rate": resolve_output_rate(edl, base_dir, None),
             "overlays": [], "behind_subject_overlays": [], "subject_mask": None, "base": None, "sfx": []}
    canvas = tuple(m["canvas"]) if m else resolve_canvas(edl, base_dir)
    rate = m["rate"] if m else resolve_output_rate(edl, base_dir, None)
    fps = float(Fraction(rate))
    title = (base_dir.parent.name if base_dir.name == "edit" else base_dir.name) + " (video-use)"
    media = base_dir / "premiere_media"
    media.mkdir(exist_ok=True)
    brand = load_brand_kit(edl, base_dir)
    tokens = Tokens(brand, base_dir)
    ranges = edl["ranges"]
    timing = segment_timing(ranges, rate)
    rotations = edl.get("source_rotations") or {}
    faces = range_face_centers(edl, base_dir, _resolve)

    # ---- assets
    sources = {name: _resolve(path, base_dir) for name, path in edl["sources"].items()}
    lights, behind, cutouts, graphics = [], [], [], []
    for k, spec in enumerate(resolve_transitions(edl, brand)):
        if spec and spec.get("type") == "light":
            color = spec.get("color") or ((brand.get("transitions") or {}).get("light") or {}).get("color") or "#FFB46E"
            clip, pre = light_overlay(color, rate, media / f"light_{color.lstrip('#')}.mov", canvas, float(spec.get("duration_ms", 470)))
            lights.append((clip, timing[k][1] - pre / fps, (pre + 1 + (int(round(float(spec.get('duration_ms', 470)) / 1000 * fps)) - 1 - pre)) / fps,
                           timing[k][1]))
    for k, o in enumerate(m["behind_subject_overlays"]):
        src = _resolve(o["file"], base_dir)
        flat = _flatten(src, media / f"behind_{k:02d}_{src.stem[:24]}.mov", canvas, int(o.get("x", 0)), int(o.get("y", 0)), rate)
        behind.append((flat, float(o["start_in_output"]), float(o["duration"]), o.get("text") or src.stem))
    if m.get("subject_mask") and behind:
        spans = sorted((s, s + d) for _f, s, d, _t in behind)
        merged: list[list[float]] = []
        for a, b in spans:
            if merged and a <= merged[-1][1] + 0.05:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        for k, (a, b) in enumerate(merged):
            cut = _cutout(_resolve(m["base"], base_dir), Path(m["subject_mask"]), a, b - a, media / f"subject_cutout_{k:02d}.mov", rate)
            cutouts.append((cut, a, b - a))
    gfx_elements = [g for g in (edl.get("graphics") or []) if not g.get("behind_subject")]
    rendered = [o for o in m["overlays"] if Path(o["file"]).name != "captions.mov"]
    for k, o in enumerate(rendered):
        element = next((g for g in gfx_elements if (g.get("covers") or g.get("text") or g.get("title") or "") == (o.get("text") or "")
                        and g.get("type") == o.get("type")), None)
        compare = (brand.get("premiere_house") or {}).get("compare_graphic") or {}
        side = compare.get(element.get("compare")) if element else None
        if side:
            # His compare / count graphic: that side's AE comp (one copy per word),
            # his settings from the exemplar, and that side's own sound.
            sfx_dir = Path("/Users/m/Desktop/material/sound effect")
            sound_path = None
            if side.get("sfx"):
                cand = sfx_dir / side["sfx"]
                sound_path = cand if cand.exists() else next(sfx_dir.glob(side["sfx"] + "*"), None)
            graphics.append({"layers": [{"nodeId": None, "aep": side["aep"], "comp": side["comp"], "exemplar": side["exemplar"],
                                         "offset": 0.0, "name": side["comp"], "lane": 0 if element.get("compare") == "right" else 1}],
                             "start": float(o["start_in_output"]), "duration": float(o["duration"]),
                             "text": str(element.get("text", "")).replace("\n", " "),
                             "companions": [{"file": str(sound_path), "offset": 0.0, "in": 0.0,
                                             "duration": float(probe(sound_path).get("format", {}).get("duration", 1.0) or 1.0),
                                             "exemplar": None}] if sound_path else []})
            continue
        text_layers = (brand.get("premiere_house") or {}).get("text_layers")
        # Only the text shape the editor's layers are (by default his bubble);
        # other client shapes (streak, outline, big text) stay rendered.
        layer_styles = (brand.get("premiere_house") or {}).get("text_layers_styles") or ["bubble"]
        if element and text_layers and element.get("type") in ("headline", "sticker", "title_card", "lower_third") \
                and (element.get("text_style") in layer_styles or not element.get("text_style")):
            # The editor's own text graphic: their layers copied with every
            # setting; the words go in a marker for them to type in Source Text.
            # Timed so the text is *readable* on its word with their entrance.
            start_g = float(o["start_in_output"])
            if o.get("word_time") is not None:
                text_layer_off = max(float(l.get("offset", 0)) for l in text_layers)
                start_g = max(0.0, float(o["word_time"]) - house_text_lead(brand) - text_layer_off)
            end_g = float(o["start_in_output"]) + float(o["duration"])
            graphics.append({"layers": text_layers, "start": start_g, "duration": end_g - start_g,
                             "text": str(element.get("text", "")).replace("\n", " ")})
            continue
        recipe = resolve_style(str(element["text_style"]), tokens) if element and element.get("text_style") else None
        mg = (recipe or {}).get("mogrt")
        house_templates = (brand.get("premiere_house") or {}).get("templates") or {}
        house_mogrts = (brand.get("premiere_house") or {}).get("mogrts") or {}
        style_key = (element or {}).get("text_style") or (element or {}).get("type")
        tname = house_templates.get(style_key) or house_templates.get((element or {}).get("type") or "")
        if element and tname and tname in house_mogrts:
            t = house_mogrts[tname]
            cx = (float(o.get("x", 0)) + float(o.get("w", 0)) / 2) / canvas[0]
            cy = (float(o.get("y", 0)) + float(o.get("h", 0)) / 2) / canvas[1]
            tp = t.get("position") or [0.5, 0.5]
            graphics.append({"copy": t["nodeId"], "start": float(o["start_in_output"]), "duration": float(o["duration"]),
                             "params": {t["text_param"]: str(element.get("text", "")).replace("\n", "\r")} if t.get("text_param") else {},
                             "name": tname, "position": [round(0.5 + cx - tp[0], 4), round(0.5 + cy - tp[1], 4)],
                             "paste": (brand.get("premiere_house") or {}).get("text_attributes_from")})
        elif mg and Path(mg.get("file", "")).exists():
            params = {name: str(v).replace("{text}", str(element.get("text", "")).replace("\n", "\r"))
                      for name, v in (mg.get("params") or {"Text": "{text}"}).items()}
            graphics.append({"mogrt": mg["file"], "start": float(o["start_in_output"]), "duration": float(o["duration"]), "params": params})
        else:
            src = _resolve(o["file"], base_dir)
            flat = _flatten(src, media / f"gfx_{k:02d}_{src.stem[:24]}.mov", canvas, int(o.get("x", 0)), int(o.get("y", 0)), rate)
            graphics.append({"file": flat, "start": float(o["start_in_output"]), "duration": float(o["duration"]),
                             "name": str(o.get("text") or o.get("type"))[:40]})
    srt = base_dir / "export" / f"{title.replace(' (video-use)', '')}.srt"
    srt.parent.mkdir(exist_ok=True)
    from export_timeline import write_srt
    write_srt(edl, base_dir, srt)
    sfx = [s for s in m["sfx"] if _resolve(s["file"], base_dir).exists()]
    if brand.get("sfx_map"):  # re-pick with the editor's favourite sounds
        from graphics import output_time
        from sfx_library import auto_sfx
        hand = []
        for item in (edl.get("audio") or {}).get("sfx") or []:
            t0 = float(item["start_in_output"]) if "start_in_output" in item else output_time(edl, item["at"], rate)
            hand.append({"file": str(_resolve(item["file"], base_dir)), "start_in_output": t0, "gain_db": float(item.get("gain_db", -6)),
                         "label": Path(str(item["file"])).stem})
        if edl.get("beats"):
            from beats import expand
            for item in expand(edl, base_dir, canvas, rate, brand)[1]:
                hand.append({"file": item["file"], "start_in_output": item["start_in_output"], "gain_db": item["gain_db"], "label": "denial"})
        picked = auto_sfx(edl, base_dir, rate, brand, resolve_at=lambda at: output_time(edl, at, rate))
        own = {str(g.get("text", "")).replace("\n", " ")[:30] for g in gfx_elements} if (brand.get("premiere_house") or {}).get("text_sfx") else set()
        sfx = hand + [{"file": i["file"], "start_in_output": i["start_in_output"], "gain_db": i["gain_db"], "label": i.get("auto")}
                      for i in picked if not (str(i.get("auto", "")).startswith("graphic:") and i.get("label", "")[:30].replace("\n", " ") in own)]
        levels = (brand.get("audio") or {}).get("levels_peak_db") or {}
        if levels.get("sfx") is not None:  # the editor's sound design: every effect peaks at ~-9 dB
            from audio_mix import level_to_peak
            sfx = level_to_peak(sfx, base_dir, float(levels["sfx"]))

    imports = sorted({str(p) for p in sources.values()} | {str(c) for c, *_ in lights} | {str(f) for f, *_ in behind}
                     | {str(c) for c, *_ in cutouts} | {str(g["file"]) for g in graphics if "file" in g}
                     | {str(_resolve(s["file"], base_dir)) for s in sfx} | {str(srt)}
                     | {str(c["file"]) for c in (brand.get("premiere_house") or {}).get("text_sfx") or []})

    # ---- drive Premiere
    if project and not bridge.dry:
        bridge("newProject", path=str(project)) if not project.exists() else bridge("openProject", path=str(project))
    bin_name = title
    bridge("importFiles", paths=imports, bin=bin_name)
    def want(section: str) -> bool:
        return not only or section in only
    bridge.sequence = None
    if not only:  # a full build starts a clean sequence; --only updates the existing one in place
        bridge("newSequence", name=title, **{"from": str(sources[ranges[0]["source"]])}, bin=bin_name,
               width=canvas[0], height=canvas[1], fps=fps, replace=True)
    bridge.sequence = title
    bridge.fps = fps
    sfx_lanes = spread([(float(s["start_in_output"]), float(s["start_in_output"]) + float(probe(_resolve(s["file"], base_dir))
                         .get("format", {}).get("duration", 0.5) or 0.5)) for s in sfx])
    house_zoom = (brand.get("premiere_house") or {}).get("zoom") or {}
    video_tracks = max(5, int(house_zoom.get("track", 0)) + 1 if house_zoom.get("method") == "adjustment_transform" else 5)
    behind_lanes = spread([(st, st + d) for _f, st, d, _t in behind])
    gfx_lanes = spread([(g["start"], g["start"] + g["duration"]) for g in graphics])
    n_behind = 1 + max(behind_lanes + [0])
    n_gfx = 1 + max(gfx_lanes + [0])
    # V2 B-roll laid over the editor's own V1 (when the picture isn't rebuilt),
    # then behind-subject text, the cut-out, graphics; the light flash above
    # all of them (else a B-roll or a card hides it) and the zoom on top.
    T_BROLL, T_BEHIND = 1, 2
    T_CUT = T_BEHIND + n_behind
    T_GFX = T_CUT + 1
    n_gfx += len((brand.get("premiere_house") or {}).get("text_layers") or [])
    T_LIGHT = T_GFX + n_gfx
    if house_zoom.get("method") == "adjustment_transform":
        house_zoom = dict(house_zoom, track=max(int(house_zoom.get("track", 0)), T_LIGHT + 1))
    video_tracks = max(video_tracks, T_LIGHT + 2, int(house_zoom.get("track", 0)) + 1)
    n_comp_lanes = 0
    _comps = (brand.get("premiere_house") or {}).get("text_sfx") or []
    if _comps and (brand.get("premiere_house") or {}).get("text_layers"):
        _texts = [g for g in graphics if "layers" in g]
        n_comp_lanes = 1 + max(spread([(g["start"] + c["offset"], g["start"] + c["offset"] + c["duration"])
                                       for g in _texts for c in g.get("companions", _comps)]) + [0]) if _texts else 0
    bridge("ensureTracks", video=video_tracks, audio=1 + max(sfx_lanes + [0]) + 1 + n_comp_lanes)
    from render import outro_index
    oi = outro_index(edl, base_dir)
    outro_start = None  # nothing of ours may sit under the outro: it covers everything
    if oi is not None:
        lean = float(((ranges[oi].get("transition") or {}).get("duration_ms") or 0)) / 1000 or float(
            ((brand.get("premiere_house") or {}).get("outro") or {}).get("overlap_s", 0.33))
        outro_start = max(0.0, timing[oi][1] - lean)
    if only:  # clear just the tracks of the parts being updated; everything else (and hand edits) stays
        n_layers = len((brand.get("premiere_house") or {}).get("text_layers") or [])
        clear = {"picture": [("video", 0), ("audio", 0)], "light": [("video", T_LIGHT)], "broll": [("video", T_BROLL)],
                 "behind": [("video", T_BEHIND + i) for i in range(n_behind)] + [("video", T_CUT)],
                 "graphics": [("video", T_GFX + i) for i in range(n_gfx)]
                             + [("audio", 1 + max(sfx_lanes + [0]) + 1 + i) for i in range(n_comp_lanes)],
                 "sfx": [("audio", 1 + i) for i in range(1 + max(sfx_lanes + [0]))],
                 "zoom": [("video", int(house_zoom.get("track", 0)))] if house_zoom.get("method") == "adjustment_transform" else []}
        for section in only:
            for kind, idx in clear.get(section, []):
                # only up to the outro: an outro the editor placed by hand stays
                bridge("clearTrack", kind=kind, track=idx, **({"to": outro_start - 0.01} if outro_start else {}))

    infos = {name: probe(path) for name, path in sources.items()}
    # Sound first (each overwrite then loses its dragged-along picture), then picture.
    from render import boundary_audio_shifts
    split_shifts = boundary_audio_shifts(ranges)
    for k, (r, (_n, start, dur)) in enumerate(zip(ranges, timing)):
        if not want("picture"):
            break
        if k == oi:
            continue  # the outro goes on the top track, over everything (below)
        sound = r.get("audio_source") if isinstance(r.get("audio_source"), dict) else None
        a_src = sources[sound["source"]] if sound else sources[r["source"]]
        a_in = float(sound.get("start", r["start"])) if sound else float(r["start"])
        if any(st.get("codec_type") == "audio" for st in probe(a_src).get("streams", [])):
            # J/L cuts: the sound's edges move against the picture (j_cut_ms / l_cut_ms)
            s_in, s_out = split_shifts[k], split_shifts[k + 1]
            s_in = max(s_in, -a_in)
            bridge("place", path=str(a_src), kind="audio", track=0, start=start + s_in,
                   **{"in": a_in + s_in, "out": a_in + dur + s_out})
            if r.get("audio_gain_db"):
                bridge("volume", track=0, start=start, db=float(r["audio_gain_db"]))
    for k, (r, (_n, start, dur)) in enumerate(zip(ranges, timing)):
        if k == oi:
            continue
        name = r["source"]
        src = sources[name]
        own_sound = not isinstance(r.get("audio_source"), dict)  # the picture's own audio is the dialogue: keep it linked
        if split_shifts[k] or split_shifts[k + 1]:
            own_sound = False  # a J/L cut: its sound was laid on its own, offset from the picture
        if want("picture"):
            bridge("place", path=str(src), kind="video", track=0, start=start, **{"in": float(r["start"]), "out": float(r["start"]) + dur},
                   name=f"{k + 1:02d} {r.get('beat') or name}", keepAudio=own_sound)
        video = next((st for st in infos[name].get("streams", []) if st.get("codec_type") == "video"), {})
        sw, sh = int(video.get("width") or canvas[0]), int(video.get("height") or canvas[1])
        rot = rotations.get(name)
        dw, dh = (sh, sw) if rot in ("cw", "ccw") else (sw, sh)
        cover = max(canvas[0] / dw, canvas[1] / dh) * 100
        keys = [{"t": kk["t"], "scale": round(cover * kk["scale"], 3), "ease": kk.get("ease")} for kk in camera_keys(r)]
        motion: dict[str, Any] = {"track": 0, "start": start, "keys": keys}
        if rot:
            motion["rotation"] = 90 if rot == "cw" else -90
        face = faces[k] if k < len(faces) else None
        if face and any(kk["scale"] > cover * 1.001 for kk in keys):
            fx, fy = face  # displayed, normalized
            raw = (fy, 1 - fx) if rot == "cw" else (1 - fy, fx) if rot == "ccw" else (fx, fy)
            px = 0.5 + (fx - 0.5) * dw * cover / 100 / canvas[0]
            py = 0.5 + (fy - 0.5) * dh * cover / 100 / canvas[1]
            motion.update(anchor=[raw[0], raw[1]], position=[px, py])  # zoom pivots on the face
        zoom_house = house_zoom
        house = brand.get("premiere_house") or {}
        moves = len({kk["scale"] for kk in keys}) > 1 or any(kk["scale"] > cover * 1.001 for kk in keys)
        if zoom_house.get("method") == "adjustment_transform" and zoom_house.get("adjustment_item"):
            # The editor's way: picture fitted on V1, the zoom on an Adjustment
            # Layer above it with a Transform effect.
            if want("picture"):
                bridge("motion", track=0, start=start, keys=[{"t": 0, "scale": cover}], **({"rotation": motion["rotation"]} if rot else {}))
            if moves and want("zoom"):
                adj_track = int(zoom_house.get("track", 5))
                if outro_start is not None and k != oi and start + dur > outro_start:
                    dur = max(1 / fps, outro_start - start)
                bridge("placeItem", nodeId=zoom_house["adjustment_item"], track=adj_track, start=start, duration=dur, name="zoom")
                ex = house.get("zoom_exemplar")
                if ex:  # paste the editor's own Transform (shutter, settings), then our keys
                    bridge("copyAttributes", fromSequence=ex["sequence"], fromTrack=ex["track"], fromStart=ex["start"],
                           track=adj_track, start=start, noKeys=True)
                else:
                    bridge("addEffect", track=adj_track, start=start, name=zoom_house.get("effect") or "Transform")
                # Easy ease on every key (in and out), as the editor keys his zooms.
                zkeys = [{"t": kk["t"], "v": round(kk["scale"] / cover * 100, 3), "ease": "smooth"} for kk in keys]
                fx_name = zoom_house.get("effect") or "Transform"
                # Uniform Scale on first: Premiere then shows one "Scale" that
                # grows height and width together; key that one.
                bridge("setProps", track=adj_track, start=start, component=fx_name, props={"Uniform Scale": {"value": True}})
                props = {"Scale": {"keys": zkeys}}
                if face:  # pivot on the face: it stays put while the frame grows around it
                    props["Anchor Point"] = {"value": [face[0], face[1]]}
                    props["Position"] = {"value": [face[0], face[1]]}
                done = bridge("setProps", track=adj_track, start=start, component=fx_name, props=props).get("set") or []
                if not bridge.dry and "Scale" not in done:  # older Transform: both axes by name
                    bridge("setProps", track=adj_track, start=start, component=fx_name,
                           props={"Scale Height": {"keys": zkeys}, "Scale Width": {"keys": zkeys}})
        elif want("picture") or want("zoom"):
            bridge("motion", **motion)

    if only and "broll" in only and "picture" not in only:
        # The editor's V1 stays: B-roll goes on V2 over it, fitted to the canvas.
        from render import is_broll
        for k, (r, (_n, start, dur)) in enumerate(zip(ranges, timing)):
            if k == oi or not is_broll(r):
                continue
            src = sources[r["source"]]
            info = probe(src)
            video = next((x for x in info.get("streams", []) if x.get("codec_type") == "video"), {})
            cover = max(canvas[0] / int(video.get("width") or canvas[0]), canvas[1] / int(video.get("height") or canvas[1])) * 100
            bridge("place", path=str(src), kind="video", track=T_BROLL, start=start,
                   **{"in": float(r["start"]), "out": float(r["start"]) + dur}, name=str(r.get("beat") or "B-roll")[:60])
            bridge("motion", track=T_BROLL, start=start, keys=[{"t": 0, "scale": cover}])
    if oi is not None and want("picture"):
        # The outro covers everything: top track, fading in over the picture
        # (never through black), its sound on its own track.
        r = ranges[oi]
        cut = timing[oi][1]
        overlap = float(((brand.get("premiere_house") or {}).get("outro") or {}).get("overlap_s", 0.33))
        top = video_tracks  # above every other layer
        bridge("ensureTracks", video=top + 1, audio=1 + max(sfx_lanes + [0]) + 1 + n_comp_lanes + 1)
        st = max(0.0, cut - overlap)
        length = float(r["end"]) - float(r["start"]) + overlap
        bridge("clearTrack", kind="video", track=top)
        bridge("place", path=str(sources[r["source"]]), kind="video", track=top, start=st,
               **{"in": float(r["start"]), "out": float(r["start"]) + length}, name="outro")
        info = probe(sources[r["source"]])
        video = next((x for x in info.get("streams", []) if x.get("codec_type") == "video"), {})
        cover = max(canvas[0] / int(video.get("width") or canvas[0]), canvas[1] / int(video.get("height") or canvas[1])) * 100
        bridge("motion", track=top, start=st, keys=[{"t": 0, "scale": cover}])
        bridge("setProps", track=top, start=st, component="Opacity", props={"Opacity": {"keys": [
            {"t": 0, "v": 0}, {"t": overlap, "v": 100, "ease": "smooth"}]}})
        if any(x.get("codec_type") == "audio" for x in info.get("streams", [])):
            a_track = 1 + max(sfx_lanes + [0]) + 1 + n_comp_lanes
            bridge("clearTrack", kind="audio", track=a_track)
            bridge("place", path=str(sources[r["source"]]), kind="audio", track=a_track, start=st,
                   **{"in": float(r["start"]), "out": float(r["start"]) + length})
    for k, spec in enumerate(resolve_transitions(edl, brand)):
        if k == oi:
            continue
        if want("picture") and spec and spec.get("type") in ("dissolve", "fade", "dip_black", "dip_white"):
            name = {"dissolve": "Cross Dissolve", "fade": "Dip to Black", "dip_black": "Dip to Black",
                    "dip_white": "Dip to White"}[spec["type"]]
            ms = float(spec.get("duration_ms") or spec.get("in_ms") or 500)
            bridge("transition", track=0, start=timing[k][1], name=name, duration=ms / 1000, fps=fps)
    leak = (brand.get("premiere_house") or {}).get("light_leak")
    for clip, start, dur, cut_t in (lights if want("light") else []):
        if leak and leak.get("nodeId") and leak.get("pair"):
            # His transition exactly: the same light-leak graphic in two pieces
            # around the cut, each with its own in-point and pasted settings.
            for piece in leak["pair"]:
                st = max(0.0, cut_t + float(piece["offset"]))
                bridge("placeItem", nodeId=leak["nodeId"], track=T_LIGHT, start=st, duration=float(piece["duration"]),
                       name="light leak", **({"in": float(piece["in"])} if piece.get("in") else {}))
                ex = piece.get("exemplar") or leak.get("exemplar")
                if ex:
                    bridge("copyAttributes", fromSequence=ex["sequence"], fromTrack=ex["track"], fromStart=ex["start"], track=T_LIGHT, start=st)
            continue
        if leak and leak.get("nodeId"):  # the editor's own light-leak graphic, centred on the cut
            ld = float(leak.get("typical_duration") or 0.3)
            cut = start + dur * 0.43
            bridge("placeItem", nodeId=leak["nodeId"], track=T_LIGHT, start=max(0.0, cut - ld / 2), duration=ld, name="light leak")
            if leak.get("exemplar"):  # blend mode (Screen), colour, scale: exactly as in the editor's project
                ex = leak["exemplar"]
                bridge("copyAttributes", fromSequence=ex["sequence"], fromTrack=ex["track"], fromStart=ex["start"],
                       track=T_LIGHT, start=max(0.0, cut - ld / 2))
        else:
            bridge("place", path=str(clip), kind="video", track=T_LIGHT, start=max(0.0, start), **{"in": 0.0, "out": dur}, name="light flash")
    for (flat, start, dur, label), lane in (zip(behind, behind_lanes) if want("behind") else []):
        bridge("place", path=str(flat), kind="video", track=T_BEHIND + lane, start=start, **{"in": 0.0, "out": dur}, name=f"behind: {label}"[:60])
    for cut, start, dur in (cutouts if want("behind") else []):
        bridge("place", path=str(cut), kind="video", track=T_CUT, start=start, **{"in": 0.0, "out": dur}, name="subject cut-out")
        if not bridge.dry:  # his Lumetri on V1 → the cut-out, so the person on top matches the graded picture
            under = next((t[1] for t in timing if t[1] <= start + 1e-3 < t[1] + t[2]), None)
            if under is not None:
                try:
                    bridge("copyAttributes", fromSequence=title, fromTrack=0, fromStart=under, track=T_CUT, start=start,
                           only=["Lumetri Color"])
                except RuntimeError as e:
                    print(f"cut-out grade: {e}")
    layered = [g for g in graphics if "layers" in g]
    graphics = [g for g in graphics if "layers" not in g]
    gfx_lanes = spread([(g["start"], g["start"] + g["duration"]) for g in graphics])
    short = title.replace(" (video-use)", "")
    companions = (brand.get("premiere_house") or {}).get("text_sfx") or []
    comp_lanes = spread([(g["start"] + c["offset"], g["start"] + c["offset"] + c["duration"])
                         for g in layered for c in g.get("companions", companions)])
    first_comp_track = 1 + max(sfx_lanes + [0]) + 1
    fade = float((brand.get("premiere_house") or {}).get("text_fade_s", 0.1))
    lane_iter = iter(comp_lanes)
    for g in (layered if want("graphics") else []):
        for c in g.get("companions", companions):  # the editor's own sounds for this graphic, same offset, trim and level
            lane = next(lane_iter)
            at = max(0.0, g["start"] + c["offset"])
            bridge("place", path=str(c["file"]), kind="audio", track=first_comp_track + lane, start=at,
                   **{"in": c["in"], "out": c["in"] + c["duration"]})
            ex = c.get("exemplar")
            if ex:
                bridge("copyAttributes", kind="audio", fromSequence=ex["sequence"], fromTrack=ex["track"], fromStart=ex["start"],
                       track=first_comp_track + lane, start=at)
            else:  # no level to copy: the sound-design target (effects peak ~-9 dB)
                from audio_mix import file_peak_db
                target = float(((brand.get("audio") or {}).get("levels_peak_db") or {}).get("sfx", -9))
                peak = file_peak_db(Path(c["file"]))
                if peak is not None:
                    bridge("volume", track=first_comp_track + lane, start=at, db=round(target - peak, 2))
        for k, layer in enumerate(g["layers"]):
            node = layer["nodeId"]
            if layer.get("aep") and layer.get("comp"):
                # Its own copy of the composition per text (named after the words):
                # editing one text never changes the others; updates reuse it.
                node = bridge("aeCopy", aep=layer["aep"], comp=layer["comp"], bin=bin_name,
                              name=f"{short} – {g['text'][:40]}").get("nodeId", node)
            tl = T_GFX + max(gfx_lanes + [-1]) + 1 + int(layer.get("lane", k))
            st = g["start"] + float(layer.get("offset", 0))
            dur = max(0.3, g["duration"] - float(layer.get("offset", 0)))
            bridge("placeItem", nodeId=node, track=tl, start=st, duration=dur, name=layer.get("name"))
            ex = layer["exemplar"]
            bridge("copyAttributes", fromSequence=ex["sequence"], fromTrack=ex["track"], fromStart=ex["start"], track=tl, start=st)
            if fade > 0:  # a quick fade in and out on every layer
                f = min(fade, dur / 3)
                bridge("setProps", track=tl, start=st, component="Opacity", props={"Opacity": {"keys": [
                    {"t": 0, "v": 0}, {"t": f, "v": 100}, {"t": dur - f, "v": 100}, {"t": dur, "v": 0}]}})
        try:
            bridge("marker", start=g["start"], duration=g["duration"], name=g["text"], comments="اكتب في Source Text: " + g["text"])
        except RuntimeError:
            pass
    for g, lane in (zip(graphics, gfx_lanes) if want("graphics") else []):
        tg = T_GFX + lane
        if "copy" in g:  # one of the editor's own MOGRTs, with the new words
            bridge("placeItem", nodeId=g["copy"], track=tg, start=g["start"], duration=g["duration"], name=g["name"])
            if g["params"]:
                bridge("mgtParams", track=tg, start=g["start"], params=g["params"])
            bridge("setProps", track=tg, start=g["start"], component="Motion", props={"Position": {"value": g["position"]}})
            if g.get("paste"):  # the editor's text look: entrance blur/rise/fade + drop shadow
                ex = g["paste"]
                bridge("copyAttributes", fromSequence=ex["sequence"], fromTrack=ex["track"], fromStart=ex["start"],
                       track=tg, start=g["start"], skip=["Motion", "Opacity", "Text", "Vector Motion", "Graphic Parameters"])
                # The learned entrance keys, set explicitly (an effect added in the same call is not yet keyable).
                entrance = next((t.get("entrance") for t in (brand.get("premiere_house") or {}).get("text_tracks", [])
                                 if t.get("exemplar") == ex), None) or []
                by_comp: dict[str, dict] = {}
                for anim in entrance:
                    by_comp.setdefault(anim["component"], {})[anim["prop"]] = {"keys": [{"t": k["t"], "v": k["v"], "ease": "smooth"} for k in anim["keys"]]}
                for comp_name, props in by_comp.items():
                    bridge("setProps", track=tg, start=g["start"], component=comp_name, props=props)
        elif "mogrt" in g:
            bridge("mogrt", path=g["mogrt"], start=g["start"], duration=g["duration"], track=tg, params=g["params"])
        else:
            bridge("place", path=str(g["file"]), kind="video", track=tg, start=g["start"], **{"in": 0.0, "out": g["duration"]},
                   name=g["name"])
    for s, lane in (zip(sfx, sfx_lanes) if want("sfx") else []):
        path = _resolve(s["file"], base_dir)
        dur = float(probe(path).get("format", {}).get("duration", 0.5) or 0.5)
        start = float(s["start_in_output"])
        bridge("place", path=str(path), kind="audio", track=1 + lane, start=start, **{"in": 0.0, "out": dur})
        bridge("volume", track=1 + lane, start=start, db=float(s["gain_db"]))
    no_captions = bool((brand.get("premiere_house") or {}).get("no_captions"))  # he makes subtitles himself
    if edl.get("captions") and not no_captions and (not only or "captions" in only):
        bridge("captions", srt=str(srt), start=0.0)
    cap_house = (brand.get("premiere_house") or {}).get("captions") or {}
    if cap_house.get("exemplar") and not no_captions and (not only or "caption_fx" in only) and not bridge.dry:
        # After the editor upgrades the caption track to graphics, paste his
        # subtitle look onto every one (skipping Text, so each keeps its words).
        ex = cap_house["exemplar"]
        seq_now = bridge("inspect", props=False)
        tracks = [t for t in seq_now.get("video") or [] if sum(1 for c in t["clips"] if c["name"] == "Graphic") >= 3]
        if tracks:
            track = max(tracks, key=lambda t: len(t["clips"]))
            for c in track["clips"]:
                if c["name"] == "Graphic" and not (ex["sequence"] == title and ex["track"] == track["index"] and abs(ex["start"] - c["start"]) < 1e-3):
                    bridge("copyAttributes", fromSequence=ex["sequence"], fromTrack=ex["track"], fromStart=ex["start"],
                           track=track["index"], start=c["start"], skip=cap_house.get("skip") or ["Text"])
        elif only and "caption_fx" in only:
            print("caption_fx: no subtitle graphics yet — select the caption track, Graphics → Upgrade Caption to Graphic, then rerun")
    # The project is left unsaved: the editor reviews the new sequence, then saves.
    return {"sequence": title, "calls": len(bridge.log), "graphics": len(graphics), "sfx": len(sfx),
            "mogrt": sum(1 for g in graphics if "mogrt" in g)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a Video Use edit inside Premiere Pro")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("install")
    sub.add_parser("ping")
    sub.add_parser("sequences")
    p_learn = sub.add_parser("learn", help="read a hand-cut sequence into the project's house style")
    p_learn.add_argument("sequence")
    p_learn.add_argument("--edit", type=Path, required=True, help="the project's edit/ folder (house style → brand_kit.json)")
    p_build = sub.add_parser("build")
    p_build.add_argument("edl", type=Path)
    p_build.add_argument("--project", type=Path, help="create/open this .prproj first (default: the open project)")
    p_build.add_argument("--plan-only", action="store_true", help="print the Premiere calls without sending them")
    p_build.add_argument("--prepare", action="store_true",
                         help="Premiere path: first make the assets only (graphics, matte, SFX list) — no final video render")
    p_build.add_argument("--only", help=f"update just these parts of the existing sequence, comma-separated: {', '.join(SECTIONS)}")
    args = parser.parse_args()
    if args.cmd == "install":
        print(f"panel → {install()}\nIn Premiere: Window → Extensions → Video Use Bridge")
    elif args.cmd == "sequences":
        print(json.dumps(Bridge()("sequences"), ensure_ascii=False, indent=1))
    elif args.cmd == "learn":
        house = learn(args.sequence, Bridge())
        kit_path = args.edit / "brand_kit.json"
        kit = json.loads(kit_path.read_text(encoding="utf-8")) if kit_path.exists() else {}
        old = kit.get("premiere_house") or {}
        house["templates"] = old.get("templates", {})  # keep the style → template mapping
        kit["premiere_house"] = house
        favourites = favourite_sfx_map(house)
        if favourites:  # auto SFX now reaches for the editor's own favourites first
            kit["sfx_map"] = {**favourites, **(kit.get("sfx_map") or {})}
        kit_path.write_text(json.dumps(kit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(house, ensure_ascii=False, indent=1))
    elif args.cmd == "ping":
        b = Bridge()
        print(b.ping())
        print(b("ping"))
    else:
        b = Bridge(dry=args.plan_only)
        only = {x.strip() for x in args.only.split(",")} if args.only else None
        if only and only - set(SECTIONS):
            raise SystemExit(f"--only takes: {', '.join(SECTIONS)}")
        if args.prepare:  # the Premiere path: assets only, never a final video render
            edl_path = args.edl.resolve()
            subprocess.run([sys.executable, str(Path(__file__).with_name("render.py")), str(edl_path),
                            "-o", str(edl_path.parent / "premiere_preview.mp4"), "--premiere-assets"], check=True)
        result = build(args.edl, b, args.project, only)
        if args.plan_only:
            print(json.dumps(b.log, ensure_ascii=False, indent=1)[:6000])
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
