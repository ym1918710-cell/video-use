"""Speak a line in someone's own voice, through a local VoiceStudio.

VoiceStudio (github.com/debpalash/VoiceStudio, AGPL-3.0) is a separate desktop
app with a local HTTP API on 127.0.0.1:3900. video-use never vendors its code:
this file only talks to the running app, and every command fails cleanly when
the app is not installed. Nothing leaves the machine.

Uses: a pickup for a fluffed or missing word, a line the speaker forgot, a
scratch voice-over before the real recording, a dub test.

    python helpers/voice.py status
    python helpers/voice.py ref <video> --from 8 --to 20 -o edit/voice/ref.wav
    python helpers/voice.py say "<text>" --ref edit/voice/ref.wav -o edit/voice/line.wav
    python helpers/voice.py lines lines.txt --ref edit/voice/ref.wav --out-dir edit/voice/

Rules this file enforces:

- Arabic text must carry tashkeel (diacritics) written for the speaker's
  dialect, or the engine falls back to MSA pronunciation. Write English words
  the way the speaker says them, in Arabic letters ("إِس كْيُو إِل").
  `--allow-plain` overrides.
- Every output gets a `<out>.json` sidecar with the engine, model, its licence
  and whether that licence allows commercial use. `--commercial` refuses a
  non-commercial model (OmniVoice weights are CC-BY-NC).
- `--check` transcribes the result back and measures it, so the agent can see
  what was actually said.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

BASE = os.environ.get("VOICESTUDIO_URL", "http://127.0.0.1:3900").rstrip("/")
DEFAULT_LANGUAGE = "Auto"

ARABIC_LETTER = re.compile(r"[ء-ي]")
HARAKAT = re.compile(r"[ً-ْ]")


class VoiceError(RuntimeError):
    pass


# ---------- HTTP ----------

def _get(path: str, timeout: float = 15) -> Any:
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read())


def _post_json(path: str, body: dict, timeout: float = 60) -> Any:
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _post_form(path: str, fields: dict[str, str], files: dict[str, Path], timeout: float) -> tuple[str, bytes]:
    boundary = uuid.uuid4().hex
    parts: list[bytes] = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for k, p in files.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{p.name}"\r\n'
                     f"Content-Type: application/octet-stream\r\n\r\n".encode() + p.read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(BASE + path, data=b"".join(parts), method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.headers.get("Content-Type", ""), r.read()
    except urllib.error.HTTPError as e:
        raise VoiceError(_explain(e.read())) from None


def _explain(raw: bytes) -> str:
    try:
        d = json.loads(raw).get("detail")
    except Exception:
        return raw[:300].decode(errors="replace")
    if isinstance(d, dict):
        msg = d.get("message") or json.dumps(d)[:300]
        if d.get("code") == "model_licence_required":
            names = ", ".join(m.get("repo_id", "?") for m in d.get("models", []))
            msg += f" — a licence needs the user's own acceptance ({names}); ask them, never accept it unasked."
        return msg
    return str(d)[:300]


def up() -> bool:
    try:
        return _get("/health", timeout=3).get("status") == "ok"
    except Exception:
        return False


def ensure_up(wait_s: float = 180) -> None:
    """Start the app if it is installed but closed; fail clearly if it is not installed."""
    if up():
        return
    if not any(Path(p).exists() for p in ("/Applications/VoiceStudio.app", str(Path.home() / "Applications/VoiceStudio.app"))):
        raise VoiceError("VoiceStudio is not installed. Install: brew tap debpalash/tap && "
                         "brew install --cask debpalash/tap/voicestudio (ask the user first).")
    subprocess.run(["open", "-g", "-a", "VoiceStudio"], check=False)
    t = time.time()
    while time.time() - t < wait_s:
        if up():
            return
        time.sleep(3)
    raise VoiceError(f"VoiceStudio did not answer on {BASE} within {wait_s:.0f} s (first launch sets up Python; open the app and finish setup).")


# ---------- engines, models, licences ----------

def engines() -> dict:
    return _get("/engines/tts")


def models() -> list[dict]:
    d = _get("/models")
    return d if isinstance(d, list) else d.get("models", [])


def licence_for(engine_id: str) -> dict:
    """Licence of the model that engine runs on (the model catalogue lists engines per repo)."""
    eng = engines()
    active_model = eng.get("active_model") if eng.get("active") == engine_id else None
    rows = [m for m in models() if m.get("role") == "TTS" and engine_id in (m.get("engines") or [])]
    row = next((m for m in rows if m["repo_id"] == active_model), None) or next((m for m in rows if m.get("installed")), None) \
        or (rows[0] if rows else None)
    if not row:
        return {"model": None, "licence": "unknown", "category": "unknown", "commercial_use": False}
    la = row.get("license_acceptance") or {}
    lic = (row.get("license_info") or {}).get("license") or la.get("license") or "unknown"
    cat = la.get("category") or "unknown"
    return {"model": row["repo_id"], "licence": lic, "category": cat, "commercial_use": cat == "commercial"}


def status() -> dict:
    ensure_up()
    eng = engines()
    out = {"url": BASE, "health": _get("/health"), "active": eng.get("active"), "engines": []}
    for b in eng.get("backends", []):
        if not b.get("available"):
            continue
        langs = b.get("supported_language_names") or []
        out["engines"].append({"id": b["id"], "clone": bool(b.get("supports_cloning")),
                               "arabic": [l for l in langs if "arabic" in l][:12], "licence": licence_for(b["id"])})
    return out


# ---------- text ----------

def tashkeel_ratio(text: str) -> float | None:
    """Share of Arabic letters followed by a haraka. None when the text has no Arabic."""
    letters = ARABIC_LETTER.findall(text)
    if not letters:
        return None
    marked = sum(1 for i, ch in enumerate(text) if ARABIC_LETTER.match(ch) and i + 1 < len(text) and HARAKAT.match(text[i + 1]))
    return marked / len(letters)


def check_text(text: str, allow_plain: bool) -> None:
    r = tashkeel_ratio(text)
    if r is not None and r < 0.35 and not allow_plain:
        raise VoiceError(f"Arabic text is only {r:.0%} diacritised. Write it with tashkeel for the speaker's dialect "
                         "(Egyptian: إِنَّك تِكُون، دِلْوَقْتِي) and English words as he says them in Arabic letters; "
                         "or pass --allow-plain.")


# ---------- reference ----------

def cut_ref(video: str, start: float, end: float, out: Path) -> dict:
    """A clean mono 24 kHz reference of the speaker (6–20 s of speech alone works best)."""
    from listen import listen
    if not 3 <= end - start <= 20:
        raise VoiceError("a reference should be 3–20 s long (10–15 s of continuous speech is ideal)")
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{start}", "-to", f"{end}", "-i", video,
                    "-vn", "-ac", "1", "-ar", "24000", str(out)], check=True)
    d = listen(str(out))
    if not d.get("speech_like"):
        print(f"warning: the reference does not sound like clean speech ({d.get('shape')}); "
              "pick a stretch without music or effects", file=sys.stderr)
    return {"ref": str(out), "duration": d.get("duration"), "shape": d.get("shape")}


# ---------- speech ----------

def say(text: str, ref: Path, out: Path, *, engine: str | None = None, language: str = DEFAULT_LANGUAGE,
        speed: float = 1.0, ref_text: str = "", seed: int | None = None, allow_plain: bool = False,
        commercial: bool = False, check: bool = False) -> dict:
    ensure_up()
    check_text(text, allow_plain)
    eng = engine or engines().get("active")
    lic = licence_for(eng)
    if commercial and not lic["commercial_use"]:
        raise VoiceError(f"{eng} runs {lic['model']} ({lic['licence']}), which is not cleared for commercial use. "
                         "Pick an engine whose model is Apache/MIT, or drop --commercial for a test.")
    fields = {"text": text, "language": language, "speed": str(speed), "engine": eng}
    if ref_text:
        fields["ref_text"] = ref_text
    if seed is not None:
        fields["seed"] = str(seed)
    ctype, body = _post_form("/generate", fields, {"ref_audio": Path(ref)}, timeout=1200)
    if "audio" not in ctype and not body.startswith(b"RIFF"):
        raise VoiceError(_explain(body))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(body)
    meta: dict[str, Any] = {"text": text, "ref": str(ref), "engine": eng, "language": language, "speed": speed,
                            "seed": seed, **lic, "tashkeel": tashkeel_ratio(text)}
    if check:
        meta.update(verify(out, language))
    if not lic["commercial_use"]:
        print(f"note: {lic['model']} is {lic['licence']} — this line is for tests only, not client deliveries", file=sys.stderr)
    Path(str(out) + ".json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return meta


def verify(path: Path, language: str = DEFAULT_LANGUAGE) -> dict:
    """Transcribe the line back and measure it. ASR is weak on dialect + English: read it as a hint, the user's ear decides."""
    from listen import listen
    lang = "ar" if "arab" in language.lower() else (None if language == "Auto" else language)
    fields = {"model": "whisper-1"}
    if lang:
        fields["language"] = lang
    try:
        _, body = _post_form("/v1/audio/transcriptions", fields, {"file": path}, timeout=300)
        heard = json.loads(body).get("text", "")
    except Exception as e:  # transcription is a hint, never a blocker
        heard = f"(transcription failed: {e})"
    d = listen(str(path))
    return {"heard": heard, "duration": d.get("duration"), "peak_db": d.get("peak_db"), "speech_like": d.get("speech_like")}


# ---------- CLI ----------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="is VoiceStudio up; engines, Arabic support, licences")
    r = sub.add_parser("ref", help="cut a clean reference clip of the speaker")
    r.add_argument("video"); r.add_argument("--from", dest="start", type=float, required=True)
    r.add_argument("--to", dest="end", type=float, required=True); r.add_argument("-o", "--out", required=True)
    for name in ("say", "lines"):
        s = sub.add_parser(name)
        s.add_argument("text" if name == "say" else "file")
        s.add_argument("--ref", required=True)
        if name == "say":
            s.add_argument("-o", "--out", required=True)
        else:
            s.add_argument("--out-dir", required=True); s.add_argument("--prefix", default="line")
        s.add_argument("--engine"); s.add_argument("--language", default=DEFAULT_LANGUAGE)
        s.add_argument("--speed", type=float, default=1.0); s.add_argument("--ref-text", default="")
        s.add_argument("--seed", type=int); s.add_argument("--allow-plain", action="store_true")
        s.add_argument("--commercial", action="store_true", help="refuse models not cleared for commercial use")
        s.add_argument("--check", action="store_true", help="transcribe back and measure")
    a = ap.parse_args()
    try:
        if a.cmd == "status":
            res: Any = status()
        elif a.cmd == "ref":
            res = cut_ref(a.video, a.start, a.end, Path(a.out))
        else:
            kw = dict(engine=a.engine, language=a.language, speed=a.speed, ref_text=a.ref_text, seed=a.seed,
                      allow_plain=a.allow_plain, commercial=a.commercial, check=a.check)
            if a.cmd == "say":
                res = say(a.text, Path(a.ref), Path(a.out), **kw)
            else:
                lines = [l.strip() for l in Path(a.file).read_text(encoding="utf-8").splitlines() if l.strip()]
                for l in lines:  # fail on the text before spending any generation time
                    check_text(l, a.allow_plain)
                res = [say(l, Path(a.ref), Path(a.out_dir) / f"{a.prefix}_{i:02d}.wav", **kw) for i, l in enumerate(lines, 1)]
    except VoiceError as e:
        sys.exit(f"voice: {e}")
    print(json.dumps(res, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
