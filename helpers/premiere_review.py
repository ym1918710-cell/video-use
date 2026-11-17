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


def review(bridge, sequence: str, a: float, b: float, out_dir: Path, every: float = 2.0) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    inspect = bridge("inspect", sequence=sequence)
    media = media_checks(inspect, a, b)

    video = out_dir / "preview.mp4"
    if video.exists():
        video.unlink()
    t0 = time.time()
    res = bridge("preview", sequence=sequence, **{"from": a, "to": b}, file=str(video), preset=preset())
    if res.get("error") or not _wait(str(video), 60):
        raise SystemExit(f"Premiere made no preview: {res.get('error') or 'no file'}")
    took = round(time.time() - t0, 1)
    dur = _ffprobe_duration(video)

    pic = picture_checks(video, a)
    snd = sound_checks(video, a)

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
    if media["offline"]:  # the preview has Premiere's "Media Offline" card and no sound where those clips are
        issues.insert(0, "MEDIA OFFLINE: reconnect the files (or plug in the drive) and review again — the picture, "
                         "dead-air and loudness results below are not about the edit while media is missing")
    if snd["clipping"]:
        issues.append(f"the mix clips (sample peak {snd['sample_peak_db']} dBFS)")

    report = {
        "sequence": sequence, "from": a, "to": b, "preview": str(video), "preview_seconds": round(dur, 2),
        "export_took_s": took, "sheet": str(sheet), "filmstrip": str(strip) if strip.exists() else None,
        "media": media, "picture": pic, "sound": snd, "issues": issues,
        "not_checked_yet": ["text over the face", "captions against the spoken words", "SFX on their event",
                            "per-track levels (voice -3 / SFX -9 / music -24)"],
    }
    (out_dir / "review.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    lines = [f"# Review: {sequence} {a:.2f}–{b:.2f} s", "",
             f"- Preview: {video.name} ({dur:.1f} s, exported in {took} s)",
             f"- Loudness {snd['integrated_lufs']} LUFS, true peak {snd['true_peak_db']} dBFS (delivery target {TARGET_LUFS} LUFS)", "",
             "## Issues" if issues else "## Issues: none found", *[f"- {i}" for i in issues], "",
             "## Not checked yet", *[f"- {x}" for x in report["not_checked_yet"]]]
    (out_dir / "review.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report
