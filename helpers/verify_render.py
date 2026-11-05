"""Create an evidence-backed QC report for a Video Use render."""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from statistics import fmean
from pathlib import Path
from typing import Any

from validate_edl import validate_edl


def frame_luma(path: Path, timestamp: float) -> dict[str, float] | None:
    """Sample a small grayscale frame without writing an image to disk."""
    command = [
        "ffmpeg", "-v", "error", "-ss", f"{max(0.0, timestamp):.3f}",
        "-i", str(path), "-frames:v", "1", "-vf", "scale=64:64",
        "-pix_fmt", "gray", "-f", "rawvideo", "-",
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    values = result.stdout
    if len(values) != 64 * 64:
        return None
    return {
        "mean_luma": round(fmean(values), 2),
        "near_black_fraction": round(sum(value <= 12 for value in values) / len(values), 4),
    }


def is_nearly_black(sample: dict[str, float]) -> bool:
    """Conservative signal: a nearly black full frame, not a dark scene."""
    return sample["mean_luma"] <= 8.0 and sample["near_black_fraction"] >= 0.98


def scan_frame_samples(output: Path, duration: float, cut_times: list[float]) -> list[dict[str, Any]]:
    """Inspect start/end plus a tight window around every edit boundary."""
    candidates: list[tuple[str, float]] = [("start", min(0.10, duration / 2)), ("end", max(0.0, duration - 0.10))]
    for index, cut in enumerate(cut_times):
        for label, offset in (("before", -0.04), ("at", 0.0), ("after", 0.04)):
            timestamp = min(max(0.0, cut + offset), duration)
            candidates.append((f"cut_{index}_{label}", timestamp))
    samples: list[dict[str, Any]] = []
    for label, timestamp in candidates:
        signal = frame_luma(output, timestamp)
        if signal is not None:
            samples.append({"label": label, "timestamp_s": round(timestamp, 3), **signal, "nearly_black": is_nearly_black(signal)})
    return samples


def probe_media(path: Path) -> dict[str, Any] | None:
    command = ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        return json.loads(result.stdout)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError):
        return None


def audio_peak_db(path: Path) -> float | None:
    command = ["ffmpeg", "-v", "info", "-i", str(path), "-map", "0:a:0", "-filter:a", "volumedetect", "-f", "null", "-"]
    try:
        result = subprocess.run(command, capture_output=True, text=True)
    except OSError:
        return None
    match = re.search(r"max_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", result.stderr)
    return float(match.group(1)) if match else None


LOUDNESS_TARGET_LUFS = -14.0
LOUDNESS_TOLERANCE_LU = 1.5
TRUE_PEAK_CEILING = -0.8
MAX_CAPTION_CPS = 20.0


def output_loudness(path: Path) -> dict[str, float | None]:
    command = ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn", "-af",
               "loudnorm=print_format=json", "-f", "null", "-"]
    stderr = subprocess.run(command, capture_output=True, text=True).stderr
    match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", stderr, re.S)
    if not match:
        return {"integrated_lufs": None, "true_peak_dbtp": None}
    data = json.loads(match.group(0))
    as_float = lambda key: float(data[key]) if data.get(key) not in (None, "-inf", "inf") else None
    return {"integrated_lufs": as_float("input_i"), "true_peak_dbtp": as_float("input_tp")}


def parse_detect_events(stderr: str) -> dict[str, list[dict[str, float]]]:
    freezes = [{"start_s": float(a), "duration_s": float(b)} for a, b in re.findall(
        r"freeze_start: ([0-9.]+).*?freeze_duration: ([0-9.]+)", stderr, re.S)]
    blacks = [{"start_s": float(a), "duration_s": float(c)} for a, _b, c in re.findall(
        r"black_start:([0-9.]+) black_end:([0-9.]+) black_duration:([0-9.]+)", stderr)]
    return {"freezes": freezes, "black": blacks}


def picture_events(path: Path) -> dict[str, list[dict[str, float]]]:
    """Whole-timeline frozen picture and black stretches."""
    command = ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-an", "-vf",
               "scale=320:-2,freezedetect=n=0.001:d=2.5,blackdetect=d=0.4:pix_th=0.08",
               "-f", "null", "-"]
    return parse_detect_events(subprocess.run(command, capture_output=True, text=True).stderr)


def caption_reading(ass_path: Path) -> dict[str, Any]:
    """Reading speed per caption (word-highlight events merged back into cues)."""
    cues: list[list[Any]] = []
    for line in ass_path.read_text(encoding="utf-8-sig").splitlines():
        if not line.startswith("Dialogue:"):
            continue
        parts = line[len("Dialogue:"):].lstrip().split(",", 9)
        if len(parts) != 10:
            continue
        h1, m1, s1 = parts[1].split(":")
        h2, m2, s2 = parts[2].split(":")
        start = int(h1) * 3600 + int(m1) * 60 + float(s1)
        end = int(h2) * 3600 + int(m2) * 60 + float(s2)
        text = re.sub(r"\{[^}]*\}", "", parts[9]).replace(r"\N", " ").replace("\u200f", "").strip()
        if cues and cues[-1][2] == text and abs(cues[-1][1] - start) < 0.02:
            cues[-1][1] = end
        else:
            cues.append([start, end, text])
    fast, short = [], []
    for start, end, text in cues:
        duration = max(0.01, end - start)
        cps = len(text.replace(" ", "")) / duration
        if cps > MAX_CAPTION_CPS:
            fast.append({"start_s": round(start, 2), "cps": round(cps, 1), "text": text})
        if duration < 0.5:
            short.append({"start_s": round(start, 2), "duration_s": round(duration, 2), "text": text})
    return {"cues": len(cues), "too_fast": fast, "too_short": short}


def pacing_report(edl: dict[str, Any], edit_dir: Path) -> dict[str, Any]:
    ranges = edl.get("ranges") or []
    shots = [float(r["end"]) - float(r["start"]) for r in ranges]
    total = sum(shots)
    words = 0
    for r in ranges:
        path = edit_dir / "transcripts" / f"{r['source']}.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            words += sum(1 for w in data.get("words", []) if w.get("type", "word") == "word"
                         and float(w.get("start", -1)) >= float(r["start"]) and float(w.get("end", 1e9)) <= float(r["end"]))
    changes = [0.0, total]
    cursor = 0.0
    for r, shot in zip(ranges, shots):
        cursor += shot
        changes.append(cursor)
    for g in (edl.get("graphics") or []) + (edl.get("overlays") or []):
        if "start_in_output" in g:
            changes += [float(g["start_in_output"]), float(g["start_in_output"]) + float(g.get("duration", 0))]
    changes = sorted(c for c in changes if 0 <= c <= total)
    longest = max((b - a for a, b in zip(changes, changes[1:])), default=total)
    minutes = total / 60 if total else 0
    return {
        "duration_s": round(total, 2),
        "shots": len(shots),
        "median_shot_s": round(sorted(shots)[len(shots) // 2], 2) if shots else None,
        "longest_shot_s": round(max(shots), 2) if shots else None,
        "words_per_minute": round(words / minutes) if minutes and words else None,
        "graphics_per_minute": round(len(edl.get("graphics") or []) / minutes, 1) if minutes else None,
        "longest_static_stretch_s": round(longest, 2),
    }


def output_cut_times(edl: dict[str, Any]) -> list[float]:
    cursor, cuts = 0.0, []
    ranges = edl.get("ranges") or []
    for index, item in enumerate(ranges):
        try:
            cursor += float(item["end"]) - float(item["start"])
        except (KeyError, TypeError, ValueError):
            continue
        if index < len(ranges) - 1:
            cuts.append(round(cursor, 3))
    return cuts


def render_visual_windows(output: Path, report_dir: Path, times: list[float], duration: float) -> list[str]:
    """Produce on-demand filmstrips around cuts for the agent's visual readback."""
    script = Path(__file__).with_name("timeline_view.py")
    created: list[str] = []
    for index, center in enumerate(times):
        start, end = max(0.0, center - 1.5), min(duration, center + 1.5)
        if end <= start:
            continue
        image = report_dir / f"cut_{index:03d}_{center:.3f}s.png"
        command = [sys.executable, str(script), str(output), f"{start:.3f}", f"{end:.3f}", "-o", str(image)]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode == 0 and image.exists():
            created.append(str(image))
    return created


def verify_render(edl_path: Path, output: Path, visual: bool = False) -> dict[str, Any]:
    validation = validate_edl(edl_path)
    report: dict[str, Any] = {
        "edl": str(edl_path), "output": str(output), "passed": False,
        "errors": [], "warnings": [], "validation": validation,
        "expected_duration_s": validation["expected_duration_s"], "visual_windows": [],
    }
    if not output.is_file() or output.stat().st_size == 0:
        report["errors"].append("Output file is missing or empty.")
        return report
    media = probe_media(output)
    if media is None:
        report["errors"].append("ffprobe could not inspect the rendered output.")
        return report
    streams = media.get("streams", [])
    video = [stream for stream in streams if stream.get("codec_type") == "video"]
    audio = [stream for stream in streams if stream.get("codec_type") == "audio"]
    report["media"] = {
        "video_streams": len(video), "audio_streams": len(audio),
        "duration_s": float(media.get("format", {}).get("duration", 0) or 0),
        "size_bytes": output.stat().st_size,
    }
    if not video:
        report["errors"].append("Rendered output has no video stream.")
    if not audio:
        report["errors"].append("Rendered output has no audio stream.")
    duration = report["media"]["duration_s"]
    expected = float(validation["expected_duration_s"])
    # AAC streams are packetized in 1024-sample frames.  Per-segment AAC
    # encoding plus lossless concat can therefore add a small container-level
    # tail even when every visual range is exact.  0.30s catches a genuine
    # editorial-range error while accepting normal AAC encoder padding.
    if expected and abs(duration - expected) > 0.30:
        report["errors"].append(f"Output duration {duration:.3f}s differs from EDL expectation {expected:.3f}s.")
    peak = audio_peak_db(output) if audio else None
    report["audio_peak_db"] = peak
    if audio and peak is None:
        report["warnings"].append("Could not measure output audio peak.")
    elif peak is not None and peak > -0.5:
        report["warnings"].append(f"Audio peak is {peak:.1f} dB; check for clipping.")

    edl = json.loads(edl_path.read_text(encoding="utf-8"))
    edit_dir = edl_path.parent
    if audio:
        loud = output_loudness(output)
        report["loudness"] = loud
        lufs, tp = loud["integrated_lufs"], loud["true_peak_dbtp"]
        if lufs is not None and abs(lufs - LOUDNESS_TARGET_LUFS) > LOUDNESS_TOLERANCE_LU:
            report["warnings"].append(f"Integrated loudness {lufs:.1f} LUFS is off the {LOUDNESS_TARGET_LUFS:.0f} LUFS target.")
        if tp is not None and tp > TRUE_PEAK_CEILING:
            report["warnings"].append(f"True peak {tp:.1f} dBTP is above {TRUE_PEAK_CEILING} dBTP.")
    if video and duration > 0:
        events = picture_events(output)
        report["picture_events"] = events
        for freeze in events["freezes"]:
            report["warnings"].append(f"Picture frozen for {freeze['duration_s']:.1f}s at {freeze['start_s']:.2f}s.")
        for black in events["black"]:
            report["warnings"].append(f"Black picture for {black['duration_s']:.2f}s at {black['start_s']:.2f}s.")
    ass = None
    if edl.get("subtitles") and str(edl["subtitles"]).endswith(".ass"):
        ass = Path(edl["subtitles"]) if Path(edl["subtitles"]).is_absolute() else edit_dir / edl["subtitles"]
    elif edl.get("captions"):
        ass = edit_dir / "captions.ass"
    if ass is not None and ass.is_file():
        reading = caption_reading(ass)
        report["captions"] = reading
        if reading["too_fast"]:
            report["warnings"].append(f"{len(reading['too_fast'])} caption(s) exceed {MAX_CAPTION_CPS:.0f} characters/second.")
        if reading["too_short"]:
            report["warnings"].append(f"{len(reading['too_short'])} caption(s) are on screen under 0.5s.")
    report["pacing"] = pacing_report(edl, edit_dir)
    report["frame_samples"] = scan_frame_samples(output, duration, output_cut_times(edl)) if video and duration > 0 else []
    suspect_frames = [sample for sample in report["frame_samples"] if sample["nearly_black"]]
    if suspect_frames:
        moments = ", ".join(f"{sample['label']} ({sample['timestamp_s']:.3f}s)" for sample in suspect_frames)
        report["warnings"].append(f"Nearly black frame signal at {moments}; inspect the cut filmstrip before delivery.")

    if visual and duration > 0:
        report_dir = edl_path.parent / "verify"
        report_dir.mkdir(parents=True, exist_ok=True)
        # The container duration includes AAC padding past the last picture
        # frame; seeking there yields no frame. Bound windows by the picture.
        try:
            picture_end = float(video[0].get("duration") or duration)
        except (TypeError, ValueError):
            picture_end = duration
        picture_end = max(0.0, min(duration, picture_end) - 0.05)
        times = [0.75, *output_cut_times(edl), max(0.75, picture_end - 0.75)]
        report["visual_windows"] = render_visual_windows(output, report_dir, times, picture_end)
        if len(report["visual_windows"]) < len(times):
            report["warnings"].append("Some visual verification windows could not be generated.")

    report["passed"] = not report["errors"] and validation["valid"]
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify a Video Use output against its EDL")
    parser.add_argument("edl", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--visual", action="store_true", help="Generate filmstrips around every output cut")
    parser.add_argument("--report", type=Path, help="Where to write QC JSON (defaults beside the EDL)")
    args = parser.parse_args()
    edl, output = args.edl.resolve(), args.output.resolve()
    report = verify_render(edl, output, visual=args.visual)
    report_path = args.report or edl.parent / "qc_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"QC report → {report_path}")
    print("PASS" if report["passed"] else "FAIL")
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
