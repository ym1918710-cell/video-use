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
    vf_parts.append(framing_filter(canvas, size, punch_in, framing))
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


def extract_all_segments(
    edl: dict,
    edit_dir: Path,
    preview: bool,
    draft: bool = False,
    fps: str | None = None,
) -> list[Path]:
    """Extract every EDL range into edit_dir/clips_graded/seg_NN.mp4.
    Returns the ordered list of segment paths.

    If the EDL `grade` is "auto", analyze each segment range with
    `auto_grade_for_clip` and apply a per-segment subtle correction.
    Otherwise, apply the same preset/raw filter to every segment.
    """
    resolved = resolve_grade_filter(edl.get("grade"))
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
        for src_name in dict.fromkeys(r["source"] for r in ranges):
            spans = [
                (float(r["start"]), float(r["end"]) - float(r["start"]))
                for r in ranges if r["source"] == src_name
            ]
            src_path = resolve_path(sources[src_name], edit_dir)
            source_grades[src_name], _ = auto_grade_for_ranges(src_path, spans)
            print(f"        {src_name} grade: {source_grades[src_name] or '(none)'}")

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

        note = r.get("beat") or r.get("note") or ""
        print(f"  [{i:02d}] {src_name}  {start:7.2f}-{start + duration:7.2f}  ({duration:5.2f}s, {frames}f)  {note}")
        extract_segment(
            src_path, start, duration, seg_filter, out_path,
            preview=preview, draft=draft, rate=out_rate, punch_in=r.get("punch_in"),
            rotation=source_rotations.get(src_name),
            canvas=canvas, framing=source_framing.get(src_name),
            frames=frames, include_audio=False,
        )
        seg_paths.append(out_path)

    return seg_paths


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
    for i, r in enumerate(ranges):
        src_name = r["source"]
        src = resolve_path(sources[src_name], edit_dir)
        if src_name not in durations:
            durations[src_name] = probe_media_duration(src)
        src_dur = durations[src_name]
        _frames, offset, duration = timing[i]
        start = float(r["start"])
        end = start + duration

        # Handles only where a neighbour exists to crossfade with.
        lead = min(half, start) if i > 0 else 0.0
        tail = half if i < len(ranges) - 1 else 0.0
        if src_dur is not None:
            tail = max(0.0, min(tail, src_dur - end))
        fade_in = lead + half if i > 0 else 0.005
        fade_out = tail + half if i < len(ranges) - 1 else 0.005
        length = lead + duration + tail
        fade_in = min(fade_in, length / 2)
        fade_out = min(fade_out, length / 2)

        part = parts_dir / f"a_{i:03d}.wav"
        cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-ss", f"{start - lead:.6f}", "-t", f"{length:.6f}",
            "-i", str(src),
            "-map", "0:a:0", "-vn",
            "-af", (
                f"aresample={AUDIO_RATE},apad,atrim=0:{length:.6f},"
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
    print(f"audio → {out_wav.name}  ({len(placements)} segment(s), {crossfade_ms:.0f}ms equal-power crossfades)")
    return out_wav


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


def build_master_srt(edl: dict, edit_dir: Path, out_path: Path) -> None:
    """Build an output-timeline SRT from per-source transcripts.

    - 2-word chunks (break on any punctuation in between)
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

        tr_path = transcripts_dir / f"{src_name}.json"
        if not tr_path.exists():
            print(f"  no transcript for {src_name}, skipping captions for this segment")
            seg_offset += seg_duration
            continue

        transcript = json.loads(tr_path.read_text())
        words_in_seg = _words_in_range(transcript, seg_start, seg_end)

        # Group into 2-word chunks, break on punctuation
        chunks: list[list[dict]] = []
        current: list[dict] = []
        for w in words_in_seg:
            text = (w.get("text") or "").strip()
            if not text:
                continue
            current.append(w)
            # Break if the current text ends in punctuation or we hit 2 words
            ends_in_punct = bool(text) and text[-1] in PUNCT_BREAK
            if len(current) >= 2 or ends_in_punct:
                chunks.append(current)
                current = []
        if current:
            chunks.append(current)

        for chunk in chunks:
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
LOUDNORM_TP = -1.0
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
        mask_path = edit_dir / "masks" / f"{base_path.stem}_person.mkv"
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
    args = ap.parse_args()

    edl_path = args.edl.resolve()
    if not edl_path.exists():
        sys.exit(f"edl not found: {edl_path}")

    edl = json.loads(edl_path.read_text())
    edit_dir = edl_path.parent
    out_path = args.output.resolve()

    # 1. Extract per-segment (auto-grade per range if EDL grade is "auto")
    segment_paths = extract_all_segments(
        edl, edit_dir, preview=args.preview, draft=args.draft, fps=args.fps
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

    # 3. Subtitles: build if requested, resolve final path
    subs_path: Path | None = None
    if not args.no_subtitles:
        if args.build_subtitles:
            subs_path = edit_dir / "master.srt"
            build_master_srt(edl, edit_dir, subs_path)
        elif edl.get("subtitles"):
            subs_path = resolve_path(edl["subtitles"], edit_dir)
            if not subs_path.exists():
                print(f"warning: subtitles path in EDL does not exist: {subs_path}")
                subs_path = None

    # 4. Audio master: PCM, equal-power crossfades on every cut
    rate = resolve_output_rate(edl, edit_dir, args.fps)
    audio_path = build_audio_track(edl, edit_dir, rate, base_path.with_name(base_path.stem + "_audio.wav"))
    audio_filter = None
    if not args.no_loudnorm:
        print("loudness normalization → social-ready (-14 LUFS / -1 dBTP / LRA 11)")
        audio_filter = loudnorm_filter(audio_path, one_pass=args.draft)

    # 5. Brand-driven graphics → ordinary overlay entries
    overlays = list(edl.get("overlays") or [])
    behind_subject_overlays = list(edl.get("behind_subject_overlays") or [])
    mask_config = edl.get("subject_mask")
    if edl.get("graphics"):
        from graphics import build_graphics
        print("graphics (brand kit tokens)")
        graphic_top, graphic_behind = build_graphics(edl, edit_dir, resolve_canvas(edl, edit_dir, args.draft), rate)
        overlays += graphic_top
        behind_subject_overlays += graphic_behind
        if graphic_behind and not mask_config:
            mask_config = {"mode": "person"}

    # 6. Composite (overlays + subtitles LAST); AAC is encoded once, here
    subject_mask = ensure_subject_mask(base_path, edit_dir, mask_config) if behind_subject_overlays else None
    build_final_composite(
        base_path, overlays, subs_path, out_path, edit_dir,
        behind_subject_overlays, subject_mask,
        audio_path=audio_path, audio_filter=audio_filter,
    )

    size_mb = out_path.stat().st_size / (1024 * 1024)
    print(f"\ndone: {out_path} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
