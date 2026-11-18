"""Listen to a sound: measure what it is, the way an editor hears it.

The agent cannot hear. This turns any audio (a sound effect, a music bed, a
window of dialogue, a whole mix) into numbers and a picture it can read:

- level: duration, sample peak (dBFS, what Premiere's meter shows), RMS,
  crest factor;
- envelope: attack (time to the peak), decay, where the energy sits — and a
  shape: hit, pop, whoosh, riser, swell-out, sustained, silence;
- colour: spectral centroid (dark ↔ bright), flatness (tonal ↔ noisy),
  pitch when tonal;
- events: onsets (each new hit), silent stretches, how much is speech-like;
- a picture: waveform over spectrogram (PNG) to look at.

    python helpers/listen.py file.wav                 # describe it
    python helpers/listen.py file.wav --png out.png   # + waveform/spectrogram image
    python helpers/listen.py file.mp4 --from 3.2 --to 4.0   # just a window
    python helpers/listen.py splits edit/edl.json     # J/L cuts the audio allows
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

SR = 22050
sys.path.insert(0, str(Path(__file__).resolve().parent))


def load(path: str | Path, start: float | None = None, end: float | None = None, sr: int = SR) -> np.ndarray:
    cmd = ["ffmpeg", "-v", "error"]
    if start is not None:
        cmd += ["-ss", f"{max(0.0, start):.4f}"]
    cmd += ["-i", str(path)]
    if end is not None:
        cmd += ["-t", f"{max(0.01, end - (start or 0.0)):.4f}"]
    cmd += ["-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"]
    raw = subprocess.run(cmd, capture_output=True).stdout
    return np.frombuffer(raw, np.float32).copy()


def _db(x: float) -> float:
    return round(20 * np.log10(max(float(x), 1e-9)), 1)


def _frames(a: np.ndarray, hop: int) -> np.ndarray:
    n = len(a) // hop
    return a[: n * hop].reshape(n, hop) if n else np.zeros((1, hop), np.float32)


def describe(a: np.ndarray, sr: int = SR) -> dict[str, Any]:
    """Numbers an editor would hear in this sound."""
    if not len(a):
        return {"duration": 0.0, "shape": "empty"}
    dur = len(a) / sr
    peak = float(np.max(np.abs(a)))
    rms = float(np.sqrt(np.mean(a ** 2)))
    hop = max(1, int(sr * 0.01))  # 10 ms envelope
    env = np.sqrt(np.mean(_frames(a, hop) ** 2, axis=1))
    t = np.arange(len(env)) * 0.01
    ipk = int(np.argmax(env))
    epk = float(env[ipk]) or 1e-9
    above = np.where(env >= epk * 0.5)[0]  # -6 dB around the peak
    attack = float(t[ipk])
    onset = float(t[above[0]]) if len(above) else 0.0
    after = np.where(env[ipk:] <= epk * 0.1)[0]  # -20 dB after the peak
    decay = float(after[0] * 0.01) if len(after) else float(dur - attack)
    loud = env > epk * 0.1
    body = float(loud.sum() * 0.01)
    # colour
    win = 1024
    spec = np.abs(np.fft.rfft(_frames(a, win) * np.hanning(win), axis=1)) + 1e-12
    freqs = np.fft.rfftfreq(win, 1 / sr)
    power = spec ** 2
    centroid = float(np.sum(power * freqs, axis=1).sum() / power.sum())
    flatness = float(np.mean(np.exp(np.mean(np.log(spec), axis=1)) / np.mean(spec, axis=1)))
    # onsets: positive spectral flux peaks
    flux = np.maximum(0, np.diff(np.log(spec), axis=0)).sum(axis=1)
    onsets: list[float] = []
    if len(flux) > 2:
        thr = np.median(flux) + 2.5 * np.std(flux)
        last = -1.0
        for i in range(1, len(flux) - 1):
            ti = (i + 1) * win / sr
            if flux[i] > thr and flux[i] >= flux[i - 1] and flux[i] >= flux[i + 1] and ti - last > 0.08:
                onsets.append(round(ti, 3))
                last = ti
    # pitch (only when tonal): autocorrelation over the loudest 100 ms
    pitch = None
    if flatness < 0.15:
        c = int(ipk * hop)
        seg = a[c: c + int(0.1 * sr)]
        if len(seg) > 400:
            ac = np.correlate(seg, seg, "full")[len(seg) - 1:]
            lo, hi = int(sr / 1000), int(sr / 60)
            if hi < len(ac):
                lag = lo + int(np.argmax(ac[lo:hi]))
                if ac[lag] > 0.3 * ac[0]:
                    pitch = round(sr / lag, 1)
    # speech-likeness: energy in the voice band, modulated at syllable rate (3–8 Hz)
    band = power[:, (freqs > 300) & (freqs < 3400)].sum(axis=1) / power.sum(axis=1)
    speechy = 0.0
    if len(env) > 100:
        m = np.abs(np.fft.rfft(env - env.mean()))
        mf = np.fft.rfftfreq(len(env), 0.01)
        speechy = float(m[(mf > 3) & (mf < 8)].sum() / (m[(mf > 0.5) & (mf < 20)].sum() + 1e-9))
    audible = np.where(env > epk * 0.03)[0]  # -30 dB: the whole audible sound, tails included
    lead_in = float(audible[0] * 0.01) if len(audible) else 0.0  # silence before the sound starts
    span = max(0.01, (float(audible[-1] * 0.01) - lead_in + 0.01) if len(audible) else dur)
    continuity = body / span  # speech and beds fill their span; chimes and beeps leave gaps
    speech = bool(np.mean(band) > 0.55 and speechy > 0.35 and flatness < 0.4 and span > 0.8 and continuity > 0.6)
    # separate bursts: loud runs split by ≥ 30 ms gaps
    bursts, prev = 0, False
    for v in loud:
        if v and not prev:
            bursts += 1
        prev = bool(v)
    # silences (below -50 dBFS, ≥ 150 ms)
    quiet = env < 10 ** (-50 / 20)
    silences, run = [], None
    for i, q in enumerate(quiet):
        if q and run is None:
            run = i
        if (not q or i == len(quiet) - 1) and run is not None:
            if (i - run) * 0.01 >= 0.15:
                silences.append([round(run * 0.01, 2), round(i * 0.01, 2)])
            run = None
    # shape
    pos = (attack - lead_in) / span if span else 0
    rel_onset = onset - lead_in
    if peak < 10 ** (-50 / 20):
        shape = "silence"
    elif speech:
        shape = "speech"
    elif bursts >= 2 and continuity < 0.85 and flatness < 0.3 and span >= 0.2:
        shape = "chime (separate tonal hits)" if decay > 0.12 else "beep/buzz (repeated bursts)"
    elif span < 0.3:
        shape = "pop/click"
    elif rel_onset < 0.04 and decay < 0.6:
        shape = "hit (instant attack, short tail)"
    elif rel_onset < 0.04:
        shape = "hit with long tail (boom/shine)"
    elif pos > 0.75:
        shape = "riser (builds to the end)"
    elif 0.15 < pos <= 0.75 and body < 2.5:
        shape = "whoosh/swell (rises then falls)"
    elif dur > 4 and body / dur > 0.7:
        shape = "sustained (bed / music / tone)"
    else:
        shape = "swell-out" if pos < 0.15 else "mixed"
    return {"duration": round(dur, 3), "peak_db": _db(peak), "rms_db": _db(rms), "crest_db": round(_db(peak) - _db(rms), 1),
            "attack_s": round(attack, 3), "onset_s": round(onset, 3), "decay_s": round(decay, 3), "body_s": round(body, 2),
            "brightness_hz": round(centroid), "tone": "noisy" if flatness > 0.3 else "tonal" if flatness < 0.1 else "mixed",
            "flatness": round(flatness, 3), "pitch_hz": pitch, "onsets": onsets[:20], "silences": silences[:20],
            "speech_like": speech, "shape": shape, "starts_after_s": round(lead_in, 3), "bursts": bursts,
            # where the sound "lands": line this point up with the picture event
            "hit_point_s": round(onset if rel_onset < 0.04 else attack, 3)}


@lru_cache(maxsize=512)
def listen(path: str, start: float | None = None, end: float | None = None) -> dict[str, Any]:
    return describe(load(path, start, end))


def picture(path: str | Path, out: Path, start: float | None = None, end: float | None = None) -> Path:
    """Waveform over spectrogram, for looking at a sound."""
    pre = (["-ss", f"{start:.3f}"] if start is not None else []) + ["-i", str(path)] + (
        ["-t", f"{end - (start or 0):.3f}"] if end is not None else [])
    subprocess.run(["ffmpeg", "-v", "error", "-y", *pre, "-filter_complex",
                    "[0:a]aformat=channel_layouts=mono,asplit[a][b];"
                    "[a]showwavespic=s=1200x200:colors=0x3FB7FF[w];"
                    "[b]showspectrumpic=s=1200x400:legend=1:scale=log:color=intensity[s];"
                    "[s]scale=1200:400[s2];[w][s2]vstack", "-frames:v", "1", str(out)], check=True)
    return out


def quiet_window(path: str, start: float, end: float, limit_db: float = -42.0) -> bool:
    """True when nothing audible (no speech, no other voice) is in [start, end)."""
    d = listen(path, start, end)
    return d.get("shape") in ("silence", "empty") or (d.get("peak_db", 0) < limit_db and not d.get("speech_like"))


def suggest_splits(edl: dict, edit_dir: Path, leads_ms: tuple[int, ...] = (320, 260, 200)) -> list[dict]:
    """Where a J-cut can soften a jump cut: the next line is *heard* just
    before its picture. A J-cut moves the sound's cut point earlier, so it is
    safe only when both sides of that stretch are quiet — the tail of the
    previous line (no last syllable clipped) and the gap before the new line
    in its take (no breath noise, no director's voice) — and only useful when
    the new line's first word then starts before the picture cut. Returns
    proposals; nothing is applied."""
    from render import resolve_path
    import json as _json
    from transcripts import transcript_path
    ranges = edl.get("ranges") or []
    out = []
    for i in range(1, len(ranges)):
        r, p = ranges[i], ranges[i - 1]
        if r.get("broll") or p.get("broll") or r.get("outro") or isinstance(r.get("audio_source"), dict) \
                or isinstance(p.get("audio_source"), dict) or r.get("j_cut_ms") or r.get("l_cut_ms"):
            continue
        src = str(resolve_path(edl["sources"][r["source"]], edit_dir))
        prev_src = str(resolve_path(edl["sources"][p["source"]], edit_dir))
        a, prev_end = float(r["start"]), float(p["end"])
        first_word = a
        try:
            words = _json.loads(transcript_path(edit_dir, edl, r["source"]).read_text(encoding="utf-8")).get("words", [])
            first_word = min((float(w["start"]) for w in words if w.get("type", "word") == "word" and float(w["start"]) >= a - 0.02), default=a)
        except Exception:
            pass
        verdict = None
        for lead_ms in leads_ms:
            lead = lead_ms / 1000
            if a < lead or first_word - a >= lead - 0.05:
                continue  # the first word would still start after the picture cut: no audible J
            extend = 0
            if not quiet_window(prev_src, prev_end - lead, prev_end):
                # no room inside the previous line: hold its picture a little longer
                # (the take continues, silent) and start the new line under it
                if quiet_window(prev_src, prev_end, prev_end + lead):
                    extend = lead_ms
                else:
                    verdict = verdict or f"not safe: the previous line still sounds in its last {lead_ms} ms and right after"
                    continue
            if not quiet_window(src, a - lead, a):
                d = listen(src, a - lead, a)
                verdict = verdict or f"not safe: {d.get('shape')} at {d.get('peak_db')} dB before the line (another voice?)"
                continue
            pr = {"range": i, "j_cut_ms": lead_ms, "why": f"first word heard {round(lead - (first_word - a), 2)} s before the picture; both sides quiet"}
            if extend:
                pr["extend_prev_end_ms"] = extend
                pr["why"] += f" (hold the previous shot {extend} ms longer: +{extend / 1000:.2f} s to the cut)"
            out.append(pr)
            break
        else:
            out.append({"range": i, "j_cut_ms": 0, "why": verdict or "no room: the cut is tight on both lines"})
    return out


def main() -> None:
    if len(sys.argv) > 2 and sys.argv[1] == "splits":
        edl_path = Path(sys.argv[2]).resolve()
        edl = json.loads(edl_path.read_text(encoding="utf-8"))
        props = suggest_splits(edl, edl_path.parent)
        for pr in props:
            ext = f" extend_prev_end_ms={pr['extend_prev_end_ms']}" if pr.get("extend_prev_end_ms") else ""
            print(f"range {pr['range']:2d}: j_cut_ms={pr['j_cut_ms']:3d}{ext}  {pr['why']}")
        return
    ap = argparse.ArgumentParser(description="Listen to a sound (numbers + optional picture)")
    ap.add_argument("file")
    ap.add_argument("--from", dest="start", type=float)
    ap.add_argument("--to", dest="end", type=float)
    ap.add_argument("--png", type=Path)
    args = ap.parse_args()
    print(json.dumps(listen(args.file, args.start, args.end), ensure_ascii=False, indent=1))
    if args.png:
        print("picture →", picture(args.file, args.png, args.start, args.end))


if __name__ == "__main__":
    main()
