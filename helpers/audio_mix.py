"""Senior audio post for Video Use: dialogue chain, music bed with
speech-aware ducking, SFX layer, and delivered stems.

All levels are *relative to the dialogue*: a music bed sits ``bed_lu`` below
the measured dialogue loudness while someone speaks and rises to ``gap_lu``
in real pauses. Speech is detected from the rendered dialogue master itself,
so ducking follows the edit exactly and never pumps on breaths.

EDL::

    "audio": {
      "dialogue": {"preset": "clean", "denoise": false},
      "music": [{"file": "music/bed.mp3", "source_start": 12.0,
                 "start_in_output": 0, "end_in_output": null,
                 "bed_lu": -20, "gap_lu": -9, "fade_in_ms": 800,
                 "fade_out_ms": 2000, "loop": true}],
      "sfx": [{"file": "sfx/pop.wav", "start_in_output": 3.2, "gain_db": -8}]
    }

Only files the user supplied or approved are used; nothing is fetched.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import numpy as np

AUDIO_RATE = 48000
ENV_RATE = 1000  # gain envelope resolution (1 ms)
DIALOGUE_PRESETS = {
    "none": "",
    "clean": "highpass=f=80,acompressor=threshold=-22dB:ratio=2.5:attack=12:release=180:makeup=1.5",
    "broadcast": ("highpass=f=90,deesser=i=0.35,"
                  "acompressor=threshold=-24dB:ratio=4:attack=8:release=150:makeup=3"),
}
MUSIC_DEFAULTS = {"bed_lu": -20.0, "gap_lu": -9.0, "fade_in_ms": 800.0, "fade_out_ms": 2000.0,
                  "attack_ms": 180.0, "release_ms": 450.0, "min_gap_ms": 700.0, "loop": True,
                  "source_start": 0.0, "gain_db": 0.0}
REFERENCE_DIALOGUE_LUFS = -14.0


def _resolve(value: str, base: Path) -> Path:
    if value.startswith("library:"):
        from library import resolve
        return resolve(value)
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.strip()[-600:]}")


# ---------------------------------------------------------------- analysis

def measure_lufs(path: Path, start: float = 0.0, duration: float | None = None) -> float | None:
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{start:.3f}"]
    if duration:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-i", str(path), "-vn", "-af", "loudnorm=print_format=json", "-f", "null", "-"]
    stderr = subprocess.run(cmd, capture_output=True, text=True, errors="replace").stderr
    match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", stderr, re.S)
    if not match:
        return None
    try:
        value = float(json.loads(match.group(0))["input_i"])
    except (ValueError, KeyError, json.JSONDecodeError):
        return None
    return value if value > -70 else None


def read_mono(path: Path, rate: int = 16000) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(rate),
                          "-f", "f32le", "-"], check=True, capture_output=True).stdout
    return np.frombuffer(raw, np.float32)


def speech_regions(samples: np.ndarray, rate: int = 16000, frame_ms: float = 20.0,
                   merge_gap: float = 0.25, min_len: float = 0.08) -> list[tuple[float, float]]:
    """Voice activity from level: frames above an adaptive threshold."""
    hop = max(1, int(rate * frame_ms / 1000))
    count = len(samples) // hop
    if count == 0:
        return []
    frames = samples[: count * hop].reshape(count, hop)
    rms_db = 20 * np.log10(np.sqrt(np.mean(frames ** 2, axis=1)) + 1e-9)
    loud = np.percentile(rms_db, 90)
    if loud < -55:
        return []
    active = rms_db > max(-50.0, loud - 20.0)
    regions: list[list[float]] = []
    step = hop / rate
    for index in np.flatnonzero(active):
        t = index * step
        if regions and t - regions[-1][1] <= merge_gap:
            regions[-1][1] = t + step
        else:
            regions.append([t, t + step])
    return [(round(a, 3), round(b, 3)) for a, b in regions if b - a >= min_len]


def duck_shape(duration: float, regions: list[tuple[float, float]], attack: float, release: float,
               min_gap: float, rate: int = ENV_RATE) -> np.ndarray:
    """0 = open (pause), 1 = fully ducked (speech); linear ramps around speech.

    Pauses shorter than ``min_gap`` stay ducked so the music does not bob
    between words.
    """
    merged: list[list[float]] = []
    for start, end in sorted(regions):
        if merged and start - merged[-1][1] < min_gap:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    n = max(1, int(round(duration * rate)))
    t = np.arange(n) / rate
    shape = np.zeros(n, np.float32)
    for start, end in merged:
        rise = np.clip((t - (start - attack)) / max(attack, 1e-3), 0, 1)
        fall = np.clip(1 - (t - end) / max(release, 1e-3), 0, 1)
        shape = np.maximum(shape, np.where(t < start, rise, np.where(t > end, fall, 1.0)))
    return shape


# ---------------------------------------------------------------- stems

def process_dialogue(dialogue: Path, config: dict | None, out: Path) -> Path:
    config = config or {}
    preset = config.get("preset", "none")
    chain = DIALOGUE_PRESETS.get(preset)
    if chain is None:
        raise ValueError(f"unknown dialogue preset {preset!r}")
    filters = [f for f in (chain,) if f]
    if config.get("denoise"):
        filters.insert(0, f"afftdn=nr={float(config.get('denoise_db', 10)):.1f}:nf=-50:tn=1")
    if not filters:
        return dialogue
    _run(["ffmpeg", "-y", "-v", "error", "-i", str(dialogue), "-af", ",".join(filters),
          "-ar", str(AUDIO_RATE), "-ac", "2", "-c:a", "pcm_f32le", str(out)])
    print(f"  dialogue chain: {preset}{' + denoise' if config.get('denoise') else ''}")
    return out


def render_music_cue(cue: dict, total: float, regions: list[tuple[float, float]], dialogue_lufs: float | None,
                     base: Path, workdir: Path, index: int) -> Path:
    c = {**MUSIC_DEFAULTS, **cue}
    src = _resolve(c["file"], base)
    start = float(c.get("start_in_output") or 0.0)
    end = float(c["end_in_output"]) if c.get("end_in_output") is not None else total
    end = min(end, total)
    duration = end - start
    if duration <= 0:
        raise ValueError(f"music[{index}] has no duration inside the output")
    music_lufs = measure_lufs(src, float(c["source_start"]), None) or -14.0
    reference = dialogue_lufs if dialogue_lufs is not None else REFERENCE_DIALOGUE_LUFS
    local = [(max(0.0, a - start), b - start) for a, b in regions if b > start and a < end]
    shape = duck_shape(duration, local, c["attack_ms"] / 1000, c["release_ms"] / 1000, c["min_gap_ms"] / 1000)
    level_db = (reference - music_lufs + float(c["gain_db"])
                + float(c["gap_lu"]) + (float(c["bed_lu"]) - float(c["gap_lu"])) * shape)
    gain = np.power(10.0, level_db / 20.0).astype(np.float32)
    env_path = workdir / f"music_{index:02d}_env.f32"
    gain.tofile(env_path)
    out = workdir / f"music_{index:02d}.wav"
    fade_in = min(duration / 2, float(c["fade_in_ms"]) / 1000)
    fade_out = min(duration / 2, float(c["fade_out_ms"]) / 1000)
    inputs = []
    if c["loop"]:
        inputs += ["-stream_loop", "-1"]
    inputs += ["-ss", f"{float(c['source_start']):.3f}", "-i", str(src),
               "-f", "f32le", "-ar", str(ENV_RATE), "-ac", "1", "-i", str(env_path)]
    delay = int(round(start * AUDIO_RATE))
    graph = (
        f"[0:a]aresample={AUDIO_RATE},aformat=sample_fmts=flt:channel_layouts=stereo,"
        f"atrim=0:{duration:.6f},asetpts=N/SR/TB"
        + (f",afade=t=in:st=0:d={fade_in:.3f}" if fade_in > 0 else "")
        + (f",afade=t=out:st={duration - fade_out:.3f}:d={fade_out:.3f}" if fade_out > 0 else "")
        + "[m];"
        f"[1:a]aresample={AUDIO_RATE},pan=stereo|c0=c0|c1=c0,aformat=sample_fmts=flt,atrim=0:{duration:.6f}[e];"
        f"[m][e]amultiply,adelay=delays={delay}S:all=1,apad=whole_dur={total:.6f},atrim=0:{total:.6f}[out]"
    )
    _run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", graph, "-map", "[out]",
          "-ar", str(AUDIO_RATE), "-c:a", "pcm_f32le", str(out)])
    print(f"  music[{index}] {src.name}: {start:.2f}-{end:.2f}s  bed {c['bed_lu']:+.0f} LU / gaps {c['gap_lu']:+.0f} LU"
          f"  (track {music_lufs:.1f} LUFS)")
    return out


def render_sfx(items: list[dict], total: float, base: Path, out: Path, at_resolver=None) -> Path:
    inputs: list[str] = []
    graph: list[str] = []
    for index, item in enumerate(items):
        start = float(item["start_in_output"]) if "start_in_output" in item else at_resolver(item["at"])
        inputs += ["-i", str(_resolve(item["file"], base))]
        graph.append(f"[{index}:a]aresample={AUDIO_RATE},aformat=sample_fmts=flt:channel_layouts=stereo,"
                     f"volume={float(item.get('gain_db', -6)):.2f}dB,"
                     f"adelay=delays={int(round(start * AUDIO_RATE))}S:all=1[s{index}]")
    labels = "".join(f"[s{i}]" for i in range(len(items)))
    graph.append(f"{labels}amix=inputs={len(items)}:normalize=0:duration=longest,"
                 f"apad=whole_dur={total:.6f},atrim=0:{total:.6f}[out]")
    _run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", ";".join(graph),
          "-map", "[out]", "-ar", str(AUDIO_RATE), "-c:a", "pcm_f32le", str(out)])
    print(f"  sfx: {len(items)} hit(s)")
    return out


def mix(stems: list[Path], total: float, out: Path) -> Path:
    if len(stems) == 1:
        return stems[0]
    inputs = [arg for stem in stems for arg in ("-i", str(stem))]
    labels = "".join(f"[{i}:a]" for i in range(len(stems)))
    _run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex",
          f"{labels}amix=inputs={len(stems)}:normalize=0:duration=longest,atrim=0:{total:.6f}[out]",
          "-map", "[out]", "-ar", str(AUDIO_RATE), "-c:a", "pcm_f32le", str(out)])
    return out


def build_mix(edl: dict, edit_dir: Path, dialogue: Path, total: float, at_resolver=None) -> Path:
    """Dialogue master → (chain) + music + sfx → mixed WAV; stems in <edit>/stems/."""
    audio = edl.get("audio") or {}
    music = audio.get("music") or []
    sfx = audio.get("sfx") or []
    if not (music or sfx or audio.get("dialogue")):
        return dialogue
    stems_dir = edit_dir / "stems"
    stems_dir.mkdir(parents=True, exist_ok=True)
    print("audio post (dialogue / music / sfx)")
    voice = process_dialogue(dialogue, audio.get("dialogue"), stems_dir / "dialogue.wav")
    if voice == dialogue:
        _run(["ffmpeg", "-y", "-v", "error", "-i", str(dialogue), "-c:a", "pcm_f32le", str(stems_dir / "dialogue.wav")])
        voice = stems_dir / "dialogue.wav"
    stems = [voice]
    if music:
        regions = speech_regions(read_mono(voice))
        dialogue_lufs = measure_lufs(voice)
        cues = [render_music_cue(cue, total, regions, dialogue_lufs, edit_dir, stems_dir, i) for i, cue in enumerate(music)]
        stems.append(mix(cues, total, stems_dir / "music.wav") if len(cues) > 1 else cues[0])
        if len(cues) == 1:
            cues[0].replace(stems_dir / "music.wav")
            stems[-1] = stems_dir / "music.wav"
    if sfx:
        stems.append(render_sfx(sfx, total, edit_dir, stems_dir / "sfx.wav", at_resolver))
    return mix(stems, total, stems_dir / "mix.wav")


def validate_audio(edl: dict, base: Path, total: float) -> list[tuple[str, str, str, str]]:
    issues: list[tuple[str, str, str, str]] = []
    audio = edl.get("audio") or {}
    if not isinstance(audio, dict):
        return issues
    dialogue = audio.get("dialogue")
    if dialogue is not None:
        if not isinstance(dialogue, dict) or dialogue.get("preset", "none") not in DIALOGUE_PRESETS:
            issues.append(("error", "invalid_dialogue_chain", f"audio.dialogue.preset must be one of {sorted(DIALOGUE_PRESETS)}.", "audio.dialogue"))
    for key in ("music", "sfx"):
        for index, item in enumerate(audio.get(key) or []):
            path = f"audio.{key}[{index}]"
            if not isinstance(item, dict) or not isinstance(item.get("file"), str):
                issues.append(("error", "invalid_audio_item", "Needs a file.", path))
                continue
            try:
                exists = _resolve(item["file"], base).is_file()
            except FileNotFoundError:
                exists = False
            if not exists:
                issues.append(("error", "audio_file_missing", f"{item['file']} not found.", f"{path}.file"))
            try:
                start = float(item.get("start_in_output", 0) or 0)
            except (TypeError, ValueError):
                issues.append(("error", "invalid_audio_time", "start_in_output must be a number.", path))
                continue
            if key == "sfx" and "start_in_output" not in item and not isinstance(item.get("at"), dict):
                issues.append(("error", "invalid_audio_time", "Give start_in_output or at: {source, time}.", path))
            if start < 0 or start > total:
                issues.append(("error", "audio_outside_output", "Starts outside the output timeline.", path))
            if key == "music":
                for field in ("bed_lu", "gap_lu"):
                    if field in item and not -40 <= float(item[field]) <= 0:
                        issues.append(("error", "invalid_music_level", f"{field} must be between -40 and 0 LU.", f"{path}.{field}"))
                if float(item.get("bed_lu", MUSIC_DEFAULTS["bed_lu"])) > -12:
                    issues.append(("warning", "music_bed_loud", "A bed above -12 LU competes with speech.", f"{path}.bed_lu"))
    return issues
