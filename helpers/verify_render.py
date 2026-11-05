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
