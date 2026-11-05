"""Build Arabic ASS captions from Video Use transcripts."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any


def ass_timestamp(seconds: float) -> str:
    centiseconds = max(0, round(seconds * 100))
    hours, rest = divmod(centiseconds, 360_000)
    minutes, rest = divmod(rest, 6_000)
    whole_seconds, remainder = divmod(rest, 100)
    return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{remainder:02d}"


def ass_escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}").replace("\n", r"\N")


def words_in_range(transcript: dict[str, Any], start: float, end: float) -> list[dict[str, Any]]:
    return [word for word in transcript.get("words", []) if word.get("type", "word") == "word" and word.get("start") is not None and word.get("end") is not None and float(word["end"]) > start and float(word["start"]) < end and str(word.get("text", "")).strip()]


# Sentence and clause marks, Arabic and Latin. A caption never continues
# across one: "،" and "؛" are as strong a break in Arabic as "," is in English.
BREAK_MARKS = (".", "!", "?", "؟", "…", "،", "؛", ",", ";", ":")


def grouped_words(words: list[dict[str, Any]], max_words: int) -> list[list[dict[str, Any]]]:
    output: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for word in words:
        current.append(word)
        if len(current) >= max_words or str(word.get("text", "")).rstrip().endswith(BREAK_MARKS):
            output.append(current)
            current = []
    if current:
        output.append(current)
    return output


def canvas_for(edl: dict[str, Any], edit_dir: Path) -> tuple[int, int]:
    """Caption canvas = render canvas: EDL `canvas`, else first source orientation."""
    canvas = edl.get("canvas")
    if isinstance(canvas, dict) and canvas.get("width") and canvas.get("height"):
        return int(canvas["width"]), int(canvas["height"])
    ranges = edl.get("ranges") or []
    if ranges:
        raw = Path(edl.get("sources", {}).get(ranges[0]["source"], ""))
        source = raw if raw.is_absolute() else (edit_dir / raw)
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height:stream_side_data=rotation",
                 "-of", "json", str(source)],
                capture_output=True, text=True, check=True,
            )
            stream = json.loads(out.stdout)["streams"][0]
            w, h = int(stream["width"]), int(stream["height"])
            rotation = next((d.get("rotation") for d in stream.get("side_data_list") or [] if d.get("rotation") is not None), 0)
            if int(round(float(rotation))) % 360 in (90, 270):
                w, h = h, w
            if (edl.get("source_rotations") or {}).get(ranges[0]["source"]):
                w, h = min(w, h), max(w, h)
            return (1080, 1920) if h > w else (1920, 1080)
        except (OSError, subprocess.CalledProcessError, KeyError, IndexError, ValueError, TypeError, json.JSONDecodeError):
            pass
    return 1080, 1920


def smooth_events(events: list[tuple[float, float, str]], bridge_gap: float = 0.25, min_duration: float = 0.45) -> list[tuple[float, float, str]]:
    """Bridge tiny gaps (no blinking between cues) and keep short cues readable."""
    ordered = sorted(events, key=lambda e: e[0])
    out: list[tuple[float, float, str]] = []
    for i, (start, end, text) in enumerate(ordered):
        next_start = ordered[i + 1][0] if i + 1 < len(ordered) else None
        end = max(end, start + min_duration)
        if next_start is not None:
            if next_start - end < bridge_gap:
                end = next_start
            end = min(end, next_start)
        out.append((start, max(end, start + 0.04), text))
    return out


def build_ass(
    edl: dict[str, Any],
    edit_dir: Path,
    font: str,
    font_size: int | None,
    margin_v: int | None,
    max_words: int,
    play_res: tuple[int, int] | None = None,
) -> str:
    width, height = play_res or canvas_for(edl, edit_dir)
    # Defaults scale with the canvas height (58 / 250 on a 1920-tall frame).
    font_size = font_size if font_size is not None else round(height * 0.030)
    margin_v = margin_v if margin_v is not None else round(height * 0.13)
    side = round(width * 0.065)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Arabic,{font},{font_size},&H00FFFFFF,&H0000D7FF,&H00101010,&H80000000,-1,0,0,0,100,100,0,0,1,3,1,2,{side},{side},{margin_v},1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""
    offset = 0.0
    timed: list[tuple[float, float, str]] = []
    for item in edl.get("ranges", []):
        source = item["source"]
        start, end = float(item["start"]), float(item["end"])
        transcript_path = edit_dir / "transcripts" / f"{source}.json"
        if not transcript_path.exists():
            offset += end - start
            continue
        transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
        for group in grouped_words(words_in_range(transcript, start, end), max_words):
            local_start = max(start, float(group[0]["start"]))
            local_end = min(end, float(group[-1]["end"]))
            if local_end <= local_start:
                continue
            text = " ".join(str(word["text"]).strip() for word in group)
            timed.append((offset + local_start - start, offset + local_end - start, text))
        offset += end - start
    events = [
        f"Dialogue: 0,{ass_timestamp(a)},{ass_timestamp(b)},Arabic,,0,0,0,,{ass_escape(text)}"
        for a, b, text in smooth_events(timed)
    ]
    return header + "\n".join(events) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Arabic ASS captions from a Video Use EDL")
    parser.add_argument("edl", type=Path)
    parser.add_argument("-o", "--output", type=Path, default=None)
    parser.add_argument("--font", default="Geeza Pro")
    parser.add_argument("--font-size", type=int, default=None, help="Default: 3%% of canvas height")
    parser.add_argument("--margin-v", type=int, default=None, help="Default: 13%% of canvas height")
    parser.add_argument("--max-words", type=int, default=4, choices=range(3, 6))
    args = parser.parse_args()
    edl_path = args.edl.resolve()
    output = (args.output or edl_path.parent / "master_ar.ass").resolve()
    edl = json.loads(edl_path.read_text(encoding="utf-8"))
    output.write_text(build_ass(edl, edl_path.parent, args.font, args.font_size, args.margin_v, args.max_words), encoding="utf-8")
    print(f"Arabic captions → {output}")


if __name__ == "__main__":
    main()
