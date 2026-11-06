"""Render a video from an EDL.

Implements the HEURISTICS render pipeline in the correct order:

  1. Per-segment picture extract, frame-exact, conformed to ONE canvas,
     graded (auto grade is measured once per source)
  2. Lossless -c copy concat into base.mp4 (picture only)
  3. Audio master built separately as PCM: every cut gets an equal-power
     micro-crossfade centered on the picture cut (no dip to silence)
  4. Single final pass: overlays (PTS-shifted so frame 0 lands at the window
     start), `subtitles` LAST, loudnorm, and the ONLY AAC encode → final.mp4

Optionally builds a master SRT from the per-source transcripts + EDL
output-timeline offsets, applies the proven force_style (2-word
UPPERCASE chunks, Helvetica 18 Bold, MarginV=90).

Usage:
    python helpers/render.py <edl.json> -o final.mp4
    python helpers/render.py <edl.json> -o preview.mp4 --preview
    python helpers/render.py <edl.json> -o final.mp4 --build-subtitles
    python helpers/render.py <edl.json> -o final.mp4 --no-subtitles
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from grade import get_preset, auto_grade_for_ranges  # same directory
except Exception:
    def get_preset(name: str) -> str:
        return ""

    def auto_grade_for_ranges(video, spans, verbose=False):  # type: ignore
        return "eq=contrast=1.03:saturation=0.98", {}


# -------- Subtitle style (bold-overlay, proven at 1920×1080 and 1080×1920) --
#
# MarginV is NOT taste — it is a platform safe-zone rule.
# TikTok / IG Reels / Shorts UI (caption, username, music, right-rail actions)
# covers roughly the bottom ~25–30% of a 1080×1920 frame. Captions placed near
# the bottom edge get clipped or obscured by the UI. libass auto-scales the
# render canvas relative to PlayResY=288, so MarginV=90 lands the caption
# baseline roughly 30% up from the bottom on any aspect — clear of the UI on
# every major vertical-video platform. Do not drop this below ~75 without a
# specific reason.
SUB_FORCE_STYLE = (
    "FontName=Helvetica,FontSize=18,Bold=1,"
    "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H00000000,"
    "BorderStyle=1,Outline=2,Shadow=0,"
    "Alignment=2,MarginV=90"
)

# -------- Helpers ------------------------------------------------------------


def run(cmd: list[str], quiet: bool = False) -> None:
    if not quiet:
        print(f"  $ {' '.join(str(c) for c in cmd[:6])}{' …' if len(cmd) > 6 else ''}")
    subprocess.run(cmd, check=True)


def resolve_grade_filter(grade_field: str | None) -> str:
    """The EDL's 'grade' field can be a preset name, a raw ffmpeg filter, or 'auto'.

    Returns the filter string to embed into the per-segment -vf chain.
    For 'auto', returns the sentinel "__AUTO__" which is resolved per-segment.
    """
    if not grade_field:
        return ""
    if grade_field == "auto":
        return "__AUTO__"
    # Preset names are short identifiers, filter strings contain '=' or ','.
    if re.fullmatch(r"[a-zA-Z0-9_\-]+", grade_field):
        try:
            return get_preset(grade_field)
        except KeyError:
            print(f"warning: unknown preset '{grade_field}', using as raw filter")
            return grade_field
    return grade_field


def resolve_path(maybe_path: str, base: Path) -> Path:
    """Resolve a path that may be absolute or relative to `base`."""
    p = Path(maybe_path)
    if p.is_absolute():
        return p
    return (base / p).resolve()


def punch_in_filter(punch_in: dict | None, output_scale: str = "scale=1920:-2") -> str:
    """Return a centered, static punch-in that preserves the output dimensions.

    A punch-in belongs to a chosen editorial range, not to a random transcript
    keyword.  Motion ramps need a dedicated visual decision and are therefore
    intentionally not faked here with a generic zoompan animation.
    """
    if not punch_in:
        return ""
    scale = float(punch_in["scale"])
    # Crop first, then force the standard delivery canvas. A rounded crop can
    # change its display ratio by a fraction of a pixel; `scale=1920:-2` would
    # then produce (for example) 1920x1078 instead of 1920x1080 and lossless
    # concat would reject this one segment.
    standard_canvas = {
        "scale=1920:-2": "scale=1920:1080",
        "scale=-2:1920": "scale=1080:1920",
        "scale=1280:-2": "scale=1280:720",
        "scale=-2:1280": "scale=720:1280",
    }.get(output_scale, output_scale)
    return (
        f"crop=trunc(iw/{scale:.3f}/2)*2:trunc(ih/{scale:.3f}/2)*2:"
        f"(iw-ow)/2:(ih-oh)/2,{standard_canvas}"
    )


def resolve_subtitles_path(maybe_path: str, edit_dir: Path) -> Path:
    """Resolve the EDL's subtitles path: relative to the EDL's directory, else the
    current directory (agents often write "edit/master.srt"). A missing file is an
    error: rendering on without it silently ships a video with no captions."""
    candidates = [resolve_path(maybe_path, edit_dir)]
    if not Path(maybe_path).is_absolute():
        candidates.append(Path(maybe_path).resolve())
    for c in candidates:
        if c.exists():
            return c
    tried = ", ".join(str(c) for c in candidates)
    sys.exit(f"subtitles file in EDL not found (tried {tried}). Fix the path or pass --no-subtitles.")


# -------- HDR → SDR tone mapping (HLG / PQ sources) --------------------------
#
# iPhone defaults to HLG HDR in Rec.2020 (and many mirrorless cameras ship PQ).
# If the source is HDR and we only downconvert bit depth (yuv420p10le → yuv420p)
# without tone-mapping, the output is 8-bit but still carries HLG/PQ transfer
# metadata. Players that honor the metadata (screen recorders, most social
# upload re-encodes) interpret 8-bit values in an HDR container and the result
# looks oversaturated / blown out. QuickTime on macOS can hide this locally —
# screen recording and uploaded renders cannot.
#
# Fix: detect HDR via color_transfer and prepend a zscale+tonemap chain to the
# vf graph so the output is clean Rec.709 SDR.

HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}  # PQ (HDR10) and HLG

TONEMAP_CHAIN = (
    "zscale=t=linear:npl=100,"
    "format=gbrpf32le,"
    "zscale=p=bt709,"
    "tonemap=tonemap=hable:desat=0,"
    "zscale=t=bt709:m=bt709:r=tv,"
    "format=yuv420p"
)


def is_hdr_source(video: Path) -> bool:
    """Return True if the source uses a PQ or HLG transfer function."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=color_transfer",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip() in HDR_TRANSFERS
    except subprocess.CalledProcessError:
        return False


def probe_display_size(video: Path) -> tuple[int, int] | None:
    """Return the (width, height) ffmpeg filters will see, rotation included."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries",
             "stream=width,height:stream_side_data=rotation",
             "-of", "json", str(video)],
            capture_output=True, text=True, check=True,
        )
        streams = json.loads(out.stdout).get("streams") or []
        if not streams:
            return None
        stream = streams[0]
        w, h = int(stream["width"]), int(stream["height"])

        # ffmpeg autorotates display-matrix side data before applying filters.
        # Swap coded dimensions for quarter-turns so the scale axis is selected
        # from the dimensions the filter actually sees. A plain metadata tag is
        # intentionally ignored because it does not guarantee autorotation.
        rotation = 0
        for side_data in stream.get("side_data_list") or []:
            if side_data.get("rotation") is not None:
                rotation = side_data["rotation"]
                break
        if int(round(float(rotation))) % 360 in (90, 270):
            w, h = h, w
        if w <= 0 or h <= 0:
            return None
        return w, h
    except (
        subprocess.CalledProcessError,
        json.JSONDecodeError,
        OSError,
        OverflowError,
        KeyError,
        TypeError,
        ValueError,
    ):
        return None


def is_portrait_source(video: Path) -> bool:
    """Return True if the displayed video is portrait, including rotation."""
    size = probe_display_size(video)
    return size is not None and size[1] > size[0]


# -------- Output canvas + framing --------------------------------------------
#
# Every segment is conformed to ONE canvas before concat. Scaling by width
# alone (the old `scale=1920:-2`) produced 1920x1440 for a 4:3 source and
# 1080x1920 for a portrait one; mixed sources then broke the lossless concat.


def _even(value: float) -> int:
    return max(2, int(round(value / 2.0)) * 2)


def default_canvas(portrait: bool, draft: bool = False) -> tuple[int, int]:
    if draft:
        return (720, 1280) if portrait else (1280, 720)
    return (1080, 1920) if portrait else (1920, 1080)


def resolve_canvas(edl: dict, edit_dir: Path, draft: bool = False) -> tuple[int, int]:
    """EDL `canvas` wins; otherwise follow the first range's source orientation."""
    canvas = edl.get("canvas")
    if isinstance(canvas, dict) and canvas.get("width") and canvas.get("height"):
        w, h = int(canvas["width"]), int(canvas["height"])
        if draft:
            factor = 720 / min(w, h) if min(w, h) > 720 else 1.0
            return _even(w * factor), _even(h * factor)
        return _even(w), _even(h)
    ranges = edl.get("ranges") or []
    portrait = False
    if ranges:
        src_name = ranges[0]["source"]
        src = resolve_path(edl["sources"][src_name], edit_dir)
        rotation = (edl.get("source_rotations") or {}).get(src_name)
        portrait = rotation is not None or is_portrait_source(src)
    return default_canvas(portrait, draft)


def _unit(value: object, default: float = 0.5) -> float:
    try:
        return min(1.0, max(0.0, float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


FACE_HEADROOM = 0.36


def framing_filter(
    canvas: tuple[int, int],
    source_size: tuple[int, int] | None,
    punch_in: dict | None = None,
    framing: dict | None = None,
) -> str:
    """Conform a source to the canvas, optionally punched in.

    Modes (per source, EDL `source_framing`):
      - cover (default): crop to the canvas aspect at an anchor, then scale.
        The punch-in crop is taken from the *source* pixels, so a 4K source
        loses no detail on a 1.1x punch.
      - fit: letterbox/pillarbox on black.
      - blur_fit: fit over a blurred, cover-cropped copy (vertical social look
        for landscape sources).
    `anchor_x` / `anchor_y` (0..1) choose which part of the frame survives
    the crop; a punch-in may override them for that range.
    """
    W, H = canvas
    framing = framing or {}
    mode = framing.get("mode", "cover")
    scale = float(punch_in["scale"]) if punch_in else 1.0
    ax = _unit((punch_in or {}).get("anchor_x", framing.get("anchor_x", 0.5)))
    ay = _unit((punch_in or {}).get("anchor_y", framing.get("anchor_y", 0.5)))
    finish = f"scale={W}:{H}:flags=lanczos,setsar=1"

    if mode == "fit":
        chain = (
            f"scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
        )
        if punch_in:
            chain += "," + punch_in_filter(punch_in, f"scale={W}:{H}")
        return chain

    if mode == "blur_fit":
        fg = f"scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos"
        bg = (
            f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
            "boxblur=luma_radius=40:luma_power=2,eq=brightness=-0.08"
        )
        chain = f"split[vu_bg][vu_fg];[vu_bg]{bg}[vu_bgb];[vu_fg]{fg}[vu_fgs];[vu_bgb][vu_fgs]overlay=(W-w)/2:(H-h)/2,setsar=1"
        if punch_in:
            chain += "," + punch_in_filter(punch_in, f"scale={W}:{H}")
        return chain

    if mode != "cover":
        raise ValueError("source_framing mode must be cover, fit, or blur_fit")

    if source_size is None:
        # Unknown source geometry: fill the canvas, then punch from the center.
        chain = (
            f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={W}:{H},setsar=1"
        )
        if punch_in:
            chain += "," + punch_in_filter(punch_in, f"scale={W}:{H}")
        return chain

    sw, sh = source_size
    target = W / H
    if sw / sh > target:
        cw, ch = sh * target, float(sh)
    else:
        cw, ch = float(sw), sw / target
    cw, ch = cw / scale, ch / scale
    cw = min(sw - sw % 2, max(2, int(cw) // 2 * 2))
    ch = min(sh - sh % 2, max(2, int(ch) // 2 * 2))
    face = (punch_in or {}).get("face_center") or framing.get("face_center")
    if face:
        # Face-anchored: face centre on the canvas's vertical centre line, at
        # FACE_HEADROOM of the crop height (eyes near the upper third).
        x = int(round(min(max(float(face[0]) * sw - cw / 2, 0), sw - cw)))
        y = int(round(min(max(float(face[1]) * sh - ch * FACE_HEADROOM, 0), sh - ch)))
    else:
        x = int(round((sw - cw) * ax))
        y = int(round((sh - ch) * ay))
    if (cw, ch, x, y) == (sw, sh, 0, 0):
        return finish
    return f"crop={cw}:{ch}:{x}:{y}," + finish


def parse_fps(value: str) -> str:
    """Validate and canonicalize an ffmpeg frame rate."""
    text = value.strip()
    if len(text) > 32 or not re.fullmatch(
        r"(?:[0-9]+(?:\.[0-9]+)?|[0-9]+/[0-9]+)", text
    ):
        raise argparse.ArgumentTypeError(
            "FPS must be a positive number or rational, e.g. 30 or 30000/1001"
        )
    try:
        rate = Fraction(text)
    except (ValueError, ZeroDivisionError) as exc:
        raise argparse.ArgumentTypeError(
            "FPS must be a positive number or rational, e.g. 30 or 30000/1001"
        ) from exc
    if rate <= 0:
        raise argparse.ArgumentTypeError("FPS must be greater than zero")
    # FFmpeg stores video rates as AVRational (signed 32-bit components).
    # Bounding the reduced fraction keeps every accepted canonical value safe
    # for ffmpeg and makes parse_fps(parse_fps(value)) idempotent.
    max_component = 2_147_483_647
    if rate.numerator > max_component or rate.denominator > max_component:
        raise argparse.ArgumentTypeError("FPS precision or magnitude is too large")
    return f"{rate.numerator}/{rate.denominator}"


def probe_source_fps(video: Path) -> str | None:
    """Return an ffmpeg-ready source rate, preferring the average frame rate.

    ``avg_frame_rate`` represents the observed average and is the better default
    for variable-frame-rate inputs. ``r_frame_rate`` remains a fallback for
    streams where the average is unavailable. Values are normalized to an exact
    rational so rates such as ``30000/1001`` survive without rounding.
    """
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=avg_frame_rate,r_frame_rate",
             "-of", "json", str(video)],
            capture_output=True, text=True, check=True,
        )
        streams = json.loads(out.stdout).get("streams") or []
        if not streams:
            return None
        for field in ("avg_frame_rate", "r_frame_rate"):
            value = streams[0].get(field)
            if value and value != "0/0":
                try:
                    return parse_fps(value)
                except argparse.ArgumentTypeError:
                    continue
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError):
        return None
    return None


# -------- Per-segment extraction (Rule 2 + Rule 3) --------------------------


def camera_chain(canvas: tuple[int, int], size: tuple[int, int] | None, punch_in: dict | None,
                 framing: dict | None, motion: dict | None, source: Path) -> str | None:
    """Keyframed zoom / pan / face tracking for this range, or None if static."""
    if not motion or size is None or (framing or {}).get("mode", "cover") != "cover":
        return None
    from camera import camera_filter, normalize_keys
    sw, sh = size
    W, H = canvas
    target = W / H
    cw, ch = (sh * target, float(sh)) if sw / sh > target else (float(sw), sw / target)
    face = (punch_in or {}).get("face_center") or (framing or {}).get("face_center")
    if face:
        base = (float(face[0]), float(face[1]))
    else:
        ax = _unit((punch_in or {}).get("anchor_x", (framing or {}).get("anchor_x", 0.5)))
        ay = _unit((punch_in or {}).get("anchor_y", (framing or {}).get("anchor_y", 0.5)))
        # Same window position the static crop would choose for this anchor.
        base = (((sw - cw) * ax + cw / 2) / sw, ((sh - ch) * ay + ch * FACE_HEADROOM) / sh)
    keys = normalize_keys(motion["spec"], base, motion["duration"], motion.get("track"))
    if keys is None:
        return None
    fps = float(Fraction(probe_source_fps(source) or "30"))
    return camera_filter(canvas, size, keys, fps, motion.get("t_offset", 0.0), FACE_HEADROOM)


def extract_segment(
    source: Path,
    seg_start: float,
    duration: float,
    grade_filter: str,
    out_path: Path,
    preview: bool = False,
    draft: bool = False,
    rate: str | None = None,
    punch_in: dict | None = None,
    rotation: str | None = None,
    canvas: tuple[int, int] | None = None,
    framing: dict | None = None,
    frames: int | None = None,
    include_audio: bool = True,
    motion: dict | None = None,
) -> None:
    """Extract a cut range as its own MP4, conformed to the canvas and graded.

    `-ss` before `-i` for fast accurate seeking. ``canvas`` is the output
    (width, height); when omitted it follows this source's orientation.
    ``rotation`` supports source camera files whose pixels are sideways but which
    carry no usable display-rotation metadata.  Accepted values are ``cw`` and
    ``ccw``; the transform is applied before cropping and scaling.

    ``frames`` pins the exact frame count so the render's audio, which is built
    separately, stays sample-aligned with the picture. ``include_audio=False``
    writes picture only (the render pipeline's path); the legacy standalone
    path keeps a 30ms faded audio track.

    Quality ladder (segments are intermediates — keep them near-lossless):
      - final (default): libx264 medium CRF 16
      - preview:         libx264 fast CRF 20 (evaluable for QC)
      - draft:           720p libx264 ultrafast CRF 28 (cut-point check only)
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if rotation not in (None, "cw", "ccw"):
        raise ValueError("rotation must be 'cw', 'ccw', or omitted")
    size = probe_display_size(source)
    if size is not None and rotation is not None:
        size = (min(size), max(size))  # sideways pixels turn portrait
    if canvas is None:
        portrait = rotation is not None or (size is not None and size[1] > size[0])
        canvas = default_canvas(portrait, draft)

    vf_parts: list[str] = []
    if is_hdr_source(source):
        vf_parts.append(TONEMAP_CHAIN)
    if rotation == "cw":
        vf_parts.append("transpose=1")
    elif rotation == "ccw":
        vf_parts.append("transpose=2")
    camera = camera_chain(canvas, size, punch_in, framing, motion, source)
    vf_parts.append(camera or framing_filter(canvas, size, punch_in, framing))
    if grade_filter:
        vf_parts.append(grade_filter)
    vf = ",".join(vf_parts)

    if draft:
        preset, crf = "ultrafast", "28"
    elif preview:
        preset, crf = "fast", "20"
    else:
        preset, crf = "medium", "16"

    # Frame rate: use the rate the caller resolved once for the whole render
    # (every segment must share it — concat -c copy in Rule 2 requires a uniform
    # frame rate). When called standalone with no rate, preserve this source's
    # own rate; fall back to 24 only if it can't be probed.
    out_rate = rate if rate is not None else (probe_source_fps(source) or "24")

    read_duration = duration
    if frames is not None:
        # Read a little past the range and let -frames:v be authoritative; at
        # the very end of a source, tpad clones the last frame so the picture
        # still has exactly the planned frame count.
        read_duration = duration + 0.2
        vf += ",tpad=stop_mode=clone:stop_duration=0.2"

    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{seg_start:.3f}",
        "-t", f"{read_duration:.3f}",
        "-i", str(source),
        "-vf", vf,
        "-c:v", "libx264", "-preset", preset, "-crf", crf,
        "-pix_fmt", "yuv420p", "-r", out_rate,
    ]
    if frames is not None:
        cmd += ["-frames:v", str(frames)]
    if include_audio:
        fade_out_start = max(0.0, duration - 0.03)
        cmd += [
            "-af", f"afade=t=in:st=0:d=0.03,afade=t=out:st={fade_out_start:.3f}:d=0.03",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        ]
    else:
        cmd += ["-an"]
    cmd += ["-movflags", "+faststart", str(out_path)]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def lut_filter(grade: dict, edit_dir: Path) -> str:
    """`grade: {"lut": "look.cube", "intensity": 0.7, "base": "auto"}` → filter chain."""
    lut = grade.get("lut")
    if not lut:
        return ""
    path = str(resolve_path(lut, edit_dir)).replace("\\", "/").replace("'", r"\'").replace(":", r"\:")
    intensity = max(0.0, min(1.0, float(grade.get("intensity", 1.0))))
    apply = f"lut3d=file='{path}':interp=tetrahedral"
    if intensity >= 0.999:
        return apply
    return (f"split[vu_lut_a][vu_lut_b];[vu_lut_b]{apply}[vu_lut_l];"
            f"[vu_lut_a][vu_lut_l]blend=all_expr='A*{1 - intensity:.3f}+B*{intensity:.3f}'")


def camera_match_filters(edl: dict, edit_dir: Path, reference: str, only: list | None = None) -> dict[str, str]:
    """Match every other camera's levels and white balance to ``reference``."""
    from grade import match_filter, rgb_percentiles
    ranges, sources = edl["ranges"], edl["sources"]
    spans = lambda name: [(float(r["start"]), float(r["end"]) - float(r["start"])) for r in ranges if r["source"] == name]
    if reference not in sources:
        raise ValueError(f"grade.match references unknown source {reference!r}")
    ref_spans = spans(reference) or [(0.0, min(10.0, probe_media_duration(resolve_path(sources[reference], edit_dir)) or 10.0))]
    ref_stats = rgb_percentiles(resolve_path(sources[reference], edit_dir), ref_spans)
    filters = {}
    for name in dict.fromkeys(r["source"] for r in ranges):
        if name == reference or (only is not None and name not in only):
            continue
        filters[name] = match_filter(rgb_percentiles(resolve_path(sources[name], edit_dir), spans(name)), ref_stats)
        print(f"  colour match {name} → {reference}: {filters[name]}")
    return filters


def range_looks(edl: dict, edit_dir: Path) -> list[tuple[dict | None, dict | None]]:
    """Per-range (punch_in, framing) after auto punch-ins and face anchors."""
    from face_anchor import needs_faces, plan_auto_punch, range_face_centers
    ranges = edl["ranges"]
    source_framing = edl.get("source_framing") or {}
    punches = plan_auto_punch(ranges, edl.get("auto_punch"))
    centers: list = [None] * len(ranges)
    if needs_faces(edl):
        try:
            centers = range_face_centers(edl, edit_dir, resolve_path)
        except (RuntimeError, OSError, subprocess.CalledProcessError, ValueError) as exc:
            print(f"  face anchoring unavailable ({exc}); using configured anchors")
    looks = []
    rotations = edl.get("source_rotations") or {}
    for i, r in enumerate(ranges):
        framing = dict(source_framing.get(r["source"]) or {})
        punch = dict(punches[i]) if punches[i] else None
        center = centers[i]
        if center and framing.get("anchor") == "face":
            framing["face_center"] = center
        if punch and center and punch.get("anchor", "face" if punch.get("auto") else None) == "face":
            punch["face_center"] = center
        motion = None
        if r.get("zoom") or r.get("camera") or r.get("track"):
            spec = {k: r[k] for k in ("zoom", "camera") if r.get(k)}
            if punch:
                spec["punch_in"] = punch
            start, end = float(r["start"]), float(r["end"])
            motion = {"spec": spec, "duration": end - start}
            if r.get("track") == "face":
                try:
                    from camera import smooth_track
                    from face_anchor import track_samples
                    src = resolve_path(edl["sources"][r["source"]], edit_dir)
                    samples = track_samples(src, start, end, edit_dir / "faces" / f"{r['source']}.json",
                                            rotations.get(r["source"]))
                    motion["track"] = smooth_track(samples)
                except (RuntimeError, OSError, subprocess.CalledProcessError, ValueError) as exc:
                    print(f"  face tracking unavailable ({exc}); static framing kept")
        looks.append((punch, framing or None, motion))
    return looks


def extract_all_segments(
    edl: dict,
    edit_dir: Path,
    preview: bool,
    draft: bool = False,
    fps: str | None = None,
    plans_out: list | None = None,
) -> list[Path]:
    """Extract every EDL range into edit_dir/clips_graded/seg_NN.mp4.
    Returns the ordered list of segment paths.

    If the EDL `grade` is "auto", analyze each segment range with
    `auto_grade_for_clip` and apply a per-segment subtle correction.
    Otherwise, apply the same preset/raw filter to every segment.
    """
    grade_field = edl.get("grade")
    lut_chain = ""
    match_reference = None
    match_sources = None
    skip_grade: set[str] = set()
    if isinstance(grade_field, dict):
        lut_chain = lut_filter(grade_field, edit_dir)
        match_reference = grade_field.get("match")
        match_sources = grade_field.get("match_sources")
        # Finished client assets (an outro, a supplied B-roll) keep their look.
        skip_grade = set(grade_field.get("skip") or [])
        grade_field = grade_field.get("base")
    resolved = resolve_grade_filter(grade_field)
    is_auto = resolved == "__AUTO__"
    clips_dir = edit_dir / (
        "clips_draft" if draft else ("clips_preview" if preview else "clips_graded")
    )
    clips_dir.mkdir(parents=True, exist_ok=True)

    ranges = edl["ranges"]
    sources = edl["sources"]
    source_rotations = edl.get("source_rotations") or {}
    source_framing = edl.get("source_framing") or {}

    out_rate = resolve_output_rate(edl, edit_dir, fps)
    canvas = resolve_canvas(edl, edit_dir, draft)
    timing = segment_timing(ranges, out_rate)

    # Auto grade is analyzed once per SOURCE across every range it contributes.
    # Per-range analysis gave two halves of one jump cut different
    # corrections, which reads as a colour flicker on the cut.
    source_grades: dict[str, str] = {}
    if is_auto:
        print("  (auto-grade per source: analyzing all ranges of each source)")
        for src_name in dict.fromkeys(r["source"] for r in ranges if r["source"] not in skip_grade):
            spans = [
                (float(r["start"]), float(r["end"]) - float(r["start"]))
                for r in ranges if r["source"] == src_name
            ]
            src_path = resolve_path(sources[src_name], edit_dir)
            source_grades[src_name], _ = auto_grade_for_ranges(src_path, spans)
            print(f"        {src_name} grade: {source_grades[src_name] or '(none)'}")

    looks = range_looks(edl, edit_dir)
    match_filters = camera_match_filters(edl, edit_dir, match_reference, match_sources) if match_reference else {}
    if match_reference and is_auto:
        # One look for every camera: matched sources take the reference's correction.
        for name in list(source_grades):
            if name != match_reference:
                source_grades[name] = source_grades.get(match_reference, "")

    seg_paths: list[Path] = []
    print(f"extracting {len(ranges)} segment(s) → {clips_dir.name}/  @ {out_rate} fps"
          f"{' (forced)' if fps is not None else ' (from source)'}  canvas {canvas[0]}x{canvas[1]}")
    for i, r in enumerate(ranges):
        src_name = r["source"]
        src_path = resolve_path(sources[src_name], edit_dir)
        start = float(r["start"])
        frames, _offset, duration = timing[i]
        out_path = clips_dir / f"seg_{i:02d}_{src_name}.mp4"
        seg_filter = source_grades.get(src_name, "") if is_auto else resolved
        seg_filter = ",".join(f for f in (match_filters.get(src_name, ""), seg_filter, lut_chain) if f)
        if src_name in skip_grade:
            seg_filter = ""
        punch, framing, motion = looks[i]

        note = r.get("beat") or r.get("note") or ""
        if punch and not r.get("punch_in"):
            note = (note + "  [auto punch]").strip()
        print(f"  [{i:02d}] {src_name}  {start:7.2f}-{start + duration:7.2f}  ({duration:5.2f}s, {frames}f)  {note}")
        extract_segment(
            src_path, start, duration, seg_filter, out_path,
            preview=preview, draft=draft, rate=out_rate, punch_in=punch,
            rotation=source_rotations.get(src_name),
            canvas=canvas, framing=framing,
            frames=frames, include_audio=False, motion=motion,
        )
        seg_paths.append(out_path)
        if plans_out is not None:
            plans_out.append({"source": src_path, "filter": seg_filter, "punch_in": punch, "framing": framing,
                              "rotation": source_rotations.get(src_name), "canvas": canvas, "motion": motion,
                              "start": start})

    return seg_paths


XFADE_TRANSITIONS = {
    "dissolve": "fade", "dip_black": "fadeblack", "dip_white": "fadewhite",
    "whip": "smoothleft", "slide": "slideleft", "zoom": "zoomin", "wipe": "wipeleft",
}


LIGHT_COLOR = "#FFB46E"  # warm leak; a brand kit sets its own under transitions.light.color


def is_broll(r: dict) -> bool:
    """A cutaway: picture from one source over another source's sound."""
    if "broll" in r:
        return bool(r["broll"])
    audio = r.get("audio_source")
    return isinstance(audio, dict) and audio.get("source") not in (None, r.get("source"))


def _brand_kit(edl: dict, edit_dir: Path) -> dict:
    try:
        from graphics import load_brand_kit
        return load_brand_kit(edl, edit_dir) or {}
    except Exception:
        return {}


def outro_index(edl: dict, edit_dir: Path | None = None) -> int | None:
    """The range that is the outro: ``"outro": true``, or a last range with no
    speech (no transcript) that the edit transitions into."""
    ranges = edl.get("ranges") or []
    for i, r in enumerate(ranges):
        if r.get("outro"):
            return i
    if len(ranges) < 2 or ranges[-1].get("outro") is False:
        return None
    last = ranges[-1]
    if not last.get("transition"):
        return None
    if edit_dir is not None:
        try:
            from transcripts import transcript_path
            if transcript_path(edit_dir, edl, last["source"]).exists():
                return None
        except Exception:
            return None
    return len(ranges) - 1


def resolve_transitions(edl: dict, brand: dict | None = None) -> list[dict | None]:
    """The transition leading into each range (None = hard cut).

    A range's own ``transition`` wins (``false``/``"cut"`` forces a hard cut).
    Otherwise ``edl["transitions"]["broll"]`` — or the brand kit's
    ``transitions.broll`` — is applied wherever the picture switches between a
    B-roll cutaway and the main footage, so a house style ("always a light
    flash in and out of B-roll") is one setting, not a note on every range.
    """
    ranges = edl["ranges"]
    rules = dict((brand or {}).get("transitions") or {})
    rules.update(edl.get("transitions") or {})
    broll_rule = rules.get("broll")
    if isinstance(broll_rule, str):
        broll_rule = {"type": broll_rule}
    out: list[dict | None] = [None]
    outro = next((i for i, r in enumerate(ranges) if r.get("outro")), None)
    for i in range(1, len(ranges)):
        own = ranges[i].get("transition")
        if i == outro or (i == len(ranges) - 1 and isinstance(own, dict) and own.get("outro")):
            # An outro always dissolves in over the picture, never through black.
            spec = dict(own) if isinstance(own, dict) else {"type": own} if isinstance(own, str) else {}
            if spec.get("type") in (None, "fade", "dip_black", "dip_white", "cut"):
                spec["type"] = "dissolve"
            spec.setdefault("duration_ms", spec.get("in_ms") or 660)
            out.append(spec)
            continue
        if own is False or own == "cut":
            out.append(None)
        elif own:
            out.append({"type": own} if isinstance(own, str) else dict(own))
        elif broll_rule and is_broll(ranges[i - 1]) != is_broll(ranges[i]):
            out.append(dict(broll_rule))
        else:
            out.append(None)
    return out


def light_curve(pre: int, post: int) -> list[float]:
    """Flash strength per frame: eases up over ``pre`` frames, peaks on the
    first frame of the incoming shot, then a soft after-glow that fades out.
    Shape measured from commercial light-leak flashes (30 fps: 6 + 1 + 7)."""
    ramp = [0.65 * ((k + 1) / pre) ** 1.6 for k in range(pre)]
    glow = []
    for j in range(post):
        u = (j + 1) / (post + 1)
        glow.append(0.14 * (1 - u) ** 3 + 0.28 * np.sin(np.pi * u) ** 2)
    return ramp + [1.0] + glow


def light_clip(base: Path, out: Path, cut_s: float, rate: str, duration_ms: float, color: str,
               before_s: float, after_s: float) -> tuple[Path, float, float] | None:
    """Screen a coloured light flash over the frames around one cut.

    Works on the assembled picture itself — a hard cut with a light wash, no
    handle media needed — and is overlaid back like every other transition.
    """
    fps = float(Fraction(rate))
    total = max(5, int(round(duration_ms / 1000 * fps)))
    pre = min(int(round(total * 0.43)), int(before_s * fps) - 1)
    post = min(total - 1 - int(round(total * 0.43)), int(after_s * fps) - 1)
    if pre < 2 or post < 2:
        return None
    w, h = probe_video_size(base)
    start = (int(round(cut_s * fps)) - pre) / fps
    n = pre + 1 + post
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{start:.6f}", "-i", str(base), "-frames:v", str(n),
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    frames = np.frombuffer(raw, np.uint8).reshape(-1, h, w, 3).astype(np.float32)
    if len(frames) < n:
        return None
    rgb = np.array([int(color.lstrip("#")[k:k + 2], 16) / 255 for k in (0, 2, 4)], np.float32)
    # Vertical falloff: the leak is strongest toward the bottom, like the measured flashes.
    falloff = (0.75 + 0.25 * np.linspace(0, 1, h, dtype=np.float32))[:, None, None]
    curve = light_curve(pre, post)
    for k, s in enumerate(curve):
        tint = rgb + (1 - rgb) * 0.35 if k == pre else rgb  # the peak frame burns toward white
        strength = np.clip(s * tint * falloff, 0, 1)
        frames[k] = 255 - (255 - frames[k]) * (1 - strength)
    proc = subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
                           "-r", rate, "-i", "-", "-frames:v", str(n), "-c:v", "libx264", "-preset", "fast",
                           "-crf", "16", "-pix_fmt", "yuv420p", str(out)],
                          input=np.clip(frames, 0, 255).astype(np.uint8).tobytes(), capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode(errors="replace")[-400:])
    return out, start, n / fps


def _read_frames(base: Path, start: float, n: int) -> tuple[np.ndarray, int, int]:
    w, h = probe_video_size(base)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{start:.6f}", "-i", str(base), "-frames:v", str(n),
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, h, w, 3).astype(np.float32), w, h


def _write_frames(frames: np.ndarray, out: Path, rate: str) -> None:
    n, h, w, _ = frames.shape
    proc = subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
                           "-r", rate, "-i", "-", "-frames:v", str(n), "-c:v", "libx264", "-preset", "fast",
                           "-crf", "16", "-pix_fmt", "yuv420p", str(out)],
                          input=np.clip(frames, 0, 255).astype(np.uint8).tobytes(), capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode(errors="replace")[-400:])


def fade_clip(base: Path, out: Path, cut_s: float, rate: str, out_ms: float, in_ms: float, color: str,
              before_s: float, after_s: float) -> tuple[Path, float, float] | None:
    """Fade the incoming shot up from a colour (black by default), optionally
    fading the outgoing shot down first. Picture only — the sound is untouched.
    Built from the assembled frames, so it needs no handle media."""
    fps = float(Fraction(rate))
    pre = min(int(round(out_ms / 1000 * fps)), int(before_s * fps) - 1)
    post = min(int(round(in_ms / 1000 * fps)), int(after_s * fps) - 1)
    if post < 2 and pre < 2:
        return None
    pre, post = max(pre, 0), max(post, 0)
    start = (int(round(cut_s * fps)) - pre) / fps
    frames, _, _ = _read_frames(base, start, pre + post)
    if len(frames) < pre + post:
        return None
    rgb = np.array([int(color.lstrip("#")[k:k + 2], 16) for k in (0, 2, 4)], np.float32)
    smooth = lambda u: u * u * (3 - 2 * u)
    for k in range(pre):  # outgoing: towards the colour
        a = smooth((k + 1) / (pre + 1))
        frames[k] = frames[k] * (1 - a) + rgb * a
    for k in range(post):  # incoming: up from the colour
        a = smooth(1 - k / post)
        frames[pre + k] = frames[pre + k] * (1 - a) + rgb * a
    _write_frames(frames, out, rate)
    return out, start, (pre + post) / fps


def probe_video_size(path: Path) -> tuple[int, int]:
    text = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                           "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    stream = json.loads(text)["streams"][0]
    return int(stream["width"]), int(stream["height"])


TRANSITION_CLIPS: list[dict] = []  # what the last render laid over its cuts (for NLE export)


def apply_transitions(edl: dict, edit_dir: Path, base_path: Path, plans: list[dict], rate: str,
                      preview: bool, draft: bool) -> Path:
    """Lay centred transition clips over the cut points of the assembled base.

    Each clip is built from handles on both sides of the boundary (outgoing
    shot running past its out point, incoming shot from before its in point)
    and crossed with ``xfade``. It replaces exactly the frames around the cut,
    so the timeline length — and the separately built audio — never moves.
    """
    ranges = edl["ranges"]
    brand = _brand_kit(edl, edit_dir)
    planned = resolve_transitions(edl, brand)
    items = [(i, spec) for i, spec in enumerate(planned) if spec]
    if not items:
        return base_path
    fps = float(Fraction(rate))
    timing = segment_timing(ranges, rate)
    tdir = edit_dir / "transitions"
    tdir.mkdir(parents=True, exist_ok=True)
    clips: list[tuple[Path, float, float]] = []
    print(f"transitions: {len(items)}")
    oi = outro_index(edl, edit_dir)
    for i, spec in items:
        kind = spec.get("type", "dissolve")
        if i == oi and kind in ("fade", "dip_black", "dip_white"):
            kind = "dissolve"  # the outro dissolves in over the picture, never through black
        if kind == "fade":
            made = fade_clip(base_path, tdir / f"t{i:02d}_fade.mp4", timing[i][1], rate,
                             float(spec.get("out_ms", 0)), float(spec.get("in_ms", spec.get("duration_ms", 500))),
                             spec.get("color", "#000000"), timing[i - 1][2], timing[i][2])
            if made:
                clips.append(made)
                print(f"  [{i:02d}] fade {made[2] * 1000:.0f}ms @ {timing[i][1]:.2f}s")
            continue
        if kind == "light":
            color = spec.get("color") or ((brand.get("transitions") or {}).get("light") or {}).get("color") or LIGHT_COLOR
            made = light_clip(base_path, tdir / f"t{i:02d}_light.mp4", timing[i][1], rate,
                              float(spec.get("duration_ms", 470)), color, timing[i - 1][2], timing[i][2])
            if made:
                clips.append(made)
                print(f"  [{i:02d}] light {made[2] * 1000:.0f}ms @ {timing[i][1]:.2f}s  ({color})")
            continue
        a, b = ranges[i - 1], ranges[i]
        a_plan, b_plan = plans[i - 1], plans[i]
        a_end, b_start = float(a["end"]), float(b["start"])
        want = float(spec.get("duration_ms", 400)) / 2000.0
        a_dur = probe_media_duration(a_plan["source"])
        # Window [cut - lead, cut + tail]: centred when both shots have handle
        # media; otherwise it leans to the side that has it (a last shot with
        # no frames after its out point dissolves *before* the cut instead of
        # falling back to a hard cut).
        a_tail = max(0.0, (a_dur - a_end) if a_dur is not None else want)
        tail = min(want, a_tail, (float(b["end"]) - float(b["start"])) / 2)
        lead_room = min(b_start, (float(a["end"]) - float(a["start"])) / 2)
        lead = min(lead_room, 2 * want - tail)
        tail = min(tail, 2 * want - lead) if lead >= want else tail
        lf, tf = int(lead * fps), int(tail * fps)
        if lf + tf < 4:
            print(f"  [{i:02d}] {kind}: not enough handle media; hard cut kept")
            continue
        n, hd = lf + tf, lf / fps
        parts = []
        for side, (rng_start, plan) in (("a", (a_end - hd, a_plan)), ("b", (b_start - hd, b_plan))):
            part = tdir / f"t{i:02d}_{side}.mp4"
            motion = dict(plan["motion"], t_offset=rng_start - plan["start"]) if plan.get("motion") else None
            extract_segment(plan["source"], rng_start, n / fps, plan["filter"], part, preview=preview, draft=draft,
                            rate=rate, punch_in=plan["punch_in"], rotation=plan["rotation"], canvas=plan["canvas"],
                            framing=plan["framing"], frames=n, include_audio=False, motion=motion)
            parts.append(part)
        clip = tdir / f"t{i:02d}_{kind}.mp4"
        subprocess.run([
            "ffmpeg", "-y", "-v", "error", "-i", str(parts[0]), "-i", str(parts[1]), "-filter_complex",
            f"[0:v][1:v]xfade=transition={XFADE_TRANSITIONS[kind]}:duration={(n - 1) / fps:.6f}:offset=0,format=yuv420p",
            "-frames:v", str(n), "-r", rate, "-c:v", "libx264", "-preset", "fast", "-crf", "16", str(clip),
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        start = timing[i][1] - hd
        clips.append((clip, start, n / fps))
        print(f"  [{i:02d}] {kind} {n / fps * 1000:.0f}ms @ {timing[i][1]:.2f}s")
    TRANSITION_CLIPS[:] = [{"file": str(c), "start_in_output": st, "duration": d} for c, st, d in clips]
    if not clips:
        return base_path
    out = base_path.with_name(base_path.stem + "_fx.mp4")
    # Splice by frame number: stream the base's raw frames and swap in each
    # clip's frames. (overlay's frame sync silently dropped late clips, and a
    # trim+concat graph reading one input many times deadlocks ffmpeg.)
    w, h = probe_video_size(base_path)
    size = w * h * 3 // 2
    swap: dict[int, bytes] = {}
    for clip, start, dur in clips:
        first = int(round(start * fps))
        raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(clip), "-f", "rawvideo", "-pix_fmt", "yuv420p",
                              "-s", f"{w}x{h}", "-"], capture_output=True, check=True).stdout
        for k in range(min(int(round(dur * fps)), len(raw) // size)):
            swap[first + k] = raw[k * size:(k + 1) * size]
    preset, crf = ("ultrafast", "28") if draft else (("fast", "20") if preview else ("medium", "16"))
    reader = subprocess.Popen(["ffmpeg", "-v", "error", "-i", str(base_path), "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"],
                              stdout=subprocess.PIPE)
    writer = subprocess.Popen(["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{w}x{h}",
                               "-r", rate, "-i", "-", "-c:v", "libx264", "-preset", preset, "-crf", crf,
                               "-pix_fmt", "yuv420p", "-an", str(out)], stdin=subprocess.PIPE)
    index = 0
    while True:
        frame = reader.stdout.read(size)
        if len(frame) < size:
            break
        writer.stdin.write(swap.get(index, frame))
        index += 1
    writer.stdin.close()
    if reader.wait() != 0 or writer.wait() != 0:
        raise RuntimeError("transition splice failed")
    return out


def resolve_output_rate(edl: dict, edit_dir: Path, fps: str | None = None) -> str:
    """Resolve ONE output frame rate for the entire render.

    The lossless concat (Rule 2, `-c copy`) requires all segments to share a
    frame rate; probing per-segment would diverge for multi-source EDLs that
    mix rates. Explicit --fps wins; otherwise preserve the first source's rate.
    """
    if fps is not None:
        return parse_fps(str(fps))
    ranges = edl.get("ranges") or []
    if ranges:
        first_src = resolve_path(edl["sources"][ranges[0]["source"]], edit_dir)
        return probe_source_fps(first_src) or "24"
    return "24"


def segment_timing(ranges: list[dict], rate: str) -> list[tuple[int, float, float]]:
    """Quantize every range to whole frames on the OUTPUT timeline.

    Returns (frames, output_offset_s, duration_s) per range. Rounding the
    cumulative boundaries (not each duration) keeps the total within one frame
    of the EDL, and the separately built audio lands sample-exact on the
    picture cuts.
    """
    fps = Fraction(rate)
    out: list[tuple[int, float, float]] = []
    cursor = 0.0
    prev_frame = 0
    for r in ranges:
        cursor += float(r["end"]) - float(r["start"])
        end_frame = max(prev_frame + 1, int(round(cursor * fps)))
        frames = end_frame - prev_frame
        out.append((frames, float(prev_frame / fps), float(frames / fps)))
        prev_frame = end_frame
    return out


# -------- Audio track (equal-power micro-crossfades, PCM until the end) ------
#
# The old path faded every segment's audio to silence for 30ms and encoded
# AAC per segment before concat. Two problems: (1) a 60ms dip to digital
# silence at every cut makes room tone and music beds audibly "gate", and
# (2) per-segment AAC priming/padding adds tiny gaps at each boundary.
#
# Now each range is pulled as PCM with a short handle on both sides, the
# handles overlap across the cut with equal-power (qsin) fades — the incoming
# and outgoing audio crossfade, centered on the picture cut — and the whole
# timeline is mixed once and encoded to AAC exactly once in the final pass.

DEFAULT_AUDIO_CROSSFADE_MS = 30.0
AUDIO_RATE = 48000


def build_audio_track(
    edl: dict,
    edit_dir: Path,
    rate: str,
    out_wav: Path,
    crossfade_ms: float | None = None,
) -> Path:
    ranges = edl["ranges"]
    sources = edl["sources"]
    if crossfade_ms is None:
        crossfade_ms = float((edl.get("audio") or {}).get("crossfade_ms", DEFAULT_AUDIO_CROSSFADE_MS))
    half = max(0.002, crossfade_ms / 2000.0)
    timing = segment_timing(ranges, rate)
    total = timing[-1][1] + timing[-1][2] if timing else 0.0

    parts_dir = out_wav.parent / f"{out_wav.stem}_parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    durations: dict[str, float | None] = {}
    placements: list[tuple[Path, float]] = []
    shifts = boundary_audio_shifts(ranges)
    runs = audio_runs(ranges)
    for k, (first, last) in enumerate(runs):
        src_name, run_start = audio_ref(ranges[first])
        src = resolve_path(sources[src_name], edit_dir)
        if src_name not in durations:
            durations[src_name] = probe_media_duration(src)
        src_dur = durations[src_name]
        offset = timing[first][1]
        duration = sum(timing[j][2] for j in range(first, last + 1))
        # J/L cuts move the run's outer audio boundaries (never the picture).
        shift_in, shift_out = shifts[first], shifts[last + 1]
        shift_in = max(shift_in, -run_start)
        start = run_start + shift_in
        offset = offset + shift_in
        duration = duration - shift_in + shift_out
        if src_dur is not None:
            duration = min(duration, src_dur - start)
        end = start + duration

        # Handles only where a neighbour exists to crossfade with.
        has_prev, has_next = k > 0, k < len(runs) - 1
        lead = min(half, start) if has_prev else 0.0
        tail = half if has_next else 0.0
        if src_dur is not None:
            tail = max(0.0, min(tail, src_dur - end))
        fade_in = lead + half if has_prev else 0.005
        fade_out = tail + half if has_next else 0.005
        length = lead + duration + tail
        fade_in = min(fade_in, length / 2)
        fade_out = min(fade_out, length / 2)

        part = parts_dir / f"a_{k:03d}.wav"
        cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-ss", f"{start - lead:.6f}", "-t", f"{length:.6f}",
            "-i", str(src),
            "-map", "0:a:0", "-vn",
            "-af", (
                f"aresample={AUDIO_RATE},apad,atrim=0:{length:.6f},"
                f"volume={float(ranges[first].get('audio_gain_db', 0)):.2f}dB,"
                f"afade=t=in:st=0:d={fade_in:.6f}:curve=qsin,"
                f"afade=t=out:st={length - fade_out:.6f}:d={fade_out:.6f}:curve=qsin"
            ),
            "-ac", "2", "-ar", str(AUDIO_RATE), "-c:a", "pcm_s24le",
            str(part),
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        placements.append((part, max(0.0, offset - lead)))

    # Mix in batches so huge EDLs never hit filter-graph input limits.
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    _mix_placements(placements, total, out_wav)
    print(f"audio → {out_wav.name}  ({len(placements)} part(s) from {len(ranges)} range(s), {crossfade_ms:.0f}ms equal-power crossfades)")
    return out_wav


def audio_ref(r: dict) -> tuple[str, float]:
    """(audio source, audio start) of a range: its own, or ``audio_source``
    (multicam: the picture comes from an angle, the sound from the master)."""
    ref = r.get("audio_source")
    if isinstance(ref, dict) and ref.get("source"):
        return str(ref["source"]), float(ref.get("start", r["start"]))
    return str(r["source"]), float(r["start"])


def audio_runs(ranges: list[dict], tolerance: float = 0.002) -> list[tuple[int, int]]:
    """Group ranges whose sound is continuous in one source into one audio part.

    Crossfading a signal with its own continuation adds ~3 dB at the seam, so a
    picture-only cut (multicam switch, punch-in on unbroken speech) keeps the
    sound untouched.
    """
    runs: list[tuple[int, int]] = []
    for i, r in enumerate(ranges):
        if runs and i > 0 and not r.get("j_cut_ms") and not r.get("l_cut_ms"):
            prev_src, prev_start = audio_ref(ranges[i - 1])
            prev_end = prev_start + float(ranges[i - 1]["end"]) - float(ranges[i - 1]["start"])
            src, start = audio_ref(r)
            if src == prev_src and abs(start - prev_end) <= tolerance:
                runs[-1] = (runs[-1][0], i)
                continue
        runs.append((i, i))
    return runs


def boundary_audio_shifts(ranges: list[dict]) -> list[float]:
    """Audio offset (s) at each boundary: shifts[i] is the boundary *into* range i.

    ``j_cut_ms`` on range i starts its sound early under the previous picture
    (negative shift); ``l_cut_ms`` lets the previous sound run on under range
    i's picture (positive shift). The first and last edges never move.
    """
    shifts = [0.0] * (len(ranges) + 1)
    for i, r in enumerate(ranges):
        if i == 0:
            continue
        j = float(r.get("j_cut_ms") or 0) / 1000.0
        l = float(r.get("l_cut_ms") or 0) / 1000.0
        prev_len = float(ranges[i - 1]["end"]) - float(ranges[i - 1]["start"])
        this_len = float(r["end"]) - float(r["start"])
        shifts[i] = max(-0.8 * prev_len, min(0.8 * this_len, l - j))
    return shifts


def _mix_placements(placements: list[tuple[Path, float]], total: float, out_wav: Path, batch: int = 32) -> None:
    if len(placements) > batch:
        partials: list[tuple[Path, float]] = []
        for b in range(0, len(placements), batch):
            partial = out_wav.with_name(f"{out_wav.stem}_mix{b // batch:02d}.wav")
            _mix_placements(placements[b:b + batch], total, partial, batch)
            partials.append((partial, 0.0))
        _mix_placements(partials, total, out_wav, batch)
        for partial, _ in partials:
            partial.unlink(missing_ok=True)
        return
    inputs: list[str] = []
    graph: list[str] = []
    for idx, (path, at) in enumerate(placements):
        inputs += ["-i", str(path)]
        delay = int(round(at * AUDIO_RATE))
        graph.append(f"[{idx}:a]adelay=delays={delay}S:all=1[d{idx}]")
    labels = "".join(f"[d{idx}]" for idx in range(len(placements)))
    graph.append(
        f"{labels}amix=inputs={len(placements)}:normalize=0:duration=longest:dropout_transition=0,"
        f"apad=whole_dur={total:.6f},atrim=0:{total:.6f}[mix]"
    )
    cmd = [
        "ffmpeg", "-y", "-v", "error", *inputs,
        "-filter_complex", ";".join(graph), "-map", "[mix]",
        "-ac", "2", "-ar", str(AUDIO_RATE), "-c:a", "pcm_s24le", str(out_wav),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def probe_media_duration(path: Path) -> float | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, check=True,
        )
        return float(out.stdout.strip())
    except (subprocess.CalledProcessError, ValueError, OSError):
        return None


# -------- Lossless concat ----------------------------------------------------


def concat_segments(segment_paths: list[Path], out_path: Path, edit_dir: Path) -> None:
    """Lossless concat via the concat demuxer. No re-encode."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    concat_list = edit_dir / "_concat.txt"
    concat_list.write_text("".join(f"file '{p.resolve()}'\n" for p in segment_paths))

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_list),
        "-c", "copy",
        "-movflags", "+faststart",
        str(out_path),
    ]
    print(f"concat → {out_path.name}")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    concat_list.unlink(missing_ok=True)


# -------- Master SRT (Rule 5) ------------------------------------------------


PUNCT_BREAK = set(".,!?;:")


def _srt_timestamp(seconds: float) -> str:
    total_ms = int(round(seconds * 1000))
    h, rem = divmod(total_ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _words_in_range(transcript: dict, t_start: float, t_end: float) -> list[dict]:
    out: list[dict] = []
    for w in transcript.get("words", []):
        if w.get("type") != "word":
            continue
        ws = w.get("start")
        we = w.get("end")
        if ws is None or we is None:
            continue
        if we <= t_start or ws >= t_end:
            continue
        out.append(w)
    return out


def smooth_cue_timing(
    entries: list[tuple[float, float, str]],
    bridge_gap: float = 0.25,
    min_duration: float = 0.45,
) -> list[tuple[float, float, str]]:
    """Remove caption flicker: bridge tiny gaps and give short cues time to read.

    Word timestamps end a cue the instant the last word ends, so consecutive
    cues blink off for 50–200ms. Bridge gaps under ``bridge_gap`` and extend
    cues shorter than ``min_duration`` without ever overlapping the next cue.
    """
    ordered = sorted(entries, key=lambda e: e[0])
    out: list[tuple[float, float, str]] = []
    for i, (start, end, text) in enumerate(ordered):
        next_start = ordered[i + 1][0] if i + 1 < len(ordered) else None
        if end - start < min_duration:
            end = start + min_duration
        if next_start is not None:
            if next_start - end < bridge_gap:
                end = next_start
            end = min(end, next_start)
        out.append((start, max(end, start + 0.04), text))
    return out


CHUNK_WORDS = 2         # target words per cue
CHUNK_MAX_WORDS = 3     # a too-short chunk may grow to this many words
CHUNK_MIN_S = 0.35      # a cue shorter than this reads as a flash
CHUNK_PAUSE_S = 0.3     # a gap this long between words ends the cue


def chunk_words(words: list[dict]) -> list[list[dict]]:
    """Group transcript words into caption cues.

    A cue closes on trailing punctuation or on a pause before the next word.
    Otherwise it closes at CHUNK_WORDS words, unless it would be on screen for
    less than CHUNK_MIN_S; then it takes up to CHUNK_MAX_WORDS words. So
    "does | is" across a pause stays split, and fast "what a" does not flash.
    """
    words = [w for w in words if (w.get("text") or "").strip()]
    chunks: list[list[dict]] = []
    current: list[dict] = []
    for i, w in enumerate(words):
        current.append(w)
        text = w["text"].strip()
        nxt = words[i + 1] if i + 1 < len(words) else None
        gap = (nxt["start"] - w["end"]) if nxt else 0.0
        dur = w["end"] - current[0]["start"]
        if (
            nxt is None
            or text[-1] in PUNCT_BREAK
            or gap >= CHUNK_PAUSE_S
            or len(current) >= CHUNK_MAX_WORDS
            or (len(current) >= CHUNK_WORDS and dur >= CHUNK_MIN_S)
        ):
            chunks.append(current)
            current = []
    return chunks


def build_master_srt(edl: dict, edit_dir: Path, out_path: Path) -> None:
    """Build an output-timeline SRT from per-source transcripts.

    - phrase-aware ~2-word chunks (see chunk_words)
    - UPPERCASE text
    - Output times computed as word.start - segment_start + segment_offset
    """
    transcripts_dir = edit_dir / "transcripts"
    sources = edl["sources"]

    entries: list[tuple[float, float, str]] = []
    seg_offset = 0.0

    for r in edl["ranges"]:
        src_name = r["source"]
        seg_start = float(r["start"])
        seg_end = float(r["end"])
        seg_duration = seg_end - seg_start

        from transcripts import transcript_path
        tr_path = transcript_path(edit_dir, edl, src_name)
        if not tr_path.exists():
            print(f"  no transcript for {src_name}, skipping captions for this segment")
            seg_offset += seg_duration
            continue

        transcript = json.loads(tr_path.read_text())
        words_in_seg = _words_in_range(transcript, seg_start, seg_end)

        for chunk in chunk_words(words_in_seg):
            local_start = max(seg_start, chunk[0].get("start", seg_start))
            local_end = min(seg_end, chunk[-1].get("end", seg_end))
            out_start = max(0.0, local_start - seg_start) + seg_offset
            out_end = max(0.0, local_end - seg_start) + seg_offset
            if out_end <= out_start:
                out_end = out_start + 0.4
            text = " ".join((w.get("text") or "").strip() for w in chunk)
            text = re.sub(r"\s+", " ", text).strip()
            # Strip trailing punctuation for cleaner uppercase look
            text = text.rstrip(",;:")
            text = text.upper()
            entries.append((out_start, out_end, text))

        seg_offset += seg_duration

    # Sort and write as SRT
    entries = smooth_cue_timing(entries)
    lines: list[str] = []
    for i, (a, b, t) in enumerate(entries, start=1):
        lines.append(str(i))
        lines.append(f"{_srt_timestamp(a)} --> {_srt_timestamp(b)}")
        lines.append(t)
        lines.append("")
    out_path.write_text("\n".join(lines))
    print(f"master SRT → {out_path.name} ({len(entries)} cues)")


# -------- Loudness normalization (social-ready audio) -----------------------


# Social-media standard: -14 LUFS integrated, -1 dBTP peak, LRA 11 LU.
# Matches YouTube / Instagram / TikTok / X / LinkedIn normalization targets.
LOUDNORM_I = -14.0
LOUDNORM_TP = -1.5  # AAC adds ~0.5 dB of inter-sample overs; land under -1 after the encode
LOUDNORM_LRA = 11.0


def measure_loudness(video_path: Path) -> dict[str, str] | None:
    """Run ffmpeg loudnorm first pass and parse the JSON measurement.

    Returns a dict with measured_i, measured_tp, measured_lra, measured_thresh,
    target_offset, or None if measurement failed.
    """
    filter_str = (
        f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}:print_format=json"
    )
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-nostats",
        "-i", str(video_path),
        "-af", filter_str,
        "-vn", "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    # loudnorm prints the JSON to stderr at the end of the run
    stderr = proc.stderr

    # Find the JSON block — loudnorm output contains a `{ ... }` block
    start = stderr.rfind("{")
    end = stderr.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(stderr[start : end + 1])
    except json.JSONDecodeError:
        return None
    needed = {"input_i", "input_tp", "input_lra", "input_thresh", "target_offset"}
    if not needed.issubset(data.keys()):
        return None
    return data


def loudnorm_filter(audio_path: Path, one_pass: bool = False) -> str:
    """Return the loudnorm filter to apply during the final (only) AAC encode.

    Final renders measure first (two-pass, linear gain — no pumping). Draft
    renders use the one-pass dynamic approximation. loudnorm upsamples
    internally for true-peak detection, so resample back to 48 kHz.
    """
    target = f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}"
    if not one_pass:
        print(f"  loudnorm pass 1: measuring {audio_path.name}")
        m = measure_loudness(audio_path)
        if m is None:
            print("  loudnorm measurement failed — falling back to 1-pass")
        else:
            print(f"    measured: I={m['input_i']} LUFS  TP={m['input_tp']}  LRA={m['input_lra']}")
            target += (
                f":measured_I={m['input_i']}:measured_TP={m['input_tp']}"
                f":measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}"
                f":offset={m['target_offset']}:linear=true"
            )
    return target + f",aresample={AUDIO_RATE}"


# -------- Final compositing (Rule 1 + Rule 4) -------------------------------


def write_render_manifest(edit_dir: Path, edl: dict, out_path: Path, base_path: Path, canvas: tuple[int, int],
                          rate: str, total_s: float, audio_path: Path, overlays: list[dict],
                          behind: list[dict], mask: Path | None, subs: Path | None) -> Path:
    """Everything this render composited, with output times — what an NLE
    export needs to rebuild the timeline layer by layer."""
    from graphics import output_time
    sfx = []
    for item in (edl.get("audio") or {}).get("sfx") or []:
        try:
            start = float(item["start_in_output"]) if "start_in_output" in item else output_time(edl, item["at"], rate)
        except Exception:
            continue
        sfx.append({"file": str(resolve_path(item["file"], edit_dir)) if not str(item["file"]).startswith("library:") else item["file"],
                    "start_in_output": round(start, 4), "gain_db": float(item.get("gain_db", -6)),
                    "label": item.get("label") or item.get("auto") or Path(str(item["file"])).stem})
    keep = ("file", "x", "y", "w", "h", "start_in_output", "duration", "type", "text", "word_time")
    manifest = {
        "output": str(out_path), "base": str(base_path), "canvas": list(canvas), "rate": rate, "total_s": total_s,
        "audio_mix": str(audio_path), "stems": str(edit_dir / "stems"),
        "overlays": [{k: o.get(k) for k in keep if k in o} for o in overlays],
        "behind_subject_overlays": [{k: o.get(k) for k in keep if k in o} for o in behind],
        "subject_mask": str(mask) if mask else None, "subtitles": str(subs) if subs else None,
        "transitions": list(TRANSITION_CLIPS), "sfx": sfx,
        "timing": [list(t) for t in segment_timing(edl["ranges"], rate)],
    }
    path = edit_dir / "render_manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def ensure_subject_mask(base_path: Path, edit_dir: Path, config: dict | None) -> Path | None:
    """Generate a local person matte only when behind-subject layers need it."""
    if not config or config.get("mode") != "person":
        return None
    try:
        from person_mask import build_person_mask
        quality = str(config.get("quality", "balanced"))
        configured_fps = config.get("fps")
        if configured_fps is None:
            rate = probe_source_fps(base_path) or "24"
            fps = float(Fraction(rate))
        else:
            fps = float(configured_fps)
        # Keyed by the picture itself (and the quality): a re-cut or re-framed
        # base must never reuse an older matte — it would sit on the wrong body.
        import hashlib
        stat = base_path.stat()
        key = hashlib.sha1(f"{stat.st_size}:{int(stat.st_mtime)}:{quality}:{fps}".encode()).hexdigest()[:10]
        mask_path = edit_dir / "masks" / f"{base_path.stem}_person_{key}.mkv"
        if not mask_path.exists():
            print(f"creating local person mask ({quality}) → {mask_path.name}")
            build_person_mask(base_path, mask_path, fps=fps, quality=quality)
        return mask_path
    except (ImportError, OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"Could not generate the local person mask: {exc}") from exc


def build_final_composite(
    base_path: Path,
    overlays: list[dict],
    subtitles_path: Path | None,
    out_path: Path,
    edit_dir: Path,
    behind_subject_overlays: list[dict] | None = None,
    subject_mask_path: Path | None = None,
    audio_path: Path | None = None,
    audio_filter: str | None = None,
) -> None:
    """Final pass: background layers → person → top overlays → subtitles.

    ``audio_path`` is the render's PCM master; when given, it is the output
    audio (with ``audio_filter``, e.g. loudnorm) and AAC is encoded exactly
    once here. Without it, the base file's own audio is copied (legacy path).
    """
    behind_subject_overlays = behind_subject_overlays or []
    has_overlays = bool(overlays)
    has_behind_subject = bool(behind_subject_overlays)
    has_subs = subtitles_path is not None and subtitles_path.exists()
    aac = ["-c:a", "aac", "-b:a", "256k", "-ar", str(AUDIO_RATE)]

    if not has_overlays and not has_behind_subject and not has_subs:
        if audio_path is None:
            run(["ffmpeg", "-y", "-i", str(base_path), "-c", "copy", str(out_path)], quiet=True)
            return
        cmd = ["ffmpeg", "-y", "-i", str(base_path), "-i", str(audio_path),
               "-map", "0:v", "-map", "1:a", "-c:v", "copy"]
        if audio_filter:
            cmd += ["-af", audio_filter]
        cmd += [*aac, "-movflags", "+faststart", str(out_path)]
        print(f"muxing → {out_path.name} (picture copied, audio encoded once)")
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        return

    inputs: list[str] = ["-i", str(base_path)]
    for ov in behind_subject_overlays:
        inputs += ["-i", str(resolve_path(ov["file"], edit_dir))]
    mask_input_index: int | None = None
    if has_behind_subject:
        if subject_mask_path is None or not subject_mask_path.exists():
            raise ValueError("behind_subject_overlays require a generated subject mask")
        mask_input_index = 1 + len(behind_subject_overlays)
        inputs += ["-i", str(subject_mask_path)]
    for ov in overlays:
        ov_path = resolve_path(ov["file"], edit_dir)
        inputs += ["-i", str(ov_path)]

    filter_parts: list[str] = []
    base_for_layers = "[0:v]"
    if has_behind_subject:
        filter_parts.append("[0:v]split[background_base][subject_base]")
        base_for_layers = "[background_base]"
    # PTS-shift every overlay so its frame 0 lands at start_in_output
    for idx, ov in enumerate(behind_subject_overlays, start=1):
        t = float(ov["start_in_output"])
        filter_parts.append(f"[{idx}:v]setpts=PTS-STARTPTS+{t}/TB[behind{idx}]")

    # Behind-subject graph/text layers are composed first, then the source
    # person is restored with its alpha matte.
    current = base_for_layers
    for idx, ov in enumerate(behind_subject_overlays, start=1):
        t = float(ov["start_in_output"])
        dur = float(ov["duration"])
        end = t + dur
        next_label = f"[behind_v{idx}]"
        x = ov.get("x", "0")
        y = ov.get("y", "0")
        filter_parts.append(
            f"{current}[behind{idx}]overlay=x={x}:y={y}:enable='between(t,{t:.3f},{end:.3f})'{next_label}"
        )
        current = next_label

    if has_behind_subject:
        assert mask_input_index is not None
        # Vision deliberately returns a compact matte. Scale it to the source
        # at composition time so segmentation stays fast but alpha merging has
        # exact dimensions. scale2ref also keeps portrait sources correct.
        filter_parts.append(f"[{mask_input_index}:v]format=gray[subject_mask_raw]")
        filter_parts.append("[subject_mask_raw][subject_base]scale2ref[subject_mask][subject_for_alpha]")
        filter_parts.append("[subject_for_alpha][subject_mask]alphamerge[subject_foreground]")
        filter_parts.append(f"{current}[subject_foreground]overlay=shortest=1[with_subject]")
        current = "[with_subject]"

    top_start_index = 1 + len(behind_subject_overlays) + (1 if has_behind_subject else 0)
    for relative_index, ov in enumerate(overlays):
        idx = top_start_index + relative_index
        t = float(ov["start_in_output"])
        filter_parts.append(f"[{idx}:v]setpts=PTS-STARTPTS+{t}/TB[top{relative_index}]")
    for relative_index, ov in enumerate(overlays):
        t = float(ov["start_in_output"])
        dur = float(ov["duration"])
        end = t + dur
        next_label = f"[top_v{relative_index}]"
        x = ov.get("x", "0")
        y = ov.get("y", "0")
        filter_parts.append(
            f"{current}[top{relative_index}]overlay=x={x}:y={y}:enable='between(t,{t:.3f},{end:.3f})'{next_label}"
        )
        current = next_label

    # Subtitles LAST — Rule 1
    if has_subs:
        subs_abs = str(subtitles_path.resolve()).replace(":", r"\:").replace("'", r"\'")
        # ASS carries intentional Arabic/brand typography and RTL-safe layout.
        # Keep the compact force style for plain SRT only.
        if subtitles_path.suffix.lower() == ".ass":
            filter_parts.append(f"{current}subtitles=filename='{subs_abs}'[outv]")
        else:
            filter_parts.append(
                f"{current}subtitles=filename='{subs_abs}':force_style='{SUB_FORCE_STYLE}'[outv]"
            )
        out_label = "[outv]"
    else:
        # Rename the last overlay output to [outv] for consistency
        if has_overlays or has_behind_subject:
            filter_parts.append(f"{current}null[outv]")
            out_label = "[outv]"
        else:
            out_label = "[0:v]"

    if audio_path is not None:
        audio_index = len(inputs) // 2
        inputs += ["-i", str(audio_path)]
        filter_parts.append(f"[{audio_index}:a]{audio_filter or 'anull'}[outa]")
        audio_map, audio_codec = "[outa]", aac
    else:
        audio_map, audio_codec = "0:a", ["-c:a", "copy"]

    filter_complex = ";".join(filter_parts)

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", out_label,
        "-map", audio_map,
        "-c:v", "libx264", "-preset", "medium", "-crf", "17",
        "-pix_fmt", "yuv420p",
        *audio_codec,
        "-movflags", "+faststart",
        str(out_path),
    ]
    print(f"compositing → {out_path.name}")
    print(f"  top overlays: {len(overlays)}, behind subject: {len(behind_subject_overlays)}, subtitles: {'yes' if has_subs else 'no'}")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


# -------- Main ---------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description="Render a video from an EDL")
    ap.add_argument("edl", type=Path, help="Path to edl.json")
    ap.add_argument("-o", "--output", type=Path, required=True, help="Output video path")
    ap.add_argument(
        "--preview",
        action="store_true",
        help="Preview mode: full canvas, fast, CRF 20 segments — evaluable for QC, faster than final.",
    )
    ap.add_argument(
        "--draft",
        action="store_true",
        help="Draft mode: 720p, ultrafast, CRF 28 — cut-point verification only.",
    )
    ap.add_argument(
        "--build-subtitles",
        action="store_true",
        help="Build master.srt from transcripts + EDL offsets before compositing",
    )
    ap.add_argument(
        "--no-subtitles",
        action="store_true",
        help="Skip subtitles even if the EDL references one",
    )
    ap.add_argument(
        "--no-loudnorm",
        action="store_true",
        help="Skip audio loudness normalization. Default is on (-14 LUFS, -1 dBTP, LRA 11).",
    )
    ap.add_argument(
        "--fps",
        type=parse_fps,
        default=None,
        help="Output frame rate. Default: preserve the source's frame rate "
             "(falls back to 24 if it can't be probed). Pass e.g. --fps 30 or "
             "--fps 30000/1001 to force.",
    )
    ap.add_argument("--canvas", help="Override the output canvas, e.g. 1080x1350 (deliverable versions)")
    args = ap.parse_args()

    edl_path = args.edl.resolve()
    if not edl_path.exists():
        sys.exit(f"edl not found: {edl_path}")

    edl = json.loads(edl_path.read_text())
    edit_dir = edl_path.parent
    out_path = args.output.resolve()
    if args.canvas:
        width, _, height = args.canvas.lower().partition("x")
        edl["canvas"] = {"width": int(width), "height": int(height)}

    # 1. Extract per-segment (auto-grade per range if EDL grade is "auto")
    plans: list[dict] = []
    segment_paths = extract_all_segments(
        edl, edit_dir, preview=args.preview, draft=args.draft, fps=args.fps, plans_out=plans
    )

    # 2. Concat → base
    if args.draft:
        base_name = "base_draft.mp4"
    elif args.preview:
        base_name = "base_preview.mp4"
    else:
        base_name = "base.mp4"
    base_path = edit_dir / base_name
    concat_segments(segment_paths, base_path, edit_dir)
    rate = resolve_output_rate(edl, edit_dir, args.fps)
    canvas = resolve_canvas(edl, edit_dir, args.draft)
    base_path = apply_transitions(edl, edit_dir, base_path, plans, rate, args.preview, args.draft)

    # 3. Audio master: PCM, equal-power crossfades on every cut
    audio_path = build_audio_track(edl, edit_dir, rate, base_path.with_name(base_path.stem + "_audio.wav"))
    timing = segment_timing(edl["ranges"], rate)
    total_s = timing[-1][1] + timing[-1][2] if timing else 0.0
    from audio_mix import build_mix
    from graphics import output_time
    from sfx_library import auto_sfx
    beat_behind: list[dict] = []
    if edl.get("beats"):
        from beats import expand
        beat_behind, beat_sfx = expand(edl, edit_dir, canvas, rate, _brand_kit(edl, edit_dir))
        if beat_sfx:
            edl = dict(edl, audio=dict(edl.get("audio") or {}, sfx=list((edl.get("audio") or {}).get("sfx") or []) + beat_sfx))
        print(f"beats: {len(edl['beats'])} (behind-subject strobes + sound)")
    extra = auto_sfx(edl, edit_dir, rate, _brand_kit(edl, edit_dir), resolve_at=lambda at: output_time(edl, at, rate))
    if extra:
        print(f"auto sfx: {len(extra)} sound(s) for effects with none — "
              + ", ".join(sorted({f"{e['auto']}" for e in extra})))
        edl = dict(edl, audio=dict(edl.get("audio") or {}, sfx=list((edl.get("audio") or {}).get("sfx") or []) + extra))
    audio_path = build_mix(edl, edit_dir, audio_path, total_s, at_resolver=lambda at: output_time(edl, at, rate))
    audio_filter = None
    if not args.no_loudnorm:
        print("loudness normalization → social-ready (-14 LUFS / -1 dBTP / LRA 11)")
        audio_filter = loudnorm_filter(audio_path, one_pass=args.draft)

    # 4. Brand-driven graphics → ordinary overlay entries
    from overlay_fx import prepared_overlay  # stock overlays keyed / recoloured / glowed on demand
    overlays = [prepared_overlay(o, edit_dir, canvas, rate) for o in (edl.get("overlays") or [])]
    behind_subject_overlays = [prepared_overlay(o, edit_dir, canvas, rate) for o in (edl.get("behind_subject_overlays") or [])]
    behind_subject_overlays += beat_behind
    mask_config = edl.get("subject_mask") or ({"mode": "person"} if beat_behind else None)
    graphic_boxes: list[dict] = []
    faces: list[dict] = []
    if edl.get("graphics") or edl.get("captions"):
        # Rule: no text on anyone's face. Faces are found on the assembled picture.
        try:
            from face_anchor import output_face_boxes
            faces = output_face_boxes(base_path, canvas, edit_dir / "faces")
            print(f"face keep-out: {len(faces)} box(es) on the output")
        except (RuntimeError, OSError, subprocess.CalledProcessError, ValueError) as exc:
            print(f"warning: face keep-out unavailable ({exc}); check text placement by eye")
    if edl.get("graphics"):
        from graphics import build_graphics
        print("graphics (brand kit tokens)")
        from build_captions import make_room_for_captions
        graphic_top, graphic_behind = build_graphics(make_room_for_captions(edl, edit_dir, rate), edit_dir, canvas, rate, faces)
        overlays += graphic_top
        behind_subject_overlays += graphic_behind
        graphic_boxes = graphic_top + graphic_behind
        if graphic_behind and not mask_config:
            mask_config = {"mode": "person"}

    # 5. Captions: explicit file, brand-styled auto captions, or legacy SRT
    subs_path: Path | None = None
    if not args.no_subtitles:
        if args.build_subtitles:
            subs_path = edit_dir / "master.srt"
            build_master_srt(edl, edit_dir, subs_path)
        elif edl.get("subtitles"):
            subs_path = resolve_subtitles_path(edl["subtitles"], edit_dir)
        elif edl.get("captions"):
            from build_captions import build_caption_track, needs_coretext_track, write_styled_captions
            # The ASS is always written: QC reads caption timing from it.
            keep_out = graphic_boxes + faces
            ass_path = write_styled_captions(edl, edit_dir, canvas, keep_out, edit_dir / "captions.ass")
            if needs_coretext_track(edl, edit_dir):
                overlays.append(build_caption_track(edl, edit_dir, canvas, keep_out, rate, total_s,
                                                    edit_dir / "captions_track" / "captions.mov"))
            else:
                subs_path = ass_path

    # The outro covers everything: nothing is drawn over it once it starts to show.
    oi = outro_index(edl, edit_dir)
    if oi is not None:
        cut = segment_timing(edl["ranges"], rate)[oi][1]
        shows = min([c["start_in_output"] for c in TRANSITION_CLIPS if abs(c["start_in_output"] + c["duration"] / 2 - cut) < 1.0]
                    + [cut])

        def under_outro(items: list[dict]) -> list[dict]:
            kept = []
            for ov in items:
                st = float(ov.get("start_in_output", 0))
                if st >= shows - 0.02:
                    continue
                if st + float(ov.get("duration", 0)) > shows:
                    ov = dict(ov, duration=round(shows - st, 4))
                kept.append(ov)
            return kept
        overlays = under_outro(overlays)
        behind_subject_overlays = under_outro(behind_subject_overlays)
        print(f"outro from {shows:.2f}s: overlays end before it")

    # 6. Composite (overlays + subtitles LAST); AAC is encoded once, here
    subject_mask = ensure_subject_mask(base_path, edit_dir, mask_config) if behind_subject_overlays else None
    write_render_manifest(edit_dir, edl, out_path, base_path, canvas, rate, total_s, audio_path, overlays,
                          behind_subject_overlays, subject_mask, subs_path)
    build_final_composite(
        base_path, overlays, subs_path, out_path, edit_dir,
        behind_subject_overlays, subject_mask,
        audio_path=audio_path, audio_filter=audio_filter,
    )

    size_mb = out_path.stat().st_size / (1024 * 1024)
    print(f"\ndone: {out_path} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
