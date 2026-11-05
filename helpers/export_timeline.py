"""Hand the cut to an NLE: CMX3600 EDL and FCP7 XML (Premiere, Resolve, Avid).

The export carries the editorial decision (sources, in/out, order) so a
finishing editor can relink the camera originals and keep working. Graphics,
captions, and the audio mix stay in Video Use's render.
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Export a Video Use EDL for Premiere / Resolve / Avid")
    parser.add_argument("edl", type=Path)
    parser.add_argument("-o", "--out-dir", type=Path, default=None)
    parser.add_argument("--fps", default=None, help="Timeline rate (default: first source's rate)")
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
    title = edl_path.stem
    (out_dir / f"{title}.edl").write_text(cmx3600(edl, base_dir, rate, title), encoding="utf-8")
    (out_dir / f"{title}.xml").write_text(fcp7_xml(edl, base_dir, rate, title, canvas), encoding="utf-8")
    print(f"timeline → {out_dir / (title + '.edl')}  +  {out_dir / (title + '.xml')}")


if __name__ == "__main__":
    main()
