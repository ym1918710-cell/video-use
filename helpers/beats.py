"""Editorial beats: short, rule-based moments a line earns.

``denial``: the speaker negates who or what they are ("أنا مش ممدوح",
"وأنا مش data analyst", "I'm not a designer"). Picture: a colour strobe behind
the person (2–3 flashes in 0.3 s) with the person kept in front by the local
matte. Sound: an "error" buzzer starting just before the beat. It turns a
spoken joke into a visual one.

EDL::

    "beats": [{"type": "denial", "at": {"source": "C1", "time": 5.86}, "duration": 0.43}]

Find candidates from the transcripts::

    python helpers/beats.py suggest edit/edl.json

Colour and sound come from the brand kit (``beats.denial.color`` /
``beats.denial.sfx``), else red and the material library's "error" sounds.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

SUBJECTS = {"انا", "أنا", "وانا", "وأنا", "إحنا", "احنا", "واحنا", "i", "i'm", "im", "we're", "were"}
NEGATIONS = {"مش", "مو", "لست", "مكنتش", "ماكنتش", "not", "never"}
STROBE = [(0.0, 1.0), (0.033, 0.0), (0.1, 1.0), (0.167, 0.0), (0.2, 1.0), (0.3, 0.0)]  # (t, alpha), held between


def _bare(w: str) -> str:
    return re.sub(r"[^\w']", "", w.lower())


def find_denials(words: list[dict]) -> list[tuple[float, float, str]]:
    """(start, end, phrase) for 'I'm not X' lines: from the negation to the
    end of the word it denies (capped at ~0.45 s)."""
    ws = [w for w in words if w.get("type", "word") == "word" and w.get("start") is not None]
    out = []
    for k, w in enumerate(ws):
        if _bare(w["text"]) not in NEGATIONS:
            continue
        # The subject must be close: within two words and the same breath.
        near = [x for x in ws[max(0, k - 2):k] if float(w["start"]) - float(x["end"]) < 0.8]
        back = [_bare(x["text"]) for x in near]
        if not any(b in SUBJECTS or b.startswith(("i'm", "im")) for b in back) and _bare(w["text"]) != "not":
            continue
        if _bare(w["text"]) == "not" and not any(b in ("i'm", "im", "i", "am", "we're") for b in back):
            continue
        # Starts on "وأنا" when it directly precedes the negation ("وأنا مش …"),
        # else on the negation ("أنا اسمي مش …"); lasts 13 frames (0.43 s).
        prev = ws[k - 1] if k else None
        anchor = prev if prev is not None and _bare(prev["text"]) in SUBJECTS else w
        start = float(anchor["start"]) - (0.0 if anchor is prev else 0.06)
        end = start + 0.433
        phrase = " ".join(x["text"] for x in ws[max(0, k - 2):k + 2])
        out.append((round(start, 3), round(end, 3), phrase))
    return out


def suggest(edl: dict, edit_dir: Path) -> list[dict]:
    from transcripts import transcript_path
    found = []
    for r in edl.get("ranges") or []:
        sound = r.get("audio_source") if isinstance(r.get("audio_source"), dict) else None
        src = sound["source"] if sound else r["source"]
        a = float(sound.get("start", r["start"])) if sound else float(r["start"])
        b = a + float(r["end"]) - float(r["start"])
        path = transcript_path(edit_dir, edl, src)
        if not path.exists():
            continue
        words = json.loads(path.read_text(encoding="utf-8")).get("words", [])
        for st, en, phrase in find_denials(words):
            if a <= st < b:
                found.append({"type": "denial", "at": {"source": src, "time": st}, "duration": round(en - st, 3), "phrase": phrase})
    return found


def strobe_clip(out: Path, canvas: tuple[int, int], rate: str, color: str, duration: float, opacity: float = 0.75) -> Path:
    """Full-frame colour flashes with alpha (held keys, like opacity keyframes)."""
    if out.exists():
        return out
    w, h = canvas
    fps = float(Fraction(rate))
    rgb = [int(color.lstrip("#")[k:k + 2], 16) for k in (0, 2, 4)]
    n = max(1, round(duration * fps))
    proc = subprocess.Popen(["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{w}x{h}", "-r", rate,
                             "-i", "-", "-c:v", "png", "-pix_fmt", "rgba", str(out)], stdin=subprocess.PIPE)
    frame = np.empty((h, w, 4), np.uint8)
    frame[..., :3] = rgb
    for k in range(n):
        t = k / fps
        a = next((v for tt, v in reversed(STROBE) if t >= tt - 1e-6), 0.0)
        frame[..., 3] = int(255 * a * opacity)
        proc.stdin.write(frame.tobytes())
    proc.stdin.close()
    proc.wait()
    return out


def expand(edl: dict, edit_dir: Path, canvas: tuple[int, int], rate: str, brand: dict | None = None) -> tuple[list[dict], list[dict]]:
    """Beats → (behind-subject overlays, sfx items) for the render."""
    from graphics import output_time
    behind, sfx = [], []
    cfg = ((brand or {}).get("beats") or {}).get("denial") or {}
    color = cfg.get("color") or "#E0242B"
    sound = cfg.get("sfx")
    if not sound:
        try:
            from sfx_library import library_choices
            picks = library_choices("error")
            sound = picks[0]["file"] if picks else None
        except Exception:
            sound = None
    folder = edit_dir / "beats"
    folder.mkdir(exist_ok=True)
    for k, beat in enumerate(edl.get("beats") or []):
        if beat.get("type") != "denial":
            continue
        start = output_time(edl, beat["at"], rate) if "at" in beat else float(beat["start_in_output"])
        dur = float(beat.get("duration", 0.43))
        clip = strobe_clip(folder / f"denial_{color.lstrip('#')}_{round(dur * 1000)}.mov", canvas, rate, color, dur,
                           float(cfg.get("opacity", 0.75)))
        behind.append({"file": str(clip), "start_in_output": round(start, 4), "duration": dur, "x": 0, "y": 0,
                       "type": "beat", "text": beat.get("phrase", "denial")})
        if sound:
            sfx.append({"file": sound, "start_in_output": max(0.0, start - float(cfg.get("sfx_lead", 0.2))),
                        "gain_db": float(cfg.get("sfx_gain_db", -12)), "label": "denial"})
    return behind, sfx


def main() -> None:
    parser = argparse.ArgumentParser(description="Editorial beats")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("suggest", help="list denial lines from the transcripts")
    p.add_argument("edl", type=Path)
    args = parser.parse_args()
    edl_path = args.edl.resolve()
    print(json.dumps(suggest(json.loads(edl_path.read_text(encoding="utf-8")), edl_path.parent), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
