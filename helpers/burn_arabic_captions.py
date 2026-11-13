"""Burn Arabic ASS captions without requiring FFmpeg's optional libass filter.

macOS CoreText draws the Arabic with native RTL shaping.  Each ASS dialogue is
rendered once as a transparent PNG, then FFmpeg overlays those small, timed
layers over the already edited video.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
from fractions import Fraction
from pathlib import Path


def parse_time(value: str) -> float:
    h, m, s = value.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def captions_from_ass(path: Path) -> list[dict[str, object]]:
    captions: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.startswith("Dialogue:"):
            continue
        parts = line[len("Dialogue:"):].lstrip().split(",", 9)
        if len(parts) != 10:
            continue
        text = re.sub(r"\{[^}]*\}", "", parts[9]).replace(r"\N", "\n").strip()
        if text:
            captions.append({"start": parse_time(parts[1]), "end": parse_time(parts[2]), "text": text})
    return captions


def probe_video(path: Path) -> tuple[int, int, str]:
    """Return (width, height, frame_rate) of the video being captioned."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,avg_frame_rate,r_frame_rate",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    stream = json.loads(out.stdout)["streams"][0]
    rate = "30"
    for key in ("avg_frame_rate", "r_frame_rate"):
        value = stream.get(key)
        if value and value != "0/0":
            try:
                if Fraction(value) > 0:
                    rate = value
                    break
            except (ValueError, ZeroDivisionError):
                continue
    return int(stream["width"]), int(stream["height"]), rate


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("video", type=Path)
    parser.add_argument("ass", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--font", default="Cocon Next Arabic")
    parser.add_argument("--font-size", type=float, default=None, help="Default: 3%% of video height")
    parser.add_argument("--margin-v", type=float, default=None, help="Default: 13%% of video height")
    args = parser.parse_args()

    width, height, rate = probe_video(args.video)
    font_size = args.font_size if args.font_size is not None else round(height * 0.030)
    margin_v = args.margin_v if args.margin_v is not None else round(height * 0.13)
    captions = captions_from_ass(args.ass)
    if not captions:
        raise SystemExit("No dialogue lines found in ASS file")
    workspace = args.output.parent / "caption_layers"
    workspace.mkdir(parents=True, exist_ok=True)
    for index, caption in enumerate(captions):
        caption["file"] = str(workspace / f"caption_{index:03d}.png")

    helper = Path(__file__).with_name("render_arabic_caption.swift")
    binary = Path(tempfile.gettempdir()) / "video-use-caption-renderer"
    if not binary.exists() or binary.stat().st_mtime < helper.stat().st_mtime:
        subprocess.run(["swiftc", str(helper), "-o", str(binary)], check=True)
    payload = workspace / "captions.json"
    payload.write_text(json.dumps({"width": width, "height": height, "font": args.font, "fontSize": font_size, "marginV": margin_v, "captions": captions}, ensure_ascii=False), encoding="utf-8")
    subprocess.run([str(binary), str(payload)], check=True)

    cmd = ["ffmpeg", "-y", "-i", str(args.video)]
    for caption in captions:
        cmd += ["-loop", "1", "-framerate", rate, "-i", str(caption["file"])]
    current = "[0:v]"
    filters: list[str] = []
    for index, caption in enumerate(captions, start=1):
        label = f"[v{index}]"
        filters.append(f"{current}[{index}:v]overlay=0:0:enable='between(t,{caption['start']:.3f},{caption['end']:.3f})'{label}")
        current = label
    cmd += ["-filter_complex", ";".join(filters), "-map", current, "-map", "0:a", "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", "-shortest", str(args.output)]
    subprocess.run(cmd, check=True)


if __name__ == "__main__":
    main()
