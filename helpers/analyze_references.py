"""Measure finished reference videos so a style can be learned from examples.

Reports facts only: format, pacing (shot length), loudness, silence, and
dominant colours. Turning those facts into a style recipe or a brand identity
is an editorial judgment the agent makes afterwards (see
references/style-library.md and references/brand-identity.md).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from statistics import median
from typing import Any

import numpy as np

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".webm"}


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def probe_format(path: Path) -> dict[str, Any]:
    out = _run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)])
    data = json.loads(out.stdout or "{}")
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    num, _, den = (video.get("avg_frame_rate") or "0/1").partition("/")
    fps = round(float(num) / float(den or 1), 3) if float(den or 1) else None
    width, height = video.get("width"), video.get("height")
    rotation = 0
    for side in video.get("side_data_list", []) or []:
        if "rotation" in side:
            rotation = abs(int(side["rotation"])) % 180
    if rotation == 90 and width and height:
        width, height = height, width
    return {
        "width": width, "height": height, "fps": fps,
        "duration_s": round(float(data.get("format", {}).get("duration", 0) or 0), 3),
        "has_audio": audio is not None,
        "orientation": None if not (width and height) else ("vertical" if height > width else "horizontal" if width > height else "square"),
    }


def parse_scene_times(stderr: str) -> list[float]:
    return [float(m) for m in re.findall(r"pts_time:([0-9.]+)", stderr)]


def scene_cuts(path: Path, threshold: float = 0.3) -> list[float]:
    out = _run(["ffmpeg", "-v", "info", "-i", str(path), "-an", "-vf",
                f"scale=320:-2,select='gt(scene,{threshold})',showinfo", "-f", "null", "-"])
    return parse_scene_times(out.stderr)


def pacing(cuts: list[float], duration: float) -> dict[str, Any]:
    bounds = [0.0, *[c for c in cuts if 0 < c < duration], duration]
    shots = [b - a for a, b in zip(bounds, bounds[1:]) if b - a > 0]
    return {
        "scene_cuts": len(bounds) - 2,
        "cuts_per_minute": round((len(bounds) - 2) / duration * 60, 1) if duration else None,
        "median_shot_s": round(median(shots), 2) if shots else None,
        "longest_shot_s": round(max(shots), 2) if shots else None,
        "note": "Scene detection misses jump cuts inside one static angle; read speech gaps too.",
    }


def parse_loudness(stderr: str) -> dict[str, float | None]:
    match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", stderr, re.S)
    if not match:
        return {"integrated_lufs": None, "true_peak_dbtp": None, "lra": None}
    data = json.loads(match.group(0))
    value = lambda key: float(data[key]) if data.get(key) not in (None, "-inf", "inf") else None
    return {"integrated_lufs": value("input_i"), "true_peak_dbtp": value("input_tp"), "lra": value("input_lra")}


def loudness(path: Path) -> dict[str, float | None]:
    out = _run(["ffmpeg", "-v", "info", "-i", str(path), "-vn", "-af",
                "loudnorm=I=-14:TP=-1:LRA=11:print_format=json", "-f", "null", "-"])
    return parse_loudness(out.stderr)


def parse_silences(stderr: str) -> list[float]:
    return [float(m) for m in re.findall(r"silence_duration:\s*([0-9.]+)", stderr)]


def silence(path: Path, duration: float) -> dict[str, Any]:
    out = _run(["ffmpeg", "-v", "info", "-i", str(path), "-vn", "-af",
                "silencedetect=noise=-45dB:d=0.3", "-f", "null", "-"])
    gaps = parse_silences(out.stderr)
    return {
        "gaps_over_300ms": len(gaps),
        "silence_ratio": round(sum(gaps) / duration, 3) if duration else None,
        "continuous_bed_likely": not gaps,
    }


def dominant_colors(pixels: np.ndarray, k: int = 5, iterations: int = 12) -> list[dict[str, Any]]:
    """Tiny deterministic k-means over RGB pixels → hex colours with share."""
    data = pixels.reshape(-1, 3).astype(np.float32)
    if len(data) == 0:
        return []
    k = min(k, len(np.unique(data, axis=0)))
    order = np.argsort(data.sum(axis=1))
    centers = data[order[np.linspace(0, len(data) - 1, k).astype(int)]].copy()
    for _ in range(iterations):
        labels = np.argmin(((data[:, None, :] - centers[None]) ** 2).sum(axis=2), axis=1)
        for i in range(k):
            members = data[labels == i]
            if len(members):
                centers[i] = members.mean(axis=0)
    labels = np.argmin(((data[:, None, :] - centers[None]) ** 2).sum(axis=2), axis=1)
    counts = np.bincount(labels, minlength=k)
    result = []
    for i in np.argsort(-counts):
        r, g, b = (int(round(v)) for v in centers[i])
        result.append({"hex": f"#{r:02X}{g:02X}{b:02X}", "share": round(float(counts[i] / len(data)), 3)})
    return result


def sample_pixels(path: Path, duration: float, samples: int = 8) -> np.ndarray:
    frames = []
    for i in range(samples):
        t = duration * (i + 0.5) / samples
        out = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
                              "-vf", "scale=48:48", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"], capture_output=True)
        if len(out.stdout) == 48 * 48 * 3:
            frames.append(np.frombuffer(out.stdout, np.uint8).reshape(-1, 3))
    return np.concatenate(frames) if frames else np.zeros((0, 3), np.uint8)


def find_light_flashes(frames: np.ndarray, fps: float) -> list[dict[str, Any]]:
    """Light-leak / flash transitions in decoded RGB frames (N×H×W×3, uint8).

    A flash is a one-frame brightness spike well above its neighbours, preceded
    by a ramp of a coloured wash. Its colour is recovered by inverting a screen
    blend on a ramp frame against a clean frame before it.
    """
    f = frames.astype(np.float32)
    mean = f.mean(axis=(1, 2, 3))
    events: list[dict[str, Any]] = []
    last = -99
    for i in range(10, len(mean) - 6):
        around = np.r_[mean[max(0, i - 12):i - 4], mean[i + 4:i + 12]]
        if mean[i] - np.median(around) <= 35 or mean[i] <= 150 or i - last < 8:
            continue
        ref = f[i - 9]
        pre = 0
        while pre < 8 and mean[i - 1 - pre] - mean[i - 9] > 3:
            pre += 1
        post = 0
        while post < 10 and i + 1 + post < len(mean) and abs(mean[i + 1 + post] - mean[min(len(mean) - 1, i + 12)]) > 3:
            post += 1
        color = None
        if pre >= 2:
            ramp = f[i - 2]
            screen = 1 - (255 - ramp) / np.maximum(255 - ref, 1)
            c = np.clip(np.median(screen.reshape(-1, 3), axis=0), 0, 1)
            if c.max() > 0.05:
                c = c / c.max()
                color = "#" + "".join(f"{int(round(v * 255)):02X}" for v in c)
        events.append({"time_s": round(i / fps, 3), "pre_frames": pre, "post_frames": post,
                       "duration_ms": round((pre + 1 + post) / fps * 1000), "color": color})
        last = i
    return events


def light_flashes(path: Path, fps: float | None) -> list[dict[str, Any]]:
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf", "scale=36:64", "-pix_fmt", "rgb24",
                          "-f", "rawvideo", "-"], capture_output=True)
    frames = np.frombuffer(out.stdout, np.uint8)
    if not len(frames):
        return []
    return find_light_flashes(frames.reshape(-1, 64, 36, 3), fps or 30.0)


def analyze(path: Path) -> dict[str, Any]:
    fmt = probe_format(path)
    duration = fmt["duration_s"] or 0.0
    report: dict[str, Any] = {"file": str(path), "format": fmt}
    report["pacing"] = pacing(scene_cuts(path), duration) if duration else None
    if fmt["has_audio"]:
        report["loudness"] = loudness(path)
        report["silence"] = silence(path, duration)
    report["dominant_colors"] = dominant_colors(sample_pixels(path, duration)) if duration else []
    report["light_flashes"] = light_flashes(path, fmt.get("fps")) if duration else []
    return report


def collect(inputs: list[Path]) -> list[Path]:
    files: list[Path] = []
    for item in inputs:
        if item.is_dir():
            files.extend(sorted(p for p in item.rglob("*") if p.suffix.lower() in VIDEO_EXTS and not p.name.startswith("._")))
        elif item.suffix.lower() in VIDEO_EXTS:
            files.append(item)
    return files


def summarize(reports: list[dict[str, Any]]) -> dict[str, Any]:
    def values(getter):
        out = []
        for r in reports:
            try:
                v = getter(r)
            except (KeyError, TypeError):
                v = None
            if v is not None:
                out.append(v)
        return out

    lufs = values(lambda r: r["loudness"]["integrated_lufs"])
    peaks = values(lambda r: r["loudness"]["true_peak_dbtp"])
    cpm = values(lambda r: r["pacing"]["cuts_per_minute"])
    flashes = [e for r in reports for e in (r.get("light_flashes") or [])]
    colors = [e["color"] for e in flashes if e.get("color")]
    light = None
    if flashes:
        rgb = np.median([[int(c[k:k + 2], 16) for k in (1, 3, 5)] for c in colors], axis=0) if colors else None
        light = {
            "count": len(flashes),
            "in_references": sum(bool(r.get("light_flashes")) for r in reports),
            "duration_ms": int(np.median([e["duration_ms"] for e in flashes])),
            "color": "#" + "".join(f"{int(v):02X}" for v in rgb) if rgb is not None else None,
            "brand_kit_suggestion": {"transitions": {"light": {"color": None}}},
        }
        light["brand_kit_suggestion"]["transitions"]["light"]["color"] = light["color"]
    return {
        "count": len(reports),
        "sizes": sorted({f"{r['format']['width']}x{r['format']['height']}@{r['format']['fps']}" for r in reports}),
        "lufs_range": [min(lufs), max(lufs)] if lufs else None,
        "clipping_references": sum(p > -1.0 for p in peaks),
        "cuts_per_minute_range": [min(cpm), max(cpm)] if cpm else None,
        "light_transition": light,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure reference videos for style learning")
    parser.add_argument("inputs", nargs="+", type=Path, help="Video files or folders")
    parser.add_argument("-o", "--output", type=Path, default=Path("reference_analysis.json"))
    args = parser.parse_args()
    files = collect(args.inputs)
    if not files:
        raise SystemExit("no video files found")
    reports = []
    for path in files:
        print(f"analyzing {path.name}")
        reports.append(analyze(path))
    result = {"summary": summarize(reports), "references": reports}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    print(f"→ {args.output}")


if __name__ == "__main__":
    main()
