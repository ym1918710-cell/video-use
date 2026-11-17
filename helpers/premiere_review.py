"""See and hear a Premiere sequence: frames, a quick preview with its mix, and checks.

The agent cannot watch Premiere's Program monitor. This gets the sequence out
the way Premiere itself renders it, then measures it:

- `look`: still PNGs straight from the sequence (MOGRTs, Lumetri, AE comps and
  captions included) at chosen times, as one contact sheet.
- `review`: a quick H.264 of [from, to] with the full mix, then
    picture — black frames, stretches with no visual change, gaps on V1;
    sound   — loudness, true peak, clipping, dead air;
    media   — clips whose files are offline (an unplugged drive);
  plus a contact sheet and a filmstrip with the waveform, and a report.

    python helpers/premiere.py look "<sequence>" --at 12.5 40 61
    python helpers/premiere.py look "<sequence>" --from 0 --to 30 --every 2
    python helpers/premiere.py review "<sequence>" --from 359.4 --to 421.32

Times are sequence seconds. Nothing in the sequence changes: the playhead,
the active tab and the In/Out marks go back as they were.
"""

from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

PRESET_DIRS = [
    "/Applications/Adobe Media Encoder 2026/Adobe Media Encoder 2026.app/Contents/MediaIO/systempresets/4E49434B_48323634",
    "/Applications/Adobe Media Encoder 2027/Adobe Media Encoder 2027.app/Contents/MediaIO/systempresets/4E49434B_48323634",
]
PREVIEW_PRESET = "02 - Match Source - Low bitrate.epr"

STILL_S = 3.0        # a reel needs a visual change every 2–3 s
DEAD_AIR_S = 0.7     # silence longer than this inside speech
TARGET_LUFS = -14.0  # delivery loudness; the preview is the raw mix, so this is a hint
# Peak targets per role on Premiere's meter (sample peak, dBFS) and the slack around them.
# Defaults from real edits; a project can pass its own (review --levels voice=-3,sfx=-9,music=-24).
LEVEL_TARGETS = {"voice": -3.0, "sfx": -9.0, "music": -24.0}
LEVEL_SLACK = 2.5
SFX_SYNC_S = 0.15    # a hit more than ~4 frames from any picture event is off its event
SILENT_DB = -60.0    # a clip window quieter than this is a silent tail, not a level to judge
SPAN_SHAPES = ("whoosh", "riser", "swell", "sustained")  # these ride a movement; they have no single hit
MUSIC_MIN_S = 12.0   # a sound clip this long on a non-dialogue track is a bed, not an effect


def preset() -> str:
    for d in PRESET_DIRS:
        p = Path(d) / PREVIEW_PRESET
        if p.exists():
            return str(p)
    raise SystemExit(f"no Media Encoder preset '{PREVIEW_PRESET}' found in {PRESET_DIRS}")


def _wait(pattern: str, timeout: float) -> str | None:
    t = time.time()
    while time.time() - t < timeout:
        hits = sorted(glob.glob(pattern))
        if hits and os.path.getsize(hits[0]) > 0:
            return hits[0]
        time.sleep(0.25)
    return None


def _ffprobe_duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True).stdout.strip()
    return float(out or 0)


# ---------- contact sheet ----------

def contact_sheet(frames: list[tuple[float, Path]], out: Path, width: int = 270, cols: int = 6) -> Path:
    from PIL import Image, ImageDraw
    thumbs = []
    for t, f in frames:
        im = Image.open(f).convert("RGB")
        h = round(im.height * width / im.width)
        im = im.resize((width, h))
        d = ImageDraw.Draw(im)
        label = f"{int(t // 60)}:{t % 60:05.2f}"
        d.rectangle([0, 0, 8 + 8 * len(label), 22], fill=(0, 0, 0))
        d.text((5, 4), label, fill=(255, 255, 255))
        thumbs.append(im)
    if not thumbs:
        raise SystemExit("no frames to put on a sheet")
    rows = (len(thumbs) + cols - 1) // cols
    h = max(t.height for t in thumbs)
    sheet = Image.new("RGB", (cols * width + (cols - 1) * 4, rows * h + (rows - 1) * 4), (20, 20, 20))
    for i, im in enumerate(thumbs):
        sheet.paste(im, ((i % cols) * (width + 4), (i // cols) * (h + 4)))
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    return out


# ---------- look ----------

def look(bridge, sequence: str, times: list[float], out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("look_*.png"):
        old.unlink()
    res = bridge("frames", sequence=sequence, times=[round(t, 4) for t in times], folder=str(out_dir), prefix="look")
    got: list[tuple[float, Path]] = []
    errors = []
    for row in res.get("frames", []):
        if row.get("error"):
            errors.append(row)
            continue
        base = row["file"][:-4]  # Premiere sometimes writes "<name>.png.png"
        f = _wait(base + "*.png", 20)
        if f:
            got.append((row["t"], Path(f)))
        else:
            errors.append({"t": row["t"], "error": "Premiere wrote no file"})
    sheet = contact_sheet(got, out_dir / "look_sheet.png") if got else None
    return {"sheet": str(sheet) if sheet else None, "frames": [str(f) for _, f in got], "errors": errors}


# ---------- review ----------

def _clips_in(inspect: dict, a: float, b: float) -> list[tuple[str, int, dict]]:
    rows = []
    for kind in ("video", "audio"):
        for t in inspect.get(kind, []):
            for c in t["clips"]:
                if c["end"] > a and c["start"] < b:
                    rows.append((kind, t["index"], c))
    return rows


def media_checks(inspect: dict, a: float, b: float) -> dict:
    offline, gaps = [], []
    seen = set()
    for kind, idx, c in _clips_in(inspect, a, b):
        path = ((c.get("item") or {}).get("path") or "").strip()
        if c.get("disabled") or not path or path in seen:
            continue
        seen.add(path)
        if not os.path.exists(path):
            target = os.path.realpath(path)
            offline.append({"track": f"{kind[0].upper()}{idx + 1}", "at": round(c["start"], 2), "file": path,
                            "points_to": target if target != path else None})
    v1 = sorted((c["start"], c["end"]) for kind, idx, c in _clips_in(inspect, a, b)
                if kind == "video" and idx == 0 and not c.get("disabled"))
    cur = a
    for s, e in v1:
        if s - cur > 0.04:
            gaps.append([round(cur, 2), round(s, 2)])
        cur = max(cur, e)
    if b - cur > 0.04:
        gaps.append([round(cur, 2), round(b, 2)])
    return {"offline": offline, "v1_gaps": gaps}


def _ffmpeg_log(args: list[str]) -> str:
    return subprocess.run(["ffmpeg", "-hide_banner", "-nostats", *args, "-f", "null", "-"],
                          capture_output=True, text=True).stderr


def picture_checks(video: Path, offset: float) -> dict:
    log = _ffmpeg_log(["-i", str(video), "-vf", "blackdetect=d=0.08:pix_th=0.08,freezedetect=n=0.003:d=%s" % STILL_S, "-an"])
    black = [[round(float(s) + offset, 2), round(float(e) + offset, 2)]
             for s, e in re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", log)]
    starts = [float(x) for x in re.findall(r"freeze_start: ([\d.]+)", log)]
    ends = [float(x) for x in re.findall(r"freeze_end: ([\d.]+)", log)]
    dur = _ffprobe_duration(video)
    still = [[round(s + offset, 2), round((ends[i] if i < len(ends) else dur) + offset, 2)] for i, s in enumerate(starts)]
    return {"black": black, "no_visual_change": still}


def sound_checks(video: Path, offset: float) -> dict:
    log = _ffmpeg_log(["-i", str(video), "-vn", "-af", "ebur128=peak=true,silencedetect=n=-40dB:d=%s" % DEAD_AIR_S])
    lufs = re.findall(r"I:\s+(-?[\d.]+) LUFS", log)
    tp = re.findall(r"Peak:\s+(-?[\d.]+) dBFS", log)
    ss = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", log)]
    se = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", log)]
    dur = _ffprobe_duration(video)
    dead = []
    for i, s in enumerate(ss):
        e = se[i] if i < len(se) else dur
        if s > 0.3 and e < dur - 0.3:  # silence at the very start/end of a part is a breath, not dead air
            dead.append([round(s + offset, 2), round(e + offset, 2)])
    from listen import listen
    d = listen(str(video))
    peak = float(d["peak_db"]) if d.get("peak_db") is not None else None
    return {"integrated_lufs": float(lufs[-1]) if lufs else None, "true_peak_db": float(tp[-1]) if tp else None,
            "sample_peak_db": peak, "dead_air": dead, "clipping": bool(peak is not None and peak > -0.1)}


def _volume_db(clip: dict) -> float:
    """The clip's Volume › Level in dB (Premiere's scale: 0 dB = 0.1778)."""
    import math
    for comp in clip.get("components") or []:
        if comp.get("name") == "Volume":
            for pr in comp.get("props", []):
                if pr.get("name") == "Level" and isinstance(pr.get("value"), (int, float)) and pr["value"] > 0:
                    return round(20 * math.log10(pr["value"]) + 15, 2)
    return 0.0


_WINDOW_PEAKS: dict[tuple, float | None] = {}


def _window_peak(path: str, start: float, dur: float) -> float | None:
    key = (path, round(start, 3), round(dur, 3))
    if key not in _WINDOW_PEAKS:
        log = _ffmpeg_log(["-ss", f"{max(0.0, start):.3f}", "-t", f"{max(0.04, dur):.3f}", "-i", path, "-vn", "-af", "volumedetect"])
        m = re.search(r"max_volume:\s*(-?[\d.]+) dB", log)
        _WINDOW_PEAKS[key] = float(m.group(1)) if m else None
    return _WINDOW_PEAKS[key]


def _role(track_index: int, clip: dict) -> str:
    if track_index == 0:
        return "voice"
    return "music" if clip["end"] - clip["start"] >= MUSIC_MIN_S else "sfx"


def level_checks(inspect: dict, a: float, b: float, targets: dict[str, float]) -> dict:
    """Peak each sound clip reaches on the meter: its file's peak over the used window plus its Volume.

    Measured from the clips themselves, so it works per track and even when the picture is offline.
    """
    rows, off = [], []
    for t in inspect.get("audio", []):
        for c in t["clips"]:
            if c["end"] <= a or c["start"] >= b or c.get("disabled"):
                continue
            path = ((c.get("item") or {}).get("path") or "").strip()
            if not path or not os.path.exists(path):
                continue
            src = _window_peak(path, c["inPoint"], c["end"] - c["start"])
            if src is None or src < SILENT_DB:
                continue
            role = _role(t["index"], c)
            peak = round(src + _volume_db(c), 1)
            row = {"track": f"A{t['index'] + 1}", "at": round(c["start"], 2), "name": c["name"], "role": role,
                   "peak_db": peak, "target_db": targets.get(role)}
            rows.append(row)
            tgt = targets.get(role)
            if tgt is not None and abs(peak - tgt) > LEVEL_SLACK:
                off.append(row)
    by_track: dict[str, dict] = {}
    for r in rows:
        tr = by_track.setdefault(r["track"], {"clips": 0, "max_peak_db": -99.0, "roles": set()})
        tr["clips"] += 1
        tr["max_peak_db"] = max(tr["max_peak_db"], r["peak_db"])
        tr["roles"].add(r["role"])
    tracks = {k: {**v, "roles": sorted(v["roles"])} for k, v in sorted(by_track.items())}
    return {"tracks": tracks, "off_target": off, "clips": rows}


def motion_series(video: Path, offset: float, width: int = 180, pixel_step: int = 24,
                  start: float | None = None, dur: float | None = None):
    """Per frame: the share (%) of pixels that clearly changed since the previous frame, on sequence time.

    Counting changed pixels (not the mean difference) makes a small icon popping in a clear event,
    while compression noise stays under `pixel_step`.
    """
    import numpy as np
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height,r_frame_rate",
                            "-of", "json", str(video)], capture_output=True, text=True).stdout
    st = json.loads(probe)["streams"][0]
    h = round(st["height"] * width / st["width"] / 2) * 2
    num, den = (int(x) for x in st["r_frame_rate"].split("/"))
    fps = num / den if den else 25.0
    window = (["-ss", f"{start:.3f}"] if start else []) + (["-t", f"{dur:.3f}"] if dur else [])
    raw = subprocess.run(["ffmpeg", "-v", "error", *window, "-i", str(video), "-an", "-vf", f"scale={width}:{h},format=gray",
                          "-f", "rawvideo", "-"], capture_output=True).stdout
    frames = np.frombuffer(raw, dtype=np.uint8)
    n = len(frames) // (width * h)
    if n < 2:
        return np.array([]), np.array([])
    f = frames[: n * width * h].reshape(n, h, width).astype(np.int16)
    changed = (np.abs(np.diff(f, axis=0)) > pixel_step).mean(axis=(1, 2)) * 100
    ys = np.concatenate([[0.0], changed])
    ts = np.arange(n) / fps + offset
    return ts, ys


VIDEO_EXT = (".mov", ".mp4", ".m4v", ".mxf", ".webm", ".mkv")
MOVING_PCT = 0.3   # % of pixels changing for the layer to count as moving


def _caption_like(clips: list[dict]) -> bool:
    """A track of many short clips (word-by-word captions): sound effects are not tied to it."""
    if len(clips) < 10:
        return False
    durs = sorted(c["end"] - c["start"] for c in clips)
    return durs[len(durs) // 2] < 1.2


def layer_events(inspect: dict, a: float, b: float) -> tuple[list[float], list[tuple[float, float]]]:
    """Picture events from the layers above the base picture, read from the timeline and the layers' own files.

    Returns (points, moving): points are clip starts/ends and the moments a layer starts or stops moving;
    moving are the stretches a layer's own content is in motion. Word-by-word caption tracks are skipped,
    and so is V1 except its cuts.
    """
    import numpy as np
    points: set[float] = set()
    moving: list[tuple[float, float]] = []
    for t in inspect.get("video", []):
        clips = [c for c in t["clips"] if c["end"] > a - 1 and c["start"] < b + 1 and not c.get("disabled")]
        if t["index"] > 0 and _caption_like(clips):
            continue
        for c in clips:
            for x in (c["start"], c["end"]):
                if a <= x <= b:
                    points.add(round(x, 3))
            if t["index"] == 0:
                continue
            path = ((c.get("item") or {}).get("path") or "").strip()
            if not path.lower().endswith(VIDEO_EXT) or not os.path.exists(path):
                continue
            ts, ys = motion_series(Path(path), c["start"] - 0.0, start=c["inPoint"], dur=c["end"] - c["start"])
            if not len(ts):
                continue
            ts = ts - ts[0] + c["start"]
            on = ys > MOVING_PCT
            k = 0
            while k < len(on):
                if on[k]:
                    m = k
                    while m + 1 < len(on) and on[m + 1]:
                        m += 1
                    if ts[m] - ts[k] >= 0.08:
                        moving.append((float(ts[k]), float(ts[m])))
                        points.update({round(float(ts[k]), 3), round(float(ts[m]), 3)})
                    k = m + 1
                else:
                    k += 1
    return sorted(points), moving


def sfx_sync_checks(inspect: dict, a: float, b: float, video: Path | None = None, search_s: float = 0.6) -> dict:
    """Is each sound effect where the picture does something?

    Events come from the layers, not the final frame (word captions and the speaker would drown a small
    icon): clip starts/ends on the timeline, and the moments a graphic's own file starts or stops moving.
    A point sound (pop, hit, click) passes when an event is within SFX_SYNC_S of its hit; a sound that
    rides a movement (whoosh, riser) passes when a layer moves or an event falls inside its span.
    Otherwise it is flagged with how far the nearest event is (within ±search_s), or that there is none.
    """
    from listen import listen
    points, moving = layer_events(inspect, a, b)
    rows, off = [], []
    for t in inspect.get("audio", []):
        if t["index"] == 0:
            continue
        for c in t["clips"]:
            if not (a <= c["start"] < b) or c.get("disabled") or _role(t["index"], c) != "sfx":
                continue
            path = ((c.get("item") or {}).get("path") or "").strip()
            if not path or not os.path.exists(path):
                continue
            d = listen(path, c["inPoint"], c["inPoint"] + (c["end"] - c["start"]))
            hit = round(c["start"] + float(d.get("hit_point_s") or 0.0), 3)
            if not (a <= hit <= b - 0.05):  # it belongs to the part next door
                continue
            shape = str(d.get("shape") or "")
            if shape in ("silence", "empty"):  # a silent leftover of a trimmed sound
                continue
            row = {"track": f"A{t['index'] + 1}", "name": c["name"], "shape": shape, "hit": hit}
            near = min(points, key=lambda e: abs(e - hit)) if points else None
            if shape.startswith(SPAN_SHAPES):
                s0, s1 = c["start"], c["end"]
                ok = any(m0 < s1 and m1 > s0 for m0, m1 in moving) or any(s0 - 0.05 <= e <= s1 + 0.05 for e in points)
            else:
                ok = near is not None and abs(near - hit) <= SFX_SYNC_S
            move = None if ok or near is None or abs(near - hit) > search_s else round(near - hit, 2)
            row.update({"ok": bool(ok), "nearest_event": near, "move_s": move})
            rows.append(row)
            if not ok:
                off.append(row)
    return {"sfx": rows, "off_event": off, "events": len(points), "moving_stretches": len(moving)}


# ---------- words on screen, faces, and what is said ----------

VISION_SRC = Path(__file__).with_name("frame_boxes.swift")
VISION_BIN = Path(os.environ.get("TMPDIR", "/tmp")) / "video-use-frame-boxes"
CAPTION_MATCH = 0.5      # share of a caption's words that must be heard while it is up
READ_CONFIDENT = 0.5     # Vision's confidence below which a caption reading is a guess, not evidence
UNCAPTIONED_S = 1.5      # speech this long with no caption on screen
FACE_OVERLAP = 0.15      # share of a text box lying on the face


def vision(images: list[Path]) -> list[dict]:
    """Faces and recognised text (Arabic + English) per image, from macOS Vision, on this machine."""
    import shutil
    if sys.platform != "darwin" or not shutil.which("swiftc"):
        raise RuntimeError("frame_boxes needs macOS Vision")
    if not VISION_BIN.exists() or VISION_BIN.stat().st_mtime < VISION_SRC.stat().st_mtime:
        subprocess.run(["swiftc", str(VISION_SRC), "-o", str(VISION_BIN), "-framework", "Vision", "-framework", "AppKit"],
                       check=True, capture_output=True)
    out: list[dict] = []
    for k in range(0, len(images), 40):  # keep the argument list short
        res = subprocess.run([str(VISION_BIN), *map(str, images[k:k + 40])], capture_output=True, text=True, check=True)
        out += json.loads(res.stdout)
    return out


AR_MARKS = re.compile(r"[\u064B-\u0652\u0640\u0670]")


def norm_tokens(text: str) -> list[str]:
    t = AR_MARKS.sub("", text.lower())
    for a_, b_ in (("أ", "ا"), ("إ", "ا"), ("آ", "ا"), ("ى", "ي"), ("ة", "ه"), ("ؤ", "و"), ("ئ", "ي")):
        t = t.replace(a_, b_)
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"(?<=[\u0621-\u064A])(?=[a-z0-9])|(?<=[a-z0-9])(?=[\u0621-\u064A])", " ", t)  # "وsql" → "و sql"
    return [w for w in t.split() if w]


def _meaningful(tokens: list[str]) -> list[str]:
    """Drop OCR crumbs: Arabic pieces under 3 letters (a lone article, a letter of an icon label)."""
    return [w for w in tokens if len(w) >= 3 or re.match(r"[a-z0-9]", w)]


def _close(a_: str, b_: str) -> bool:
    """Same word, allowing one OCR slip in longer words and the Arabic article."""
    if a_ == b_:
        return True
    for x, y in ((a_, b_), (b_, a_)):
        if x.startswith("ال") and x[2:] == y:
            return True
    if min(len(a_), len(b_)) < 4 or abs(len(a_) - len(b_)) > 1:
        return False
    # one edit
    i = 0
    while i < min(len(a_), len(b_)) and a_[i] == b_[i]:
        i += 1
    return a_[i + 1:] == b_[i + 1:] or a_[i + 1:] == b_[i:] or a_[i:] == b_[i + 1:]


def find_transcript(source: str, edit_dir: Path | None) -> Path | None:
    stem = Path(source).stem
    roots = [edit_dir] if edit_dir else []
    roots += list(Path(source).parents)[:3]
    for r in roots:
        for cand in (r / "transcripts" / f"{stem}.json", r / "edit" / "transcripts" / f"{stem}.json"):
            if cand.exists():
                return cand
    return None


def spoken_words(inspect: dict, a: float, b: float, edit_dir: Path | None) -> tuple[list[dict], list[str]]:
    """Words heard in [a, b] on sequence time, from the V1 clips' transcripts."""
    words, missing = [], []
    v1 = next((t for t in inspect.get("video", []) if t["index"] == 0), {"clips": []})
    cache: dict[str, list] = {}
    for c in v1["clips"]:
        if c["end"] <= a or c["start"] >= b or c.get("disabled"):
            continue
        path = ((c.get("item") or {}).get("path") or "").strip()
        if not path:
            continue
        if path not in cache:
            tp = find_transcript(path, edit_dir)
            cache[path] = json.loads(tp.read_text(encoding="utf-8")).get("words", []) if tp else []
            if not tp:
                missing.append(Path(path).name)
        src0, src1 = c["inPoint"], c["inPoint"] + (c["end"] - c["start"])
        for w in cache[path]:
            if w.get("type", "word") != "word" or w["start"] < src0 or w["end"] > src1 + 0.05:
                continue
            t0 = c["start"] + (w["start"] - src0)
            if a <= t0 < b:
                words.append({"t": round(t0, 3), "e": round(c["start"] + (w["end"] - src0), 3), "w": w["text"]})
    words.sort(key=lambda x: x["t"])
    return words, sorted(set(missing))


def caption_clips(inspect: dict, a: float, b: float) -> list[dict]:
    rows = []
    for t in inspect.get("video", []):
        clips = [c for c in t["clips"] if not c.get("disabled")]
        if t["index"] > 0 and _caption_like(clips):
            rows += [{"start": c["start"], "end": c["end"]} for c in clips if c["end"] > a and c["start"] < b]
    return sorted(rows, key=lambda r: r["start"])


def _frame(video: Path, t: float, out: Path, width: int = 720) -> Path | None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{max(0.0, t):.3f}", "-i", str(video), "-frames:v", "1",
                    "-vf", f"scale={width}:-2", str(out)], capture_output=True)
    return out if out.exists() else None


def text_on_face(row: dict) -> list[str]:
    """Texts in one Vision result whose box lies on a face by more than FACE_OVERLAP of the text's area."""
    hits = []
    for f in row.get("faces", []):
        for x in row.get("text", []):
            ix = max(0.0, min(f["x"] + f["w"], x["x"] + x["w"]) - max(f["x"], x["x"]))
            iy = max(0.0, min(f["y"] + f["h"], x["y"] + x["h"]) - max(f["y"], x["y"]))
            area = x["w"] * x["h"]
            if area and ix * iy / area > FACE_OVERLAP:
                hits.append(x["s"])
    return hits


def screen_checks(inspect: dict, a: float, b: float, video: Path, work: Path, edit_dir: Path | None,
                  face_every: float = 1.0) -> dict:
    """Captions against the spoken words, speech without captions, and text lying on the face."""
    work.mkdir(parents=True, exist_ok=True)
    for old in work.glob("*.png"):
        old.unlink()
    caps = caption_clips(inspect, a, b)
    for c in caps:  # read each caption after its entrance animation, before it leaves
        c["read_at"] = round(min(max((c["start"] + c["end"]) / 2, c["start"] + 0.42), c["end"] - 0.04), 3)
    times = sorted({c["read_at"] for c in caps} |
                   {round(a + k * face_every, 3) for k in range(int((b - a) / face_every) + 1)})
    shots = []
    for k, t in enumerate(times):
        f = _frame(video, t - a, work / f"v_{k:04d}.png")
        if f:
            shots.append((t, f))
    seen = vision([f for _, f in shots]) if shots else []
    by_time = {t: r for (t, _), r in zip(shots, seen)}

    # text that stays on screen most of the time (titles, table labels) is not a caption
    counts: dict[str, int] = {}
    for r in seen:
        for s in {x["s"] for x in r.get("text", [])}:
            counts[s] = counts.get(s, 0) + 1
    steady = {s for s, n in counts.items() if n > max(3, 0.4 * len(seen))}

    # the caption band: the height where text changes from one caption to the next
    ys_ = sorted(x["y"] + x["h"] / 2 for c in caps for x in (by_time.get(c["read_at"]) or {}).get("text", [])
                 if x["s"] not in steady)
    band = None
    if ys_:
        bins: dict[int, int] = {}
        for y in ys_:
            bins[int(y * 20)] = bins.get(int(y * 20), 0) + 1
        top = max(bins, key=bins.get)
        band = (top / 20 - 0.06, (top + 1) / 20 + 0.06)

    words, missing = spoken_words(inspect, a, b, edit_dir)
    mismatched: list[dict] = []
    unread: list[dict] = []
    for c in caps:
        r = by_time.get(c["read_at"])
        if not r:
            continue
        boxes = [x for x in r.get("text", []) if x["s"] not in steady and x.get("c", 1) >= 0.3
                 and (band is None or band[0] <= x["y"] + x["h"] / 2 <= band[1])]
        shown = " ".join(x["s"] for x in boxes)
        conf = min((x.get("c", 1) for x in boxes), default=1.0)
        cap = _meaningful(norm_tokens(shown))
        if not cap:
            continue
        heard = norm_tokens(" ".join(w["w"] for w in words if w["e"] >= c["start"] - 0.35 and w["t"] <= c["end"] + 0.35))

        def score(tokens: list[str]) -> float:
            return sum(1 for x in tokens if any(_close(x, y) for y in heard)) / len(tokens) if tokens else 1.0
        ratio = score(cap)
        if ratio < CAPTION_MATCH and c["end"] - c["read_at"] > 0.12:  # read it again from a later frame before flagging
            for k2, t2 in enumerate((c["read_at"], c["end"] - 0.08)):  # full resolution this time
                again = _frame(video, t2 - a, work / f"r_{len(mismatched)}_{len(unread)}_{k2}.png", width=1080)
                if not again:
                    continue
                r2 = vision([again])[0]
                boxes2 = [x for x in r2.get("text", []) if x["s"] not in steady and x.get("c", 1) >= 0.3
                          and (band is None or band[0] <= x["y"] + x["h"] / 2 <= band[1])]
                cap2 = _meaningful(norm_tokens(" ".join(x["s"] for x in boxes2)))
                if cap2 and score(cap2) > ratio:
                    shown, cap, ratio = " ".join(x["s"] for x in boxes2), cap2, score(cap2)
                    conf = min(x.get("c", 1) for x in boxes2)
        if ratio < CAPTION_MATCH and len(cap) >= 2 and conf < READ_CONFIDENT:
            unread.append({"at": round(c["start"], 2), "read_as": shown, "heard": " ".join(heard)})
        elif ratio < CAPTION_MATCH and len(cap) >= 2:
            mismatched.append({"at": round(c["start"], 2), "shown": shown, "heard": " ".join(heard), "match": round(ratio, 2)})

    uncaptioned = []
    if caps or words:
        k = 0
        run: list[dict] = []
        def covered(t: float) -> bool:
            return any(c["start"] - 0.1 <= t <= c["end"] + 0.1 for c in caps)
        for w in words + [{"t": 1e9, "e": 1e9, "w": ""}]:
            if w["w"] and not covered(w["t"]):
                if run and w["t"] - run[-1]["e"] > 0.6:
                    if run[-1]["e"] - run[0]["t"] >= UNCAPTIONED_S:
                        uncaptioned.append([round(run[0]["t"], 2), round(run[-1]["e"], 2), " ".join(x["w"] for x in run)])
                    run = []
                run.append(w)
            else:
                if run and run[-1]["e"] - run[0]["t"] >= UNCAPTIONED_S:
                    uncaptioned.append([round(run[0]["t"], 2), round(run[-1]["e"], 2), " ".join(x["w"] for x in run)])
                run = []

    on_face = [{"at": t, "text": s_} for t, r in sorted(by_time.items()) for s_ in text_on_face(r)]
    # speech whose words are on screen anyway (a question card, a title) is covered by that graphic
    def on_screen(t0: float, t1: float, said: str) -> bool:
        toks = norm_tokens(said)
        if not toks:
            return True
        shown = norm_tokens(" ".join(x["s"] for t, r in by_time.items() if t0 - 0.2 <= t <= t1 + 0.2 for x in r.get("text", [])))
        return sum(1 for w in toks if any(_close(w, y) for y in shown)) / len(toks) >= CAPTION_MATCH
    uncaptioned = [u for u in uncaptioned if not on_screen(u[0], u[1], u[2])]
    faces_seen = sum(1 for r in seen if r.get("faces"))
    return {"captions": len(caps), "caption_band": [round(x, 2) for x in band] if band else None,
            "frames_read": len(seen), "frames_with_face": faces_seen,
            "words_heard": len(words), "transcripts_missing": missing,
            "caption_mismatch": mismatched, "captions_unread": unread, "uncaptioned_speech": uncaptioned,
            "text_on_face": on_face}


def review(bridge, sequence: str, a: float, b: float, out_dir: Path, every: float = 2.0,
           targets: dict[str, float] | None = None, reuse: bool = False, edit_dir: Path | None = None,
           video_file: Path | None = None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    inspect = bridge("inspect", sequence=sequence, audioProps=True)
    media = media_checks(inspect, a, b)
    targets = {**LEVEL_TARGETS, **(targets or {})}
    levels = level_checks(inspect, a, b, targets)

    video = Path(video_file) if video_file else out_dir / "preview.mp4"
    t0 = time.time()
    if video_file:  # an export of exactly [from, to] (e.g. the delivered reel): check it as it is
        took = 0.0
        if not video.exists():
            raise SystemExit(f"no such video: {video}")
    elif reuse and video.exists() and video.stat().st_size > 0:  # same part, nothing re-exported (checks only)
        took = 0.0
    else:
        if video.exists():
            video.unlink()
        res = bridge("preview", sequence=sequence, **{"from": a, "to": b}, file=str(video), preset=preset())
        if res.get("error") or not _wait(str(video), 60):
            raise SystemExit(f"Premiere made no preview: {res.get('error') or 'no file'}")
        took = round(time.time() - t0, 1)
    dur = _ffprobe_duration(video)

    pic = picture_checks(video, a)
    snd = sound_checks(video, a)
    sync = sfx_sync_checks(inspect, a, b)
    if media["offline"] and not video_file:  # the "Media Offline" card would be read as text, and there is no face to find
        screen: dict[str, Any] = {"skipped": "media offline"}
    else:
        screen = screen_checks(inspect, a, b, video, out_dir / "screen", edit_dir)

    frames_dir = out_dir / "frames"
    frames_dir.mkdir(exist_ok=True)
    for old in frames_dir.glob("*.png"):
        old.unlink()
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-vf", f"fps=1/{every}", str(frames_dir / "p_%04d.png")], check=True)
    shots = sorted(frames_dir.glob("p_*.png"))
    sheet = contact_sheet([(a + i * every, f) for i, f in enumerate(shots)], out_dir / "sheet.png")
    strip = out_dir / "filmstrip.png"
    subprocess.run([sys.executable, str(Path(__file__).with_name("timeline_view.py")), str(video), "0", f"{max(0.5, dur - 0.15):.2f}",
                    "-o", str(strip), "--n-frames", "10"], capture_output=True)

    issues = []
    for f in media["offline"]:
        issues.append(f"offline media on {f['track']} at {f['at']} s: {Path(f['file']).name}"
                      + (f" → {f['points_to']}" if f.get("points_to") else ""))
    for g in media["v1_gaps"]:
        issues.append(f"gap on V1 {g[0]}–{g[1]} s")
    for s, e in pic["black"]:
        issues.append(f"black picture {s}–{e} s")
    for s, e in pic["no_visual_change"]:
        issues.append(f"no visual change {s}–{e} s ({e - s:.1f} s; a reel wants one every 2–3 s)")
    for s, e in snd["dead_air"]:
        issues.append(f"dead air {s}–{e} s ({e - s:.1f} s)")
    if media["offline"] and not video_file:  # the preview has Premiere's "Media Offline" card and no sound where those clips are
        issues.insert(0, "MEDIA OFFLINE: reconnect the files (or plug in the drive) and review again — the picture, "
                         "dead-air and loudness results below are not about the edit while media is missing")
    for r in levels["off_target"]:
        issues.append(f"{r['role']} level {r['peak_db']} dB on {r['track']} at {r['at']} s ({r['name']}); target {r['target_db']} dB")
    for r in sync["off_event"]:
        if r["move_s"] is None:
            issues.append(f"SFX with nothing happening on screen: {r['name']} on {r['track']} at {r['hit']} s")
        else:
            issues.append(f"SFX off its event: {r['name']} on {r['track']} at {r['hit']} s — the nearest picture event is "
                          f"{abs(r['move_s']):.2f} s {'later' if r['move_s'] > 0 else 'earlier'} (move it {r['move_s']:+.2f} s)")
    for m in screen.get("caption_mismatch", []):
        issues.append(f"caption at {m['at']} s doesn't match the speech — on screen: «{m['shown']}», heard: «{m['heard']}»")
    for s0, s1, said in screen.get("uncaptioned_speech", []):
        issues.append(f"speech with no caption {s0}–{s1} s: «{said}»")
    for f in screen.get("text_on_face", []):
        issues.append(f"text on the face at {f['at']} s: «{f['text']}»")
    for t in screen.get("transcripts_missing", []):
        issues.append(f"no transcript found for {t} (captions can't be checked against the words; pass --edit)")
    if snd["clipping"]:
        issues.append(f"the mix clips (sample peak {snd['sample_peak_db']} dBFS)")

    report = {
        "sequence": sequence, "from": a, "to": b, "preview": str(video), "preview_seconds": round(dur, 2),
        "export_took_s": took, "sheet": str(sheet), "filmstrip": str(strip) if strip.exists() else None,
        "media": media, "picture": pic, "sound": snd, "levels": levels, "sfx_sync": sync, "screen": screen, "issues": issues,
        "not_checked_yet": (["captions, speech and the face (media offline)"] if media["offline"] and not video_file else []),
    }
    (out_dir / "review.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    lines = [f"# Review: {sequence} {a:.2f}–{b:.2f} s", "",
             f"- Preview: {video.name} ({dur:.1f} s, exported in {took} s)",
             f"- Loudness {snd['integrated_lufs']} LUFS, true peak {snd['true_peak_db']} dBFS (delivery target {TARGET_LUFS} LUFS)",
             f"- SFX on their event: {len(sync['sfx']) - len(sync['off_event'])}/{len(sync['sfx'])}",
             "- Peak per track: " + (", ".join(f"{k} {v['max_peak_db']} dB ({'/'.join(v['roles'])})" for k, v in levels["tracks"].items()) or "no sound files found"),
             f"- Targets: " + ", ".join(f"{k} {v} dB" for k, v in targets.items()) + f" (±{LEVEL_SLACK})",
             (f"- Captions read: {screen['captions'] - len(screen['caption_mismatch']) - len(screen['captions_unread'])}/{screen['captions']} match the speech"
              f" ({len(screen['captions_unread'])} unreadable); face in {screen['frames_with_face']}/{screen['frames_read']} frames"
              if "captions" in screen else "- Captions and face: skipped (media offline)"), "",
             "## Issues" if issues else "## Issues: none found", *[f"- {i}" for i in issues], "",
             *(["## Not checked", *[f"- {x}" for x in report["not_checked_yet"]]] if report["not_checked_yet"] else [])]
    (out_dir / "review.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report
