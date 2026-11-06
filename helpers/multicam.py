"""Multicam: sync angles by audio, then cut to whoever is speaking.

Workflow: edit the story on the MASTER source (the angle or recorder whose
transcript has speaker ids), then let this helper choose the picture::

    python helpers/multicam.py sync edit/edl.json            # writes offsets into edl["multicam"]
    python helpers/multicam.py switch edit/edl.json -o edit/edl_multicam.json

``edl["multicam"]``::

    {"master": "M",
     "angles": {"A": {"speaker": "speaker_0"}, "B": {"speaker": "speaker_1"}, "W": {"wide": true}},
     "offsets": {"A": 0.0, "B": 1.84, "W": -0.52},   # angle_time = master_time + offset
     "min_shot_s": 1.5, "max_shot_s": 7.0, "switch_delay_s": 0.15}

Every switched range keeps the master's continuous sound through
``audio_source``, so picture cuts never touch the dialogue.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

import numpy as np

SYNC_RATE = 4000


def _resolve(value: str, base: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def audio_envelope(path: Path, seconds: float = 240.0) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-t", f"{seconds}", "-i", str(path), "-vn", "-ac", "1",
                          "-ar", str(SYNC_RATE), "-f", "f32le", "-"], capture_output=True, check=True).stdout
    signal = np.frombuffer(raw, np.float32)
    # Onset-like envelope: robust to different mics and levels.
    hop = SYNC_RATE // 100
    frames = signal[: len(signal) // hop * hop].reshape(-1, hop)
    env = np.sqrt(np.mean(frames ** 2, axis=1))
    env = np.diff(np.log(env + 1e-4), prepend=0)
    env = np.clip(env, 0, None)
    return (env - env.mean()) / (env.std() + 1e-9)


def estimate_offset(master: np.ndarray, angle: np.ndarray, rate: float = 100.0) -> tuple[float, float]:
    """Seconds to add to master time to get angle time, plus a confidence."""
    n = 1
    while n < len(master) + len(angle):
        n *= 2
    corr = np.fft.irfft(np.fft.rfft(angle, n) * np.conj(np.fft.rfft(master, n)), n)
    lag = int(np.argmax(corr))
    if lag > n // 2:
        lag -= n
    peak = corr.max()
    confidence = float(peak / (np.sqrt(np.sum(master ** 2) * np.sum(angle ** 2)) + 1e-9))
    return lag / rate, confidence


def sync(edl: dict, base: Path) -> dict[str, float]:
    config = edl["multicam"]
    master_env = audio_envelope(_resolve(edl["sources"][config["master"]], base))
    offsets = {}
    for name in config["angles"]:
        if name == config["master"]:
            offsets[name] = 0.0
            continue
        offset, confidence = estimate_offset(master_env, audio_envelope(_resolve(edl["sources"][name], base)))
        offsets[name] = round(offset, 3)
        print(f"  {name}: offset {offset:+.3f}s  (confidence {confidence:.2f}{'  LOW — check by eye' if confidence < 0.2 else ''})")
    return offsets


def speaker_segments(words: list[dict], start: float, end: float) -> list[tuple[float, float, str | None]]:
    """(start, end, speaker) runs inside [start, end] from diarized words."""
    runs: list[list[Any]] = []
    for w in words:
        if w.get("type", "word") != "word" or w.get("start") is None:
            continue
        ws, we = max(start, float(w["start"])), min(end, float(w["end"]))
        if we <= ws:
            continue
        speaker = w.get("speaker_id")
        if runs and runs[-1][2] == speaker:
            runs[-1][1] = we
        else:
            runs.append([ws, we, speaker])
    return [tuple(r) for r in runs]


def plan_shots(segments: list[tuple[float, float, str | None]], start: float, end: float, config: dict) -> list[tuple[float, float, str]]:
    """Pick an angle per stretch: the speaker's camera, wide for overlaps and
    long monologues, never a shot shorter than ``min_shot_s``."""
    angles = config["angles"]
    by_speaker = {a.get("speaker"): name for name, a in angles.items() if a.get("speaker")}
    wide = next((name for name, a in angles.items() if a.get("wide")), None)
    default = wide or next(iter(angles))
    min_shot = float(config.get("min_shot_s", 1.5))
    max_shot = float(config.get("max_shot_s", 7.0))
    delay = float(config.get("switch_delay_s", 0.15))
    shots: list[list[Any]] = []
    for seg_start, seg_end, speaker in segments:
        angle = by_speaker.get(speaker, default)
        cut = seg_start + delay if shots else start
        if shots and shots[-1][2] == angle:
            continue
        if shots:
            shots[-1][1] = cut
        shots.append([cut, end, angle])
    if not shots:
        return [(start, end, default)]
    shots[0][0] = start
    shots[-1][1] = end
    # Merge flashes into the previous shot.
    merged: list[list[Any]] = []
    for shot in shots:
        if merged and (shot[1] - shot[0] < min_shot or shot[2] == merged[-1][2]):
            merged[-1][1] = shot[1]
        else:
            merged.append(shot)
    if len(merged) > 1 and merged[0][1] - merged[0][0] < min_shot:
        merged[1][0] = merged[0][0]
        merged.pop(0)
    # Break long single-angle stretches with the wide (or back again).
    final: list[tuple[float, float, str]] = []
    for s, e, angle in merged:
        if wide and angle != wide and e - s > max_shot + min_shot:
            mid = s + (e - s) * 0.55
            final += [(s, mid, angle), (mid, min(e, mid + max(min_shot, 2.0)), wide)]
            if e > mid + max(min_shot, 2.0):
                final.append((mid + max(min_shot, 2.0), e, angle))
        else:
            final.append((s, e, angle))
    return [(round(s, 3), round(e, 3), a) for s, e, a in final if e - s > 0.04]


def switch(edl: dict, base: Path) -> dict:
    config = edl["multicam"]
    master = config["master"]
    offsets = config.get("offsets") or {}
    from transcripts import transcript_path
    transcript = json.loads(transcript_path(base, edl, master).read_text(encoding="utf-8"))
    words = transcript.get("words", [])
    ranges = []
    for r in edl["ranges"]:
        if r["source"] != master:
            ranges.append(r)
            continue
        start, end = float(r["start"]), float(r["end"])
        shots = plan_shots(speaker_segments(words, start, end), start, end, config)
        for k, (s, e, angle) in enumerate(shots):
            off = float(offsets.get(angle, 0.0))
            item = {"source": angle, "start": round(s + off, 3), "end": round(e + off, 3),
                    "audio_source": {"source": master, "start": s}}
            if k == 0:
                item.update({key: v for key, v in r.items() if key not in {"source", "start", "end"}})
            ranges.append(item)
    out = {k: v for k, v in edl.items() if k != "ranges"}
    out["ranges"] = ranges
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Multicam sync and speaker switching")
    parser.add_argument("command", choices=["sync", "switch"])
    parser.add_argument("edl", type=Path)
    parser.add_argument("-o", "--output", type=Path)
    args = parser.parse_args()
    path = args.edl.resolve()
    edl = json.loads(path.read_text(encoding="utf-8"))
    if args.command == "sync":
        edl["multicam"]["offsets"] = sync(edl, path.parent)
        path.write_text(json.dumps(edl, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"offsets → {path}")
    else:
        out = (args.output or path.with_name(path.stem + "_multicam.json")).resolve()
        result = switch(edl, path.parent)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"{len(result['ranges'])} shot(s) → {out}")


if __name__ == "__main__":
    main()
