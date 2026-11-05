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


def detect(video: Path, times: list[float], cache: Path) -> dict[str, list[dict]]:
    """Faces per timestamp (rounded to 0.1 s), cached on disk."""
    known: dict[str, list[dict]] = json.loads(cache.read_text()) if cache.exists() else {}
    todo = [t for t in times if f"{t:.1f}" not in known]
    if todo:
        out = subprocess.run([str(_binary()), str(video), ",".join(f"{t:.1f}" for t in todo)],
                             check=True, capture_output=True, text=True).stdout
        for item in json.loads(out or "[]"):
            known[f"{float(item['t']):.1f}"] = item["faces"]
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(known))
    return known


def sample_times(start: float, end: float, count: int = 3) -> list[float]:
    span = max(0.0, end - start)
    return [round(start + span * (i + 0.5) / count, 1) for i in range(count)]


def face_center(faces_by_time: dict[str, list[dict]], times: list[float],
                rotation: str | None = None) -> tuple[float, float] | None:
    """Median centre of the largest face across the sampled times."""
    xs, ys = [], []
    for t in times:
        faces = faces_by_time.get(f"{t:.1f}") or []
        if not faces:
            continue
        face = max(faces, key=lambda f: f["w"] * f["h"])
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
