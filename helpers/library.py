"""A user-owned music & SFX library with analysis and mood tags.

Video Use never fetches tracks. The user drops licensed files in a folder;
this helper analyzes them (duration, loudness, tempo, energy, brightness),
keeps their mood tags and licence, and answers "what fits this edit?".

    python helpers/library.py scan ~/Music/licensed --type music --license "Artlist #123"
    python helpers/library.py tag <id> --mood uplifting,tech,corporate
    python helpers/library.py search --type music --mood uplifting --energy high --min-duration 30
    python helpers/library.py suggest --recipe kinetic_promo --duration 45

The index lives at $VIDEO_USE_LIBRARY (default ~/video-use-library/library.json).
In an EDL, a music or SFX ``file`` may be ``"library:<id>"``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import numpy as np

AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".aif", ".aiff", ".ogg"}
ANALYSIS_RATE = 11025

# What each style recipe usually wants from a music bed (style-library.md).
RECIPE_PROFILES = {
    "kinetic_promo": {"energy": "high", "tempo": (105, 140), "moods": ["uplifting", "energetic", "tech", "confident"]},
    "static_qa": {"energy": "calm", "tempo": (70, 105), "moods": ["ambient", "minimal", "warm"]},
    "clean_authority": {"energy": "calm", "tempo": (70, 110), "moods": ["corporate", "minimal", "confident"]},
    "native_short_form": {"energy": "high", "tempo": (95, 135), "moods": ["playful", "trendy", "energetic"]},
    "documentary": {"energy": "medium", "tempo": (60, 100), "moods": ["emotional", "cinematic", "warm"]},
    "product_demo": {"energy": "medium", "tempo": (90, 120), "moods": ["tech", "clean", "positive"]},
    "cinematic_montage": {"energy": "medium", "tempo": (80, 130), "moods": ["cinematic", "epic", "emotional"]},
}


def library_path() -> Path:
    return Path(os.environ.get("VIDEO_USE_LIBRARY", Path.home() / "video-use-library" / "library.json")).expanduser()


def load(path: Path | None = None) -> dict[str, Any]:
    path = path or library_path()
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"version": 1, "items": {}}


def save(library: dict[str, Any], path: Path | None = None) -> Path:
    path = path or library_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(library, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def resolve(value: str, base: Path | None = None) -> Path:
    """``library:<id>`` → file path; anything else is a normal path."""
    if value.startswith("library:"):
        item = load()["items"].get(value.split(":", 1)[1])
        if not item:
            raise FileNotFoundError(f"{value} is not in the library ({library_path()})")
        return Path(item["file"])
    path = Path(value).expanduser()
    return path if path.is_absolute() or base is None else (base / path).resolve()


def read_audio(path: Path, seconds: float = 90.0) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-t", f"{seconds}", "-i", str(path), "-vn", "-ac", "1",
                          "-ar", str(ANALYSIS_RATE), "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32)


def onset_envelope(samples: np.ndarray, rate: int = ANALYSIS_RATE, hop_s: float = 0.01) -> np.ndarray:
    hop = int(rate * hop_s)
    frames = samples[: len(samples) // hop * hop].reshape(-1, hop)
    energy = np.log(np.sqrt(np.mean(frames ** 2, axis=1)) + 1e-5)
    return np.clip(np.diff(energy, prepend=energy[:1]), 0, None)


def estimate_bpm(envelope: np.ndarray, hop_s: float = 0.01) -> float | None:
    if len(envelope) < 400 or envelope.std() < 1e-6:
        return None
    env = envelope - envelope.mean()
    corr = np.correlate(env, env, mode="full")[len(env) - 1:]
    lags = np.arange(len(corr))
    bpm = 60.0 / (np.maximum(lags, 1) * hop_s)
    mask = (bpm >= 60) & (bpm <= 180)
    if not mask.any():
        return None
    best = lags[mask][np.argmax(corr[mask])]
    return round(60.0 / (best * hop_s), 1)


def analyze(path: Path) -> dict[str, Any]:
    from audio_mix import measure_lufs
    samples = read_audio(path)
    duration = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                     "-of", "csv=p=0", str(path)], capture_output=True, text=True, errors="replace").stdout.strip() or 0)
    env = onset_envelope(samples)
    spectrum = np.abs(np.fft.rfft(samples[: ANALYSIS_RATE * 30] if len(samples) else np.zeros(1)))
    freqs = np.fft.rfftfreq(min(len(samples), ANALYSIS_RATE * 30) or 1, 1 / ANALYSIS_RATE)
    centroid = float((spectrum * freqs).sum() / (spectrum.sum() + 1e-9)) if len(spectrum) > 1 else 0.0
    onset_rate = float((env > env.mean() + 2 * env.std()).sum() / max(1.0, len(env) * 0.01)) if len(env) else 0.0
    lufs = measure_lufs(path)
    bpm = None
    try:  # librosa is a project dependency and tracks tempo better
        import librosa
        tempo, _beats = librosa.beat.beat_track(y=samples.astype(np.float32), sr=ANALYSIS_RATE)
        bpm = round(float(np.atleast_1d(tempo)[0]), 1) or None
    except Exception:
        pass
    bpm = bpm or estimate_bpm(env)
    energy = "high" if onset_rate > 3.0 or (lufs or -30) > -11 else "calm" if onset_rate < 1.2 else "medium"
    return {
        "duration_s": round(duration, 2), "lufs": lufs, "bpm": bpm, "energy": energy,
        "brightness": "bright" if centroid > 2200 else "dark" if centroid < 900 else "neutral",
        "onsets_per_s": round(onset_rate, 2),
        "tempo": None if bpm is None else ("slow" if bpm < 90 else "fast" if bpm > 125 else "mid"),
    }


def item_id(path: Path) -> str:
    return hashlib.sha1(str(path.resolve()).encode()).hexdigest()[:10]


def scan(folder: Path, kind: str, license_note: str | None, library: dict[str, Any]) -> list[str]:
    added = []
    for path in sorted(p for p in folder.rglob("*") if p.suffix.lower() in AUDIO_EXTS and not p.name.startswith("._")):
        key = item_id(path)
        item = library["items"].get(key, {"moods": [], "notes": ""})
        item.update({"id": key, "file": str(path.resolve()), "name": path.stem, "type": kind, **analyze(path)})
        if kind == "sfx":
            from sfx_library import classify
            item["role"] = item.get("role") or classify(path.stem)
        if license_note:
            item["license"] = license_note
        library["items"][key] = item
        added.append(key)
        print(f"  {key}  {kind:<5} {item['duration_s']:6.1f}s  {item['bpm'] or '-':>6} bpm  {item['energy']:<6} {path.name}")
    return added


def search(library: dict[str, Any], kind: str | None = None, moods: list[str] | None = None,
           energy: str | None = None, tempo: tuple[float, float] | None = None,
           min_duration: float | None = None) -> list[dict[str, Any]]:
    results = []
    for item in library["items"].values():
        if kind and item.get("type") != kind:
            continue
        if min_duration and item.get("duration_s", 0) < min_duration:
            continue
        score = 0.0
        if moods:
            overlap = len(set(m.lower() for m in moods) & set(m.lower() for m in item.get("moods", [])))
            score += 2.0 * overlap
        if energy:
            score += 1.5 if item.get("energy") == energy else 0.0
        if tempo and item.get("bpm"):
            low, high = tempo
            bpm = float(item["bpm"])
            # Half/double time counts as fitting.
            score += 1.0 if any(low <= b <= high for b in (bpm, bpm / 2, bpm * 2)) else 0.0
        if not item.get("license"):
            score -= 0.5  # unknown licence: still listed, ranked lower
        results.append({**item, "score": round(score, 2)})
    return sorted(results, key=lambda i: -i["score"])


def suggest(library: dict[str, Any], recipe: str, duration: float | None = None) -> list[dict[str, Any]]:
    profile = RECIPE_PROFILES.get(recipe)
    if profile is None:
        raise ValueError(f"unknown recipe {recipe!r}; known: {sorted(RECIPE_PROFILES)}")
    return search(library, "music", profile["moods"], profile["energy"], profile["tempo"],
                  min_duration=(duration * 0.6 if duration else None))


def main() -> None:
    parser = argparse.ArgumentParser(description="Music & SFX library")
    sub = parser.add_subparsers(dest="command", required=True)
    p_scan = sub.add_parser("scan")
    p_scan.add_argument("folder", type=Path)
    p_scan.add_argument("--type", choices=["music", "sfx"], required=True)
    p_scan.add_argument("--license")
    p_tag = sub.add_parser("tag")
    p_tag.add_argument("id")
    p_tag.add_argument("--mood", default="")
    p_tag.add_argument("--license")
    p_tag.add_argument("--notes")
    p_search = sub.add_parser("search")
    p_search.add_argument("--type", choices=["music", "sfx"])
    p_search.add_argument("--mood", default="")
    p_search.add_argument("--energy", choices=["calm", "medium", "high"])
    p_search.add_argument("--bpm", help="e.g. 100-130")
    p_search.add_argument("--min-duration", type=float)
    p_suggest = sub.add_parser("suggest")
    p_suggest.add_argument("--recipe", required=True, choices=sorted(RECIPE_PROFILES))
    p_suggest.add_argument("--duration", type=float)
    args = parser.parse_args()

    library = load()
    if args.command == "scan":
        added = scan(args.folder.expanduser(), args.type, args.license, library)
        print(f"{len(added)} item(s) → {save(library)}")
        if not args.license:
            print("note: no --license given; record where each track is licensed from before delivery.")
        return
    if args.command == "tag":
        item = library["items"][args.id]
        if args.mood:
            item["moods"] = sorted(set(item.get("moods", [])) | {m.strip().lower() for m in args.mood.split(",") if m.strip()})
        if args.license:
            item["license"] = args.license
        if args.notes:
            item["notes"] = args.notes
        save(library)
        print(json.dumps(item, ensure_ascii=False, indent=2))
        return
    if args.command == "search":
        tempo = tuple(float(v) for v in args.bpm.split("-")) if args.bpm else None
        results = search(library, args.type, [m for m in args.mood.split(",") if m], args.energy, tempo, args.min_duration)
    else:
        results = suggest(library, args.recipe, args.duration)
    for item in results[:10]:
        print(f"{item['score']:5.1f}  library:{item['id']}  {item['type']:<5} {item['duration_s']:6.1f}s "
              f"{item.get('bpm') or '-':>6} bpm  {item['energy']:<6} {','.join(item.get('moods', [])) or '-':<28} "
              f"{item.get('license') or 'LICENSE?'}  {item['name']}")


if __name__ == "__main__":
    main()
