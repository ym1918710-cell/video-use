"""Hand the edit to an NLE: CMX3600 EDL, FCP7 XML, and a full Premiere project.

- ``.edl`` / ``.xml``: the editorial decision (sources, in/out, order) for
  relinking camera originals in Premiere, Resolve or Avid.
- ``--premiere``: the *finished* edit rebuilt layer by layer from the last
  render (``render_manifest.json``), as an FCP7 XML that Premiere opens as a
  project:

  V1  rendered picture, cut at every edit (zoom, framing, B-roll, transitions exact)
  V2  layers behind the subject       V3  subject cut-out (person matte) above them
  V4  graphics                         V5  captions (alpha)
  V6  source clips (disabled: re-edit / relink from the camera files)
  A1  processed dialogue    A2  one clip per sound effect, at its level
  A3  source dialogue per range (disabled)

  Every overlay is laid out full-frame (so no Motion maths can drift), and a
  matching ``.srt`` is written for Premiere's caption track.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from fractions import Fraction
from pathlib import Path
from urllib.parse import quote
from xml.sax.saxutils import escape


def _resolve(value: str, base: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def probe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                         capture_output=True, text=True)
    try:
        return json.loads(out.stdout or "{}")
    except json.JSONDecodeError:
        return {}


def timebase(rate: str) -> tuple[int, bool]:
    """Integer timebase + NTSC flag (29.97 → 30, True)."""
    value = float(Fraction(rate))
    base = round(value)
    return base, abs(value - base) > 0.01


def source_tc_frames(info: dict, base: int) -> int:
    """Embedded start timecode (camera files) in frames; 0 when absent."""
    tags = dict(info.get("format", {}).get("tags", {}))
    for stream in info.get("streams", []):
        tags.update(stream.get("tags", {}))
    tc = tags.get("timecode")
    if not tc:
        return 0
    parts = tc.replace(";", ":").split(":")
    try:
        h, m, s, f = (int(p) for p in parts)
    except ValueError:
        return 0
    return ((h * 60 + m) * 60 + s) * base + f


def tc(frames: int, base: int) -> str:
    f = frames % base
    s = frames // base
    return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}:{f:02d}"


def reel_name(name: str, used: dict[str, str]) -> str:
    if name not in used:
        cleaned = "".join(ch for ch in name.upper() if ch.isalnum())[:8] or f"R{len(used) + 1}"
        while cleaned in used.values():
            cleaned = (cleaned[:6] + f"{len(used):02d}")[:8]
        used[name] = cleaned
    return used[name]


def cmx3600(edl: dict, base_dir: Path, rate: str, title: str) -> str:
    base, ntsc = timebase(rate)
    lines = [f"TITLE: {title}", "FCM: NON-DROP FRAME", ""]
    record = 0
    reels: dict[str, str] = {}
    infos: dict[str, dict] = {}
    for index, r in enumerate(edl["ranges"], start=1):
        name = r["source"]
        path = _resolve(edl["sources"][name], base_dir)
        infos.setdefault(name, probe(path))
        offset = source_tc_frames(infos[name], base)
        src_in = offset + round(float(r["start"]) * base)
        src_out = offset + round(float(r["end"]) * base)
        length = src_out - src_in
        lines.append(f"{index:03d}  {reel_name(name, reels):<8} B     C        "
                     f"{tc(src_in, base)} {tc(src_out, base)} {tc(record, base)} {tc(record + length, base)}")
        lines.append(f"* FROM CLIP NAME: {path.name}")
        lines.append("")
        record += length
    return "\n".join(lines) + "\n"


def fcp7_xml(edl: dict, base_dir: Path, rate: str, title: str, canvas: tuple[int, int]) -> str:
    base, ntsc = timebase(rate)
    rate_xml = f"<rate><timebase>{base}</timebase><ntsc>{'TRUE' if ntsc else 'FALSE'}</ntsc></rate>"
    files: dict[str, str] = {}
    video_items, audio_items = [], []
    record = 0
    for index, r in enumerate(edl["ranges"], start=1):
        name = r["source"]
        path = _resolve(edl["sources"][name], base_dir)
        info = probe(path)
        src_frames = round(float(info.get("format", {}).get("duration", 0) or 0) * base)
        start, end = round(float(r["start"]) * base), round(float(r["end"]) * base)
        length = end - start
        file_id = f"file-{len(files) + 1}"
        if name in files:
            file_xml = f'<file id="{files[name]}"/>'
        else:
            files[name] = file_id
            video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
            file_xml = (f'<file id="{file_id}"><name>{escape(path.name)}</name>'
                        f"<pathurl>file://localhost{quote(str(path))}</pathurl>{rate_xml}<duration>{src_frames}</duration>"
                        f"<media><video><samplecharacteristics><width>{video.get('width', canvas[0])}</width>"
                        f"<height>{video.get('height', canvas[1])}</height></samplecharacteristics></video>"
                        "<audio><channelcount>2</channelcount></audio></media></file>")
        common = (f"<name>{escape(path.name)}</name><enabled>TRUE</enabled><duration>{src_frames}</duration>{rate_xml}"
                  f"<start>{record}</start><end>{record + length}</end><in>{start}</in><out>{end}</out>")
        video_items.append(f'<clipitem id="v{index}">{common}{file_xml}</clipitem>')
        audio_items.append(f'<clipitem id="a{index}">{common}<file id="{files[name]}"/>'
                           "<sourcetrack><mediatype>audio</mediatype><trackindex>1</trackindex></sourcetrack></clipitem>")
        record += length
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n<xmeml version="5"><sequence id="sequence-1">'
            f"<name>{escape(title)}</name><duration>{record}</duration>{rate_xml}<media>"
            f"<video><format><samplecharacteristics>{rate_xml}<width>{canvas[0]}</width><height>{canvas[1]}</height>"
            f"<pixelaspectratio>square</pixelaspectratio></samplecharacteristics></format>"
            f"<track>{''.join(video_items)}</track></video>"
            f"<audio><track>{''.join(audio_items)}</track></audio></media></sequence></xmeml>\n")


def _flatten(src: Path, out: Path, canvas: tuple[int, int], x: int, y: int, rate: str) -> Path:
    """An alpha clip placed at (x, y) on a full transparent canvas (PNG-in-MOV)."""
    if not out.exists():
        w, h = canvas
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c=black@0.0:s={w}x{h}:r={rate},format=rgba",
                        "-i", str(src), "-filter_complex", f"[0][1]overlay=x={x}:y={y}:shortest=1:format=auto,format=rgba",
                        "-c:v", "png", "-pix_fmt", "rgba", str(out)], check=True)
    return out


def _cutout(base: Path, mask: Path, start: float, duration: float, out: Path, rate: str) -> Path:
    """The person alone (alpha from the matte) for one stretch of the base."""
    if not out.exists():
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.4f}", "-t", f"{duration:.4f}", "-i", str(base),
                        "-ss", f"{start:.4f}", "-t", f"{duration:.4f}", "-i", str(mask), "-filter_complex",
                        "[1:v]format=gray[m0];[m0][0:v]scale2ref[m][v];[v][m]alphamerge,format=rgba",
                        "-r", rate, "-c:v", "png", "-pix_fmt", "rgba", str(out)], check=True)
    return out


def write_srt(edl: dict, base_dir: Path, out: Path) -> Path:
    """The edit's captions as SRT (same cues and cleaning as the burned-in ones)."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from build_captions import caption_cues, caption_skin, clean_word

    skin = caption_skin(edl, base_dir)

    def stamp(t: float) -> str:
        ms = round(t * 1000)
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"

    rows = [f"{k}\n{stamp(a)} --> {stamp(b)}\n{' '.join(clean_word(t, skin) for _x, _y, t in w)}\n"
            for k, (a, b, w) in enumerate(caption_cues(edl, base_dir, int(skin["max_words"])), start=1)]
    out.write_text("\n".join(rows), encoding="utf-8")
    return out


class _Xml:
    def __init__(self, rate: str, canvas: tuple[int, int]):
        self.base, ntsc = timebase(rate)
        self.rate_xml = f"<rate><timebase>{self.base}</timebase><ntsc>{'TRUE' if ntsc else 'FALSE'}</ntsc></rate>"
        self.canvas = canvas
        self.files: dict[str, str] = {}
        self.n = 0

    def frames(self, seconds: float) -> int:
        return round(seconds * self.base)

    def file(self, path: Path, kind: str = "video") -> str:
        key = str(path)
        if key in self.files:
            return f'<file id="{self.files[key]}"/>'
        fid = f"file-{len(self.files) + 1}"
        self.files[key] = fid
        info = probe(path)
        dur = self.frames(float(info.get("format", {}).get("duration", 0) or 0))
        video = next((st for st in info.get("streams", []) if st.get("codec_type") == "video"), None)
        has_audio = any(st.get("codec_type") == "audio" for st in info.get("streams", []))
        media = ""
        if video:
            media += (f"<video><samplecharacteristics><width>{video.get('width')}</width>"
                      f"<height>{video.get('height')}</height></samplecharacteristics></video>")
        if has_audio:
            media += "<audio><channelcount>2</channelcount></audio>"
        return (f'<file id="{fid}"><name>{escape(path.name)}</name><pathurl>file://localhost{quote(str(path))}</pathurl>'
                f"{self.rate_xml}<duration>{dur}</duration><media>{media}</media></file>")

    def clip(self, path: Path, start: float, duration: float, src_in: float = 0.0, *, audio: bool = False,
             enabled: bool = True, name: str | None = None, gain_db: float | None = None) -> str:
        self.n += 1
        a, d = self.frames(start), max(1, self.frames(duration))
        i = self.frames(src_in)
        level = ""
        if gain_db is not None:
            level = ("<filter><effect><name>Audio Levels</name><effectid>audiolevels</effectid><effectcategory>audiolevels"
                     "</effectcategory><effecttype>audiolevels</effecttype><mediatype>audio</mediatype><parameter>"
                     "<parameterid>level</parameterid><name>Level</name><valuemin>0</valuemin><valuemax>3.98109</valuemax>"
                     f"<value>{10 ** (gain_db / 20):.5f}</value></parameter></effect></filter>")
        src = ("<sourcetrack><mediatype>audio</mediatype><trackindex>1</trackindex></sourcetrack>" if audio else "")
        return (f'<clipitem id="c{self.n}"><name>{escape(name or path.name)}</name>'
                f"<enabled>{'TRUE' if enabled else 'FALSE'}</enabled>{self.rate_xml}"
                f"<start>{a}</start><end>{a + d}</end><in>{i}</in><out>{i + d}</out>{self.file(path)}{src}{level}</clipitem>")


def premiere_export(edl: dict, edl_path: Path, out_dir: Path, title: str) -> Path:
    base_dir = edl_path.parent
    manifest_path = base_dir / "render_manifest.json"
    if not manifest_path.exists():
        raise SystemExit("no render_manifest.json — render the edit once (run_edit.py) before a Premiere export")
    m = json.loads(manifest_path.read_text(encoding="utf-8"))
    canvas, rate = tuple(m["canvas"]), m["rate"]
    media = out_dir / f"{title}_media"
    media.mkdir(parents=True, exist_ok=True)
    x = _Xml(rate, canvas)
    total = float(m["total_s"])
    base = _resolve(m["base"], base_dir)
    timing = m["timing"]

    v_picture = [x.clip(base, t[1], t[2], t[1], name=f"{k + 1:02d} {edl['ranges'][k].get('beat') or edl['ranges'][k]['source']}")
                 for k, t in enumerate(timing)]
    def layer(items: list[dict], tag: str) -> list[str]:
        out = []
        for k, o in enumerate(items):
            src = _resolve(o["file"], base_dir)
            flat = _flatten(src, media / f"{tag}_{k:02d}_{src.stem[:24]}.mov", canvas, int(o.get("x", 0)), int(o.get("y", 0)), rate)
            out.append(x.clip(flat, float(o["start_in_output"]), float(o["duration"]),
                              name=f"{tag} {str(o.get('text') or o.get('type') or src.stem)[:40]}"))
        return out
    captions = [o for o in m["overlays"] if Path(o["file"]).name == "captions.mov"]
    graphics = [o for o in m["overlays"] if o not in captions]
    v_behind = layer(m["behind_subject_overlays"], "behind")
    v_cutout = []
    if m.get("subject_mask") and m["behind_subject_overlays"]:
        spans = sorted((float(o["start_in_output"]), float(o["start_in_output"]) + float(o["duration"])) for o in m["behind_subject_overlays"])
        merged: list[list[float]] = []
        for a, b in spans:
            if merged and a <= merged[-1][1] + 0.05:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        for k, (a, b) in enumerate(merged):
            cut = _cutout(base, Path(m["subject_mask"]), a, b - a, media / f"subject_cutout_{k:02d}.mov", rate)
            v_cutout.append(x.clip(cut, a, b - a, name="subject cut-out (person matte)"))
    v_graphics = layer(graphics, "gfx")
    v_captions = [x.clip(_resolve(o["file"], base_dir), float(o["start_in_output"]), float(o["duration"]), name="captions")
                  for o in captions]
    v_sources, a_sources = [], []
    for k, (r, t) in enumerate(zip(edl["ranges"], timing)):
        src = _resolve(edl["sources"][r["source"]], base_dir)
        v_sources.append(x.clip(src, t[1], t[2], float(r["start"]), enabled=False, name=f"source {r['source']}"))
        sound = r.get("audio_source") if isinstance(r.get("audio_source"), dict) else None
        a_src = _resolve(edl["sources"][sound["source"]], base_dir) if sound else src
        a_in = float(sound.get("start", r["start"])) if sound else float(r["start"])
        a_sources.append(x.clip(a_src, t[1], t[2], a_in, audio=True, enabled=False, name=f"source audio {a_src.stem}"))
    stems = Path(m["stems"])
    dialogue = stems / "dialogue.wav" if (stems / "dialogue.wav").exists() else Path(m["audio_mix"])
    a_dialogue = [x.clip(dialogue, 0.0, total, audio=True, name="dialogue (processed)")]
    a_sfx = []
    for item in m["sfx"]:
        path = _resolve(item["file"], base_dir)
        if not path.exists():
            continue
        dur = float(probe(path).get("format", {}).get("duration", 0) or 0)
        dur = min(dur, max(0.05, total - float(item["start_in_output"])))
        a_sfx.append(x.clip(path, float(item["start_in_output"]), dur, audio=True, gain_db=float(item["gain_db"]),
                            name=f"sfx {item['label']}"))

    try:
        write_srt(edl, base_dir, out_dir / f"{title}.srt")
    except Exception as exc:
        print(f"  (no SRT: {exc})")

    def track(items: list[str], name: str) -> str:
        return f"<track><name>{escape(name)}</name>{''.join(items)}</track>" if items else ""
    frames_total = x.frames(total)
    video = "".join(track(t, n) for t, n in [(v_picture, "V1 picture"), (v_behind, "V2 behind subject"),
                                              (v_cutout, "V3 subject cut-out"), (v_graphics, "V4 graphics"),
                                              (v_captions, "V5 captions"), (v_sources, "V6 sources (off)")])
    audio = "".join(track(t, n) for t, n in [(a_dialogue, "A1 dialogue"), (a_sfx, "A2 sfx"), (a_sources, "A3 source audio (off)")])
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n<xmeml version="5"><sequence id="sequence-1">'
           f"<name>{escape(title)}</name><duration>{frames_total}</duration>{x.rate_xml}<media>"
           f"<video><format><samplecharacteristics>{x.rate_xml}<width>{canvas[0]}</width><height>{canvas[1]}</height>"
           f"<pixelaspectratio>square</pixelaspectratio></samplecharacteristics></format>{video}</video>"
           f"<audio>{audio}</audio></media></sequence></xmeml>\n")
    out = out_dir / f"{title}_premiere.xml"
    out.write_text(xml, encoding="utf-8")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Export a Video Use EDL for Premiere / Resolve / Avid")
    parser.add_argument("edl", type=Path)
    parser.add_argument("-o", "--out-dir", type=Path, default=None)
    parser.add_argument("--fps", default=None, help="Timeline rate (default: first source's rate)")
    parser.add_argument("--premiere", action="store_true",
                        help="also rebuild the finished edit layer by layer for Premiere (needs a prior render)")
    args = parser.parse_args()
    edl_path = args.edl.resolve()
    edl = json.loads(edl_path.read_text(encoding="utf-8"))
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from render import resolve_canvas, resolve_output_rate
    base_dir = edl_path.parent
    rate = resolve_output_rate(edl, base_dir, args.fps)
    canvas = resolve_canvas(edl, base_dir)
    out_dir = (args.out_dir or base_dir / "export").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    # Name the timeline after the project folder, not "edl".
    title = edl_path.stem if edl_path.stem not in ("edl", "edl_first_cut") else (
        edl_path.parent.parent.name if edl_path.parent.name == "edit" else edl_path.parent.name)
    (out_dir / f"{title}.edl").write_text(cmx3600(edl, base_dir, rate, title), encoding="utf-8")
    (out_dir / f"{title}.xml").write_text(fcp7_xml(edl, base_dir, rate, title, canvas), encoding="utf-8")
    print(f"timeline → {out_dir / (title + '.edl')}  +  {out_dir / (title + '.xml')}")
    if args.premiere:
        print(f"premiere → {premiere_export(edl, edl_path, out_dir, title)}")


if __name__ == "__main__":
    main()
