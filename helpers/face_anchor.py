"""Face-anchored framing: where is the face in each kept range?

Used for ``"anchor": "face"`` in ``source_framing`` / ``punch_in`` and for
``auto_punch``. Faces come from local macOS Vision; results are cached per
source in ``<edit>/faces/<source>.json`` so re-renders are free.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from statistics import median

SWIFT_SOURCE = Path(__file__).with_name("face_anchor.swift")
BINARY = Path(tempfile.gettempdir()) / "video-use-face-anchor"


def _binary() -> Path:
    if sys.platform != "darwin" or not shutil.which("swiftc"):
        raise RuntimeError("face anchoring uses macOS Vision")
    if not BINARY.exists() or BINARY.stat().st_mtime < SWIFT_SOURCE.stat().st_mtime:
        subprocess.run(["swiftc", str(SWIFT_SOURCE), "-o", str(BINARY), "-framework", "AVFoundation",
                        "-framework", "Vision"], check=True, capture_output=True)
    return BINARY


def rotate_point(x: float, y: float, rotation: str | None) -> tuple[float, float]:
    """Map a point from raw pixels into the manually rotated picture."""
    if rotation == "cw":
        return 1 - y, x
    if rotation == "ccw":
        return y, 1 - x
    return x, y


def detect_opencv(video: Path, times: list[float]) -> list[dict]:
    """Fallback face detector (Linux / no Vision): OpenCV Haar cascade."""
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("face anchoring needs macOS Vision or `pip install opencv-python-headless`") from exc
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    capture = cv2.VideoCapture(str(video))
    results = []
    for t in times:
        capture.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, frame = capture.read()
        faces = []
        if ok:
            h, w = frame.shape[:2]
            scale = 640 / max(w, h)
            small = cv2.resize(frame, (round(w * scale), round(h * scale)))
            gray = cv2.equalizeHist(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY))
            for x, y, fw, fh in cascade.detectMultiScale(gray, 1.1, 5, minSize=(24, 24)):
                sh, sw = gray.shape
                faces.append({"x": x / sw, "y": y / sh, "w": fw / sw, "h": fh / sh})
        results.append({"t": t, "faces": faces})
    capture.release()
    return results


def detect(video: Path, times: list[float], cache: Path) -> dict[str, list[dict]]:
    """Faces per timestamp (rounded to 0.1 s), cached on disk."""
    known: dict[str, list[dict]] = json.loads(cache.read_text()) if cache.exists() else {}
    todo = [t for t in times if f"{t:.1f}" not in known]
    if todo:
        try:
            out = subprocess.run([str(_binary()), str(video), ",".join(f"{t:.1f}" for t in todo)],
                                 check=True, capture_output=True, text=True).stdout
            found = json.loads(out or "[]")
        except RuntimeError:
            found = detect_opencv(video, todo)
        for item in found:
            known[f"{float(item['t']):.1f}"] = item["faces"]
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(known))
    return known


def sample_times(start: float, end: float, count: int = 3) -> list[float]:
    span = max(0.0, end - start)
    return [round(start + span * (i + 0.5) / count, 1) for i in range(count)]


def subject_position(faces_by_time: dict[str, list[dict]], times: list[float]) -> tuple[float, float] | None:
    """Where the *subject* is: the face seen most often in one place.

    In an interview two-shot the person answering faces the camera and is
    detected in nearly every sample; the interviewer is often in profile and
    detected only now and then. Size alone picks the wrong person.
    """
    centres = [(f["x"] + f["w"] / 2, f["y"] + f["h"] / 2, f["w"] * f["h"])
               for t in times for f in (faces_by_time.get(f"{t:.1f}") or [])]
    if not centres:
        return None
    def support(c):
        return sum(1 for o in centres if abs(o[0] - c[0]) < 0.08 and abs(o[1] - c[1]) < 0.08)
    best = max(centres, key=lambda c: (support(c), c[2]))
    return best[0], best[1]


def face_center(faces_by_time: dict[str, list[dict]], times: list[float],
                rotation: str | None = None) -> tuple[float, float] | None:
    """Median centre of the subject's face across the sampled times."""
    anchor = subject_position(faces_by_time, times)
    xs, ys = [], []
    for t in times:
        faces = faces_by_time.get(f"{t:.1f}") or []
        if not faces or anchor is None:
            continue
        face = min(faces, key=lambda f: (f["x"] + f["w"] / 2 - anchor[0]) ** 2 + (f["y"] + f["h"] / 2 - anchor[1]) ** 2)
        x, y = rotate_point(face["x"] + face["w"] / 2, face["y"] + face["h"] / 2, rotation)
        xs.append(x)
        ys.append(y)
    if not xs:
        return None
    return round(median(xs), 4), round(median(ys), 4)


def range_face_centers(edl: dict, edit_dir: Path, resolve) -> list[tuple[float, float] | None]:
    """Face centre for every range (None where no face was found)."""
    rotations = edl.get("source_rotations") or {}
    centers: list[tuple[float, float] | None] = []
    per_source: dict[str, list[float]] = {}
    ranges = edl.get("ranges") or []
    for r in ranges:
        per_source.setdefault(r["source"], []).extend(sample_times(float(r["start"]), float(r["end"])))
    detected = {
        name: detect(resolve(edl["sources"][name], edit_dir), times, edit_dir / "faces" / f"{name}.json")
        for name, times in per_source.items()
    }
    for r in ranges:
        times = sample_times(float(r["start"]), float(r["end"]))
        centers.append(face_center(detected[r["source"]], times, rotations.get(r["source"])))
    return centers


def _auto_config(value) -> dict | None:
    if value is True:
        return {}
    if isinstance(value, dict) and value.get("enabled", True):
        return value
    return None


def needs_faces(edl: dict) -> bool:
    if _auto_config(edl.get("auto_punch")) is not None:
        return True
    if any((f or {}).get("anchor") == "face" for f in (edl.get("source_framing") or {}).values()):
        return True
    if any(r.get("track") == "face" for r in edl.get("ranges") or []):
        return True
    return any((r.get("punch_in") or {}).get("anchor") == "face" for r in edl.get("ranges") or [])


def plan_auto_punch(ranges: list[dict], config: dict | None) -> list[dict | None]:
    """Alternate framing on jump cuts: consecutive ranges of one source whose
    source gap is short read as a jump, so every other one is punched in.

    A hand-set ``punch_in`` always wins and restarts the alternation.
    """
    punches: list[dict | None] = [r.get("punch_in") for r in ranges]
    config = _auto_config(config)
    if config is None:
        return punches
    scale = float(config.get("scale", 1.1))
    max_gap = float(config.get("max_gap_s", 20.0))
    tight = False  # whether the previous range was punched in
    for i, r in enumerate(ranges):
        if r.get("punch_in"):
            tight = float(r["punch_in"].get("scale", 1)) > 1.0
            continue
        jump = (i > 0 and ranges[i - 1]["source"] == r["source"]
                and 0 <= float(r["start"]) - float(ranges[i - 1]["end"]) <= max_gap)
        if jump and not tight:
            punches[i] = {"scale": scale, "anchor": "face", "auto": True,
                          "reason": "auto: alternate framing on a jump cut"}
            tight = True
        else:
            tight = False
    return punches


def track_samples(video: Path, start: float, end: float, cache: Path, rotation: str | None = None,
                  step: float = 0.2) -> list[tuple[float, float | None, float | None]]:
    """Largest-face centre every ``step`` seconds, times relative to ``start``."""
    count = max(2, int((end - start) / step) + 1)
    times = [round(start + i * (end - start) / (count - 1), 1) for i in range(count)]
    faces = detect(video, times, cache)
    out = []
    for t in times:
        found = faces.get(f"{t:.1f}") or []
        if found:
            anchor = subject_position(faces, times)
            face = min(found, key=lambda f: (f["x"] + f["w"] / 2 - anchor[0]) ** 2 + (f["y"] + f["h"] / 2 - anchor[1]) ** 2)
            x, y = rotate_point(face["x"] + face["w"] / 2, face["y"] + face["h"] / 2, rotation)
            out.append((round(t - start, 3), x, y))
        else:
            out.append((round(t - start, 3), None, None))
    return out


BANDS = ((0.0, 0.4), (0.3, 0.7), (0.6, 1.0))


def band_faces(video: Path, times: list[float], cache_dir: Path, key: str) -> dict[float, list[dict]]:
    """Faces on the full frame plus three overlapping bands across its long side.

    Small faces — a crowd, a split-screen or stacked grid, a wide shot — are
    missed on the full frame but found reliably once a band is cropped out,
    because the detector sees them three times larger. Coordinates come back
    normalised to the full frame.
    """
    out: dict[float, list[dict]] = {t: list(detect(video, times, cache_dir / f"{key}.json").get(f"{t:.1f}") or []) for t in times}
    size = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                           "-of", "csv=p=0:s=x", str(video)], capture_output=True, text=True).stdout.strip().split("x")
    portrait = len(size) >= 2 and int(size[1]) >= int(size[0])
    with tempfile.TemporaryDirectory() as tmp:
        for k, (lo, hi) in enumerate(BANDS):
            band_cache = cache_dir / f"{key}_band{k}.json"
            band = Path(tmp) / f"band{k}.mp4"
            if not band_cache.exists():
                crop = (f"crop=iw:ih*{hi - lo}:0:ih*{lo}" if portrait else f"crop=iw*{hi - lo}:ih:iw*{lo}:0")
                subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-an", "-vf", crop, "-c:v", "libx264",
                                "-preset", "ultrafast", "-crf", "20", str(band)], check=True)
            found = detect(band, times, band_cache)
            for t in times:
                for f in found.get(f"{t:.1f}") or []:
                    g = dict(f)
                    if portrait:
                        g["y"], g["h"] = lo + f["y"] * (hi - lo), f["h"] * (hi - lo)
                    else:
                        g["x"], g["w"] = lo + f["x"] * (hi - lo), f["w"] * (hi - lo)
                    out[t].append(g)
    return out


def output_face_boxes(video: Path, canvas: tuple[int, int], cache_dir: Path, step: float = 0.4) -> list[dict]:
    """Face keep-out boxes on the assembled output (pixels + time windows).

    Detected on the rendered base (after cuts, framing, B-roll), so they are
    exactly where faces appear on screen. Each box is grown to cover hair,
    forehead, and chin: text must never sit on a face.
    """
    import subprocess as _sp
    duration = float(_sp.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                              str(video)], capture_output=True, text=True).stdout.strip() or 0)
    stat = video.stat()
    cache = cache_dir / f"output_{video.stem}_{int(stat.st_mtime)}_{stat.st_size}.json"
    times = [round(i * step, 1) for i in range(int(duration / step) + 1)]
    found = band_faces(video, times, cache_dir, cache.stem)
    w, h = canvas
    boxes = []
    for t, faces in found.items():
        for f in faces:
            fx, fy, fw, fh = f["x"] * w, f["y"] * h, f["w"] * w, f["h"] * h
            # Held one sample either side: detection flickers, a face doesn't.
            boxes.append({"type": "face", "start_in_output": max(0.0, t - 1.5 * step), "duration": 3 * step,
                          "x": fx - 0.35 * fw, "y": fy - 0.6 * fh, "w": fw * 1.7, "h": fh * 1.9})
    return boxes
