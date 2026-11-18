"""Validate a Video Use EDL before spending time on a render.

Validation is intentionally independent of the renderer: an agent can inspect
the report, repair a plan, and only then invoke FFmpeg.  It validates objective
constraints; subjective editorial quality remains a later QC decision.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from typing import Any


def issue(level: str, code: str, message: str, path: str | None = None) -> dict[str, str]:
    item = {"level": level, "code": code, "message": message}
    if path:
        item["path"] = path
    return item


def resolve_path(value: str, base: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (base / path).resolve()


def probe_duration(path: Path) -> float | None:
    """Return container duration, or None when ffprobe cannot determine it."""
    command = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "json", str(path),
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        value = json.loads(result.stdout).get("format", {}).get("duration")
        duration = float(value)
        return duration if math.isfinite(duration) and duration >= 0 else None
    except (OSError, subprocess.CalledProcessError, ValueError, TypeError, json.JSONDecodeError):
        return None


def transcript_edge_warning(
    transcript_path: Path, start: float, end: float, source_duration: float | None
) -> list[dict[str, str]]:
    """Warn when a non-terminal cut is far from a transcript word edge.

    ASR timestamps drift, so this is deliberately a warning rather than a hard
    error.  The editor gets the evidence without blocking intentional choices.
    """
    if not transcript_path.exists():
        return [issue("warning", "transcript_missing", "No cached transcript; word-boundary cuts cannot be checked.", str(transcript_path))]
    try:
        words = json.loads(transcript_path.read_text(encoding="utf-8")).get("words", [])
        edges = [float(w[key]) for w in words if w.get("type", "word") == "word" for key in ("start", "end") if w.get(key) is not None]
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return [issue("warning", "transcript_unreadable", "Transcript could not be read for word-boundary checks.", str(transcript_path))]
    if not edges:
        return [issue("warning", "transcript_has_no_words", "Transcript has no word timestamps.", str(transcript_path))]

    findings: list[dict[str, str]] = []
    for label, value, terminal in (("start", start, start <= 0.03), ("end", end, source_duration is not None and abs(end - source_duration) <= 0.03)):
        if terminal:
            continue
        delta = min(abs(value - edge) for edge in edges)
        if delta > 0.22:
            findings.append(issue("warning", "cut_not_near_word_edge", f"Range {label} is {delta:.3f}s from the nearest transcript word edge.", label))
    return findings


FRAMING_MODES = {"cover", "fit", "blur_fit"}


def probe_display_size(path: Path) -> tuple[int, int] | None:
    command = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height:stream_side_data=rotation", "-of", "json", str(path),
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        stream = json.loads(result.stdout)["streams"][0]
        width, height = int(stream["width"]), int(stream["height"])
        rotation = next((d.get("rotation") for d in stream.get("side_data_list") or [] if d.get("rotation") is not None), 0)
        if int(round(float(rotation))) % 360 in (90, 270):
            width, height = height, width
        return width, height
    except (OSError, subprocess.CalledProcessError, ValueError, TypeError, KeyError, IndexError, json.JSONDecodeError):
        return None


def validate_canvas(canvas: Any, report: dict[str, Any]) -> tuple[int, int] | None:
    if canvas is None:
        return None
    try:
        if not isinstance(canvas, dict):
            raise ValueError
        width, height = int(canvas["width"]), int(canvas["height"])
        if not (16 <= width <= 8192 and 16 <= height <= 8192) or width % 2 or height % 2:
            raise ValueError
        return width, height
    except (KeyError, TypeError, ValueError):
        report["errors"].append(issue("error", "invalid_canvas", "canvas must be {width, height} with even integers from 16 to 8192.", "canvas"))
        return None


def validate_framing(
    edl: dict[str, Any], sources: dict[str, Any], base: Path,
    canvas: tuple[int, int] | None, report: dict[str, Any],
) -> None:
    framing = edl.get("source_framing") or {}
    if not isinstance(framing, dict):
        report["errors"].append(issue("error", "invalid_source_framing", "source_framing must map source ids to framing objects.", "source_framing"))
        return
    for source_id, value in framing.items():
        path = f"source_framing.{source_id}"
        if source_id not in sources:
            report["errors"].append(issue("error", "unknown_source", f"source_framing references unknown source '{source_id}'.", path))
            continue
        try:
            if not isinstance(value, dict) or value.get("mode", "cover") not in FRAMING_MODES:
                raise ValueError
            for key in ("anchor_x", "anchor_y"):
                if key in value and not 0.0 <= float(value[key]) <= 1.0:
                    raise ValueError
            if value.get("anchor") not in (None, "face"):
                raise ValueError
        except (TypeError, ValueError):
            report["errors"].append(issue("error", "invalid_source_framing", "Framing mode must be cover, fit, or blur_fit; anchors must be from 0 to 1.", path))

    # A landscape source on a portrait canvas (or the reverse) under the
    # default `cover` mode keeps only a narrow slice of the frame. That can be
    # right for a centred talking head, but it must be a decision.
    if canvas is None:
        return
    canvas_portrait = canvas[1] > canvas[0]
    rotations = edl.get("source_rotations") or {}
    for source_id, raw_path in sources.items():
        source_framing = framing.get(source_id) or {}
        if not isinstance(raw_path, str) or not isinstance(source_framing, dict) or rotations.get(source_id):
            continue
        if (source_framing.get("mode", "cover") != "cover" or "anchor_x" in source_framing
                or source_framing.get("anchor") == "face"):
            continue
        # Ranges that place their own window (camera x keys / face tracking) chose the crop.
        own = [r for r in edl.get("ranges") or [] if isinstance(r, dict) and r.get("source") == source_id]
        if own and all(r.get("track") == "face" or any("x" in k for k in r.get("camera") or []) for r in own):
            continue
        size = probe_display_size(resolve_path(raw_path, base))
        if size and (size[1] > size[0]) != canvas_portrait:
            report["warnings"].append(issue(
                "warning", "orientation_crop",
                f"Source {size[0]}x{size[1]} is cover-cropped into a {canvas[0]}x{canvas[1]} canvas. "
                "Use anchor: \"face\", choose an anchor_x/anchor_y on the subject, or use fit / blur_fit.",
                f"source_framing.{source_id}",
            ))


TRANSITION_TYPES = {"dissolve", "dip_black", "dip_white", "whip", "slide", "zoom", "wipe", "light", "fade"}


def validate_extras(edl: dict[str, Any], base: Path) -> list[tuple[str, str, str, str]]:
    """Transitions, J/L cuts, auto punch, LUT grade, captions, deliverables."""
    from camera import validate_motion
    out: list[tuple[str, str, str, str]] = []
    sources = edl.get("sources") or {}
    for k, beat in enumerate(edl.get("beats") or []):
        if not isinstance(beat, dict) or beat.get("type") != "denial" or not ("at" in beat or "start_in_output" in beat):
            out.append(("error", "invalid_beat", 'beats need {"type": "denial", "at": {...} | "start_in_output"}.', f"beats[{k}]"))
    rules = edl.get("transitions")
    if rules is not None:
        broll = rules.get("broll") if isinstance(rules, dict) else None
        kind = broll if isinstance(broll, str) else (broll or {}).get("type") if isinstance(broll, dict) else None
        if not isinstance(rules, dict) or (broll is not None and kind not in TRANSITION_TYPES):
            out.append(("error", "invalid_transition", f'transitions.broll needs a type from {sorted(TRANSITION_TYPES)}.', "transitions"))
    for i, r in enumerate(edl.get("ranges") or []):
        if not isinstance(r, dict):
            continue
        path = f"ranges[{i}]"
        out += validate_motion(r, path)
        audio_source = r.get("audio_source")
        if audio_source is not None:
            try:
                if not isinstance(audio_source, dict) or audio_source.get("source") not in sources:
                    raise ValueError
                if float(audio_source.get("start", 0)) < 0:
                    raise ValueError
            except (TypeError, ValueError):
                out.append(("error", "invalid_audio_source", "audio_source needs a known source and start ≥ 0.", f"{path}.audio_source"))
        transition = r.get("transition")
        if transition is not None and transition is not False and transition != "cut":
            spec = {"type": transition} if isinstance(transition, str) else transition
            if i == 0:
                out.append(("error", "invalid_transition", "The first range has no incoming cut to transition.", f"{path}.transition"))
            elif not isinstance(spec, dict) or spec.get("type", "dissolve") not in TRANSITION_TYPES:
                out.append(("error", "invalid_transition", f"type must be one of {sorted(TRANSITION_TYPES)}.", f"{path}.transition"))
            else:
                try:
                    if not 80 <= float(spec.get("duration_ms", 400)) <= 2000:
                        raise ValueError
                except (TypeError, ValueError):
                    out.append(("error", "invalid_transition", "duration_ms must be 80–2000.", f"{path}.transition.duration_ms"))
        if r.get("audio_gain_db") is not None:
            try:
                if not -24 <= float(r["audio_gain_db"]) <= 12:
                    raise ValueError
            except (TypeError, ValueError):
                out.append(("error", "invalid_audio_gain", "audio_gain_db must be -24..12 dB.", f"{path}.audio_gain_db"))
        for key in ("j_cut_ms", "l_cut_ms"):
            if r.get(key) is None:
                continue
            try:
                value = float(r[key])
                if i == 0 or not 0 <= value <= 1500:
                    raise ValueError
            except (TypeError, ValueError):
                out.append(("error", "invalid_split_edit", f"{key} must be 0–1500 ms and not on the first range.", f"{path}.{key}"))
        if r.get("j_cut_ms") and r.get("l_cut_ms"):
            out.append(("error", "invalid_split_edit", "Use either j_cut_ms or l_cut_ms on one cut, not both.", path))
    auto = edl.get("auto_punch")
    if isinstance(auto, dict):
        try:
            if not 1.03 <= float(auto.get("scale", 1.1)) <= 1.25:
                raise ValueError
        except (TypeError, ValueError):
            out.append(("error", "invalid_auto_punch", "auto_punch.scale must be 1.03–1.25.", "auto_punch.scale"))
    elif auto not in (None, True, False):
        out.append(("error", "invalid_auto_punch", "auto_punch must be true or an object.", "auto_punch"))
    grade = edl.get("grade")
    if isinstance(grade, dict):
        lut = grade.get("lut")
        if lut and not resolve_path(str(lut), base).is_file():
            out.append(("error", "lut_missing", f"LUT {lut} not found.", "grade.lut"))
        try:
            if not 0 <= float(grade.get("intensity", 1)) <= 1:
                raise ValueError
        except (TypeError, ValueError):
            out.append(("error", "invalid_grade", "grade.intensity must be 0–1.", "grade.intensity"))
    if isinstance(grade, dict) and grade.get("match") is not None and grade["match"] not in sources:
        out.append(("error", "invalid_grade", "grade.match must name a source (the reference camera).", "grade.match"))
    multicam = edl.get("multicam")
    if multicam is not None:
        if not isinstance(multicam, dict) or multicam.get("master") not in sources or not isinstance(multicam.get("angles"), dict):
            out.append(("error", "invalid_multicam", "multicam needs master (a source) and angles {source: {speaker|wide}}.", "multicam"))
        else:
            for name in multicam["angles"]:
                if name not in sources:
                    out.append(("error", "invalid_multicam", f"angle {name!r} is not a source.", f"multicam.angles.{name}"))
    captions = edl.get("captions")
    if captions is not None and not isinstance(captions, (bool, dict)):
        out.append(("error", "invalid_captions", "captions must be true or an object of caption-skin overrides.", "captions"))
    if isinstance(captions, dict) and "position" in captions and not 0.05 <= float(captions["position"]) <= 0.95:
        out.append(("error", "invalid_captions", "captions.position must be 0.05–0.95.", "captions.position"))
    for k, item in enumerate(edl.get("deliverables") or []):
        if not isinstance(item, dict) or not str(item.get("name", "")).strip():
            out.append(("error", "invalid_deliverable", "Every deliverable needs a name.", f"deliverables[{k}]"))
            continue
        canvas = item.get("canvas")
        if canvas is not None:
            scratch: dict[str, Any] = {"errors": [], "warnings": []}
            validate_canvas(canvas, scratch)
            if scratch["errors"]:
                out.append(("error", "invalid_deliverable", "canvas needs even integer width/height (16–8192).", f"deliverables[{k}].canvas"))
    return out


def validate_edl(edl_path: Path) -> dict[str, Any]:
    report: dict[str, Any] = {"edl": str(edl_path), "valid": False, "errors": [], "warnings": [], "expected_duration_s": 0.0}
    try:
        edl = json.loads(edl_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        report["errors"].append(issue("error", "invalid_json", f"Could not read EDL JSON: {exc}"))
        return report
    if not isinstance(edl, dict):
        report["errors"].append(issue("error", "edl_not_object", "The EDL root must be a JSON object."))
        return report

    base = edl_path.parent
    sources = edl.get("sources")
    ranges = edl.get("ranges")
    if not isinstance(sources, dict) or not sources:
        report["errors"].append(issue("error", "sources_required", "EDL must contain a non-empty sources object.", "sources"))
        sources = {}
    if not isinstance(ranges, list) or not ranges:
        report["errors"].append(issue("error", "ranges_required", "EDL must contain at least one range.", "ranges"))
        ranges = []

    durations: dict[str, float | None] = {}
    for source_id, raw_path in sources.items():
        if not isinstance(source_id, str) or not isinstance(raw_path, str):
            report["errors"].append(issue("error", "invalid_source", "Every source id and source path must be a string.", f"sources.{source_id}"))
            continue
        path = resolve_path(raw_path, base)
        if not path.is_file():
            report["errors"].append(issue("error", "source_missing", f"Source file does not exist: {path}", f"sources.{source_id}"))
            durations[source_id] = None
            continue
        durations[source_id] = probe_duration(path)
        if durations[source_id] is None:
            report["warnings"].append(issue("warning", "source_duration_unknown", f"Could not determine source duration: {path}", f"sources.{source_id}"))

    total = 0.0
    for index, item in enumerate(ranges):
        item_path = f"ranges[{index}]"
        if not isinstance(item, dict):
            report["errors"].append(issue("error", "range_not_object", "Every range must be an object.", item_path))
            continue
        source_id = item.get("source")
        if source_id not in sources:
            report["errors"].append(issue("error", "unknown_source", f"Range references unknown source '{source_id}'.", f"{item_path}.source"))
            continue
        try:
            start, end = float(item["start"]), float(item["end"])
        except (KeyError, TypeError, ValueError):
            report["errors"].append(issue("error", "invalid_range_time", "Range start and end must be numbers.", item_path))
            continue
        if not all(math.isfinite(value) for value in (start, end)) or start < 0 or end <= start:
            report["errors"].append(issue("error", "invalid_range_bounds", "Range must satisfy 0 <= start < end.", item_path))
            continue
        source_duration = durations.get(source_id)
        if source_duration is not None and end > source_duration + 0.03:
            report["errors"].append(issue("error", "range_exceeds_source", f"Range ends at {end:.3f}s but source duration is {source_duration:.3f}s.", item_path))
        punch_in = item.get("punch_in")
        if punch_in is not None:
            try:
                if not isinstance(punch_in, dict):
                    raise ValueError
                scale = float(punch_in["scale"])
                if not 1.03 <= scale <= 1.20:
                    raise ValueError
                for key in ("anchor_x", "anchor_y"):
                    if key in punch_in and not 0.0 <= float(punch_in[key]) <= 1.0:
                        raise ValueError
            except (KeyError, TypeError, ValueError):
                report["errors"].append(issue("error", "invalid_punch_in", "punch_in must be an object with scale from 1.03 to 1.20 and optional anchor_x/anchor_y from 0 to 1.", f"{item_path}.punch_in"))
            else:
                if not str(punch_in.get("reason", "")).strip():
                    report["warnings"].append(issue("warning", "punch_in_without_reason", "Record why this punch-in helps the edit.", f"{item_path}.punch_in.reason"))
        total += end - start
        # Cut edges are checked where the *sound* comes from (B-roll keeps the
        # interview's audio through audio_source).
        from transcripts import transcript_path
        sound = item.get("audio_source") if isinstance(item.get("audio_source"), dict) else None
        if sound and sound.get("source") in sources:
            s_start = float(sound.get("start", start))
            report["warnings"].extend(transcript_edge_warning(transcript_path(base, edl, sound["source"]), s_start, s_start + end - start, None))
        else:
            report["warnings"].extend(transcript_edge_warning(transcript_path(base, edl, source_id), start, end, source_duration))

    report["expected_duration_s"] = round(total, 3)
    declared = edl.get("total_duration_s")
    if declared is not None:
        try:
            if abs(float(declared) - total) > 0.1:
                report["warnings"].append(issue("warning", "declared_duration_mismatch", f"total_duration_s is {float(declared):.3f}s; ranges add up to {total:.3f}s.", "total_duration_s"))
        except (TypeError, ValueError):
            report["errors"].append(issue("error", "invalid_declared_duration", "total_duration_s must be numeric.", "total_duration_s"))

    for overlay_key in ("overlays", "behind_subject_overlays"):
        for index, overlay in enumerate(edl.get(overlay_key) or []):
            item_path = f"{overlay_key}[{index}]"
            if not isinstance(overlay, dict):
                report["errors"].append(issue("error", "overlay_not_object", "Every overlay must be an object.", item_path))
                continue
            try:
                start, duration = float(overlay["start_in_output"]), float(overlay["duration"])
            except (KeyError, TypeError, ValueError):
                report["errors"].append(issue("error", "invalid_overlay_time", "Overlay start_in_output and duration must be numbers.", item_path))
                continue
            if start < 0 or duration <= 0 or start + duration > total + 0.03:
                report["errors"].append(issue("error", "overlay_outside_output", "Overlay must fall within the output timeline.", item_path))
            raw_path = overlay.get("file")
            if not isinstance(raw_path, str) or not resolve_path(raw_path, base).is_file():
                report["errors"].append(issue("error", "overlay_missing", "Overlay file is missing.", f"{item_path}.file"))

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from graphics import validate_graphics
    from audio_mix import validate_audio
    for level, code, message, item_path in [*validate_graphics(edl, total), *validate_audio(edl, base, total),
                                            *validate_extras(edl, base)]:
        report["errors" if level == "error" else "warnings"].append(issue(level, code, message, item_path))

    canvas = validate_canvas(edl.get("canvas"), report)
    validate_framing(edl, sources, base, canvas, report)

    audio = edl.get("audio")
    if audio is not None:
        try:
            if not isinstance(audio, dict):
                raise ValueError
            crossfade = float(audio.get("crossfade_ms", 30))
            if not 4.0 <= crossfade <= 200.0:
                raise ValueError
        except (TypeError, ValueError):
            report["errors"].append(issue("error", "invalid_audio", "audio.crossfade_ms must be a number from 4 to 200.", "audio.crossfade_ms"))

    behind_subject = edl.get("behind_subject_overlays") or []
    subject_mask = edl.get("subject_mask")
    if behind_subject:
        if not isinstance(subject_mask, dict) or subject_mask.get("mode") != "person":
            report["errors"].append(issue("error", "subject_mask_required", "behind_subject_overlays require subject_mask.mode set to 'person'.", "subject_mask"))
        elif subject_mask.get("quality", "balanced") not in {"fast", "balanced", "accurate"}:
            report["errors"].append(issue("error", "invalid_subject_mask_quality", "subject_mask quality must be fast, balanced, or accurate.", "subject_mask.quality"))
        elif subject_mask.get("fps") is not None:
            try:
                if float(subject_mask["fps"]) <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                report["errors"].append(issue("error", "invalid_subject_mask_fps", "subject_mask fps must be a positive number when specified.", "subject_mask.fps"))

    report["valid"] = not report["errors"]
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate a Video Use EDL before render")
    parser.add_argument("edl", type=Path)
    parser.add_argument("--report", type=Path, help="Where to write validation JSON (defaults beside the EDL)")
    parser.add_argument("--strict", action="store_true", help="Treat warnings as a non-zero exit status")
    args = parser.parse_args()
    edl_path = args.edl.resolve()
    report = validate_edl(edl_path)
    report_path = args.report or edl_path.parent / "validation_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"validation report → {report_path}")
    print(f"errors: {len(report['errors'])}, warnings: {len(report['warnings'])}")
    raise SystemExit(0 if report["valid"] and (not args.strict or not report["warnings"]) else 1)


if __name__ == "__main__":
    main()
