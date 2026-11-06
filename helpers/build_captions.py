"""Build Arabic ASS captions from Video Use transcripts."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
from transcripts import transcript_path as find_transcript  # noqa: E402


def ass_timestamp(seconds: float) -> str:
    centiseconds = max(0, round(seconds * 100))
    hours, rest = divmod(centiseconds, 360_000)
    minutes, rest = divmod(rest, 6_000)
    whole_seconds, remainder = divmod(rest, 100)
    return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{remainder:02d}"


def ass_escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}").replace("\n", r"\N")


RLM = "\u200f"


def rtl_line(text: str) -> str:
    """Force right-to-left reading for any line that contains Arabic.

    libass picks a line's direction from its first strong character, so a cue
    that opens with a Latin term ("dashboard وفاهم كل الـ") would otherwise be
    laid out left-to-right and read in the wrong order.
    """
    return RLM + text if any("\u0590" <= ch <= "\u08ff" for ch in text) else text


def words_in_range(transcript: dict[str, Any], start: float, end: float) -> list[dict[str, Any]]:
    return [word for word in transcript.get("words", []) if word.get("type", "word") == "word" and word.get("start") is not None and word.get("end") is not None and float(word["end"]) > start and float(word["start"]) < end and str(word.get("text", "")).strip()]


# Sentence and clause marks, Arabic and Latin. A caption never continues
# across one: "،" and "؛" are as strong a break in Arabic as "," is in English.
BREAK_MARKS = (".", "!", "?", "؟", "…", "،", "؛", ",", ";", ":")


def grouped_words(words: list[dict[str, Any]], max_words: int) -> list[list[dict[str, Any]]]:
    """Chunk words into captions: never across a clause mark, and balanced —
    a 4-word clause with max 3 reads 2 + 2, never 3 + an orphan flash."""
    output: list[list[dict[str, Any]]] = []
    clause: list[dict[str, Any]] = []

    def flush() -> None:
        if not clause:
            return
        parts = -(-len(clause) // max(1, max_words))
        size, extra = divmod(len(clause), parts)
        index = 0
        for k in range(parts):
            take = size + (1 if k < extra else 0)
            output.append(clause[index:index + take])
            index += take
        clause.clear()

    for word in words:
        clause.append(word)
        if str(word.get("text", "")).rstrip().endswith(BREAK_MARKS):
            flush()
    flush()
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
Style: Arabic,{font},{font_size},&H00FFFFFF,&H0000D7FF,&H00101010,&H80000000,-1,0,0,0,100,100,0,0,1,3,1,2,{side},{side},{margin_v},-1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""
    offset = 0.0
    timed: list[tuple[float, float, str]] = []
    for item in edl.get("ranges", []):
        source = item["source"]
        start, end = float(item["start"]), float(item["end"])
        transcript_path = find_transcript(edit_dir, edl, source)
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
        f"Dialogue: 0,{ass_timestamp(a)},{ass_timestamp(b)},Arabic,,0,0,0,,{rtl_line(ass_escape(text))}"
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


# ---------------------------------------------------------------- styled captions

CAPTION_DEFAULTS = {
    "position": 0.80,      # vertical centre of the caption block, 0 = top, 1 = bottom
    "size_ratio": 0.030,   # font size / canvas height
    "box": False,
    "box_color": "#000000",
    "box_opacity": 0.55,
    "text_color": "#FFFFFF",
    "outline_color": "#101010",
    "highlight": None,     # "#HEX": colour the word being spoken (or every keyword)
    "highlight_mode": "word",  # "word" = karaoke on the spoken word; "keywords" = colour `keywords` all cue long
    "keywords": [],
    "glow": None,          # "#HEX[AA]": soft coloured halo around the text
    "strip_punctuation": False,  # True: no . , ، ؟ ! … on screen
    "fixed": False,        # True: always at `position`, never moved around graphics
    "skip_repeats": False, # True: no caption while a text graphic / the logo shows the same words
    "max_words": 4,
    "font": None,
}


def ass_color(hex_color: str, alpha: float = 1.0) -> str:
    """#RRGGBB + opacity → &HAABBGGRR (ASS alpha: 00 opaque, FF clear)."""
    s = hex_color.lstrip("#")
    r, g, b = s[0:2], s[2:4], s[4:6]
    a = round((1 - max(0.0, min(1.0, alpha))) * 255)
    return f"&H{a:02X}{b}{g}{r}".upper()


def caption_skin(edl: dict[str, Any], edit_dir: Path) -> dict[str, Any]:
    """Defaults ← brand_kit.caption_skin ← EDL captions overrides."""
    sys_path = str(Path(__file__).resolve().parent)
    import sys
    if sys_path not in sys.path:
        sys.path.insert(0, sys_path)
    from graphics import load_brand_kit
    kit = load_brand_kit(edl, edit_dir)
    skin = dict(CAPTION_DEFAULTS)
    skin.update({k: v for k, v in (kit.get("caption_skin") or {}).items() if v is not None})
    fonts = kit.get("fonts") or {}
    if not skin.get("font"):
        skin["font"] = fonts.get("caption") or fonts.get("arabic") or fonts.get("body") or "Geeza Pro"
    if skin["font"] and not str(skin["font"]).lower().endswith((".ttf", ".otf", ".ttc")):
        from graphics import resolve_font_name
        skin["font"] = resolve_font_name(str(skin["font"]))
    overrides = edl.get("captions")
    if isinstance(overrides, dict):
        skin.update({k: v for k, v in overrides.items() if k != "enabled" and v is not None})
    return skin


def caption_cues(edl: dict[str, Any], edit_dir: Path, max_words: int) -> list[tuple[float, float, list[tuple[float, float, str]]]]:
    """(start, end, [(word_start, word_end, text)]) on the output timeline, smoothed.

    Words come from where the *sound* comes from (B-roll / multicam use
    ``audio_source``); a range can opt out with ``"captions": false``. A word
    belongs to the one range holding its midpoint (no repeats at a cut), and
    captions run across picture cuts while the sound is continuous, so a
    cutaway never chops a phrase into flashes.
    """
    offset = 0.0
    runs: list[list[tuple[float, float, str, str]]] = []
    previous: tuple[str, float] | None = None
    transcripts: dict[str, dict] = {}
    for item in edl.get("ranges", []):
        sound = item.get("audio_source") if isinstance(item.get("audio_source"), dict) else None
        source = sound["source"] if sound else item["source"]
        start = float(sound.get("start", item["start"])) if sound else float(item["start"])
        end = start + float(item["end"]) - float(item["start"])
        if item.get("captions") is False:
            offset += end - start
            previous = None
            continue
        if source not in transcripts:
            path = find_transcript(edit_dir, edl, source)
            transcripts[source] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not (previous and previous[0] == source and abs(previous[1] - start) < 0.05 and runs):
            runs.append([])
        for w in words_in_range(transcripts[source], start, end):
            mid = (float(w["start"]) + float(w["end"])) / 2
            if not start <= mid < end:
                continue
            runs[-1].append((offset + max(start, float(w["start"])) - start, offset + min(end, float(w["end"])) - start,
                             str(w["text"]).strip(), ""))
        previous = (source, end)
        offset += end - start
    raw: list[tuple[float, float, list[tuple[float, float, str]]]] = []
    for run in runs:
        items = [{"start": a, "end": b, "text": t} for a, b, t, _ in run]
        groups = grouped_words(items, max_words)
        # A flash under 0.5 s is unreadable: join it to its clause neighbour
        # when the pair stays within one extra word.
        merged: list[list[dict[str, Any]]] = []
        for group in groups:
            if merged and not str(merged[-1][-1]["text"]).rstrip().endswith(BREAK_MARKS) \
                    and len(merged[-1]) + len(group) <= max_words + 1 \
                    and min(merged[-1][-1]["end"] - merged[-1][0]["start"], group[-1]["end"] - group[0]["start"]) < 0.5:
                merged[-1] = merged[-1] + group
            else:
                merged.append(group)
        for group in merged:
            words = [(float(w["start"]), float(w["end"]), w["text"]) for w in group]
            if words[-1][1] > words[0][0]:
                raw.append((words[0][0], words[-1][1], words))
    smoothed = smooth_events([(a, b, str(i)) for i, (a, b, _w) in enumerate(raw)])
    return [(a, b, raw[int(key)][2]) for a, b, key in smoothed]


def _overlaps(a0: float, a1: float, b0: float, b1: float) -> bool:
    return a0 < b1 and b0 < a1


def _norm(word: str) -> str:
    return word.strip(PUNCTUATION).replace("ـ", "").replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").lower()


def repeats_graphic(words: list, cue: tuple[float, float], boxes: list[dict]) -> bool:
    """True when an on-screen text graphic (or the logo) already says this cue.

    Captions would only repeat it, so the cue is dropped. A graphic "says" a
    cue when they overlap in time and share a word; the logo covers the
    brand-name words its entry lists in ``text`` (or any cue under it when it
    lists none).
    """
    cue_words = {_norm(t) for _a, _b, t in words if len(_norm(t)) > 1}
    for b in boxes:
        if b.get("type") in (None, "face", "end_card"):
            continue
        if not _overlaps(cue[0], cue[1], float(b["start_in_output"]), float(b["start_in_output"]) + float(b["duration"])):
            continue
        said = {_norm(t) for t in str(b.get("text") or "").split() if len(_norm(t)) > 1}
        if (b.get("type") == "logo" and not said) or cue_words & said:
            return True
    return False


def make_room_for_captions(edl: dict[str, Any], edit_dir: Path, rate: str) -> dict[str, Any]:
    """With fixed captions, a text graphic in the caption area leaves when the
    next caption it doesn't already say begins — the two never overlap.

    Only graphics near the caption spot are trimmed (top headlines and items
    behind the subject keep their time). Returns a copy of the EDL.
    """
    skin = caption_skin(edl, edit_dir)
    if not skin.get("fixed") or not edl.get("captions"):
        return edl
    from graphics import NAMED_POSITIONS, output_time
    cues = [(a, b, [(x, y, clean_word(t, skin)) for x, y, t in w]) for a, b, w in caption_cues(edl, edit_dir, int(skin["max_words"]))]
    spot = float(skin["position"])
    graphics = []
    for g in edl.get("graphics") or []:
        g = dict(g)
        if g.get("behind_subject") or g.get("type") == "end_card" or g.get("keep_with_captions"):
            graphics.append(g)
            continue
        pos = g.get("position")
        y = float(pos.get("y", 0.5)) if isinstance(pos, dict) else NAMED_POSITIONS.get(str(pos), (0.5, 0.5))[1]
        start = output_time(edl, g["at"], rate) if "at" in g and "start_in_output" not in g else float(g.get("start_in_output", 0))
        end = start + float(g["duration"])
        if abs(y - spot) <= 0.2:
            box = {"type": g.get("type"), "start_in_output": start, "duration": end - start,
                   "text": g.get("covers") or g.get("text") or g.get("title") or ""}
            for a, b, words in cues:
                if start + 0.3 < a < end and not repeats_graphic(words, (a, b), [box]):
                    g["duration"] = round(max(0.6, a - start), 3)
                    break
        graphics.append(g)
    return dict(edl, graphics=graphics)


def caption_margin(canvas: tuple[int, int], skin: dict, cue: tuple[float, float], boxes: list[dict]) -> int | None:
    """MarginV for this cue, moved clear of any graphic in the caption band.

    Returns None when a full-frame graphic (end card) owns the screen.
    """
    w, h = canvas
    size = round(h * float(skin["size_ratio"]))
    block = round(size * 2.8)  # up to two lines plus padding
    centre = float(skin["position"]) * h
    top, bottom = centre - block / 2, centre + block / 2
    active = [b for b in boxes if _overlaps(cue[0], cue[1], float(b["start_in_output"]),
                                            float(b["start_in_output"]) + float(b["duration"]))]
    if any(b.get("type") == "end_card" for b in active):
        return None
    if skin.get("fixed"):  # one steady spot: the eye always knows where to read
        return max(0, round(h - bottom))
    bands = [(float(b["y"]), float(b["y"]) + float(b.get("h", 0))) for b in active]

    def blocked(t: float) -> bool:
        return any(_overlaps(t, t + block, y0, y1) for y0, y1 in bands)

    # Nearest band to the preferred spot that clears every graphic and face.
    if blocked(top):
        gap = round(h * 0.015)
        limit = h * 0.88 - block
        options = sorted({max(0.0, y1 + gap) for _y0, y1 in bands} | {y0 - gap - block for y0, _y1 in bands},
                         key=lambda t: abs(t - top))
        for t in options:
            if h * 0.04 <= t <= limit and not blocked(t):
                top = t
                break
    bottom = top + block
    return max(0, round(h - bottom))


def build_styled_ass(edl: dict[str, Any], edit_dir: Path, canvas: tuple[int, int], boxes: list[dict] | None = None) -> str:
    skin = caption_skin(edl, edit_dir)
    w, h = canvas
    size = round(h * float(skin["size_ratio"]))
    side = round(w * 0.065)
    text = ass_color(skin["text_color"])
    if skin.get("box"):
        # BorderStyle 4 (libass): one box behind the whole line in BackColour,
        # so word-highlight runs do not draw overlapping per-run boxes.
        border_style, outline, shadow = 4, max(4, round(size * 0.3)), 0
        outline_colour = back_colour = ass_color(skin["box_color"], float(skin["box_opacity"]))
    else:
        border_style, outline, shadow = 1, max(2, round(size * 0.06)), 1
        outline_colour = ass_color(skin["outline_color"])
        back_colour = "&H80000000"
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Caption,{skin['font']},{size},{text},{text},{outline_colour},{back_colour},-1,0,0,0,100,100,0,0,{border_style},{outline},{shadow},2,{side},{side},{round(h * (1 - float(skin['position']))) - size},-1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""
    highlight = skin.get("highlight")
    events: list[str] = []
    for start, end, words in caption_cues(edl, edit_dir, int(skin["max_words"])):
        words = [(a, b, clean_word(t, skin)) for a, b, t in words]
        if skin.get("skip_repeats") and repeats_graphic(words, (start, end), boxes or []):
            continue
        margin = caption_margin(canvas, skin, (start, end), boxes or [])
        if margin is None:
            continue
        if not highlight:
            line = " ".join(ass_escape(t) for _a, _b, t in words)
            events.append(f"Dialogue: 0,{ass_timestamp(start)},{ass_timestamp(end)},Caption,,0,0,{margin},,{rtl_line(line)}")
            continue
        colour = ass_color(highlight)[4:]  # BBGGRR
        if keyword_mode(skin):
            line = " ".join(("{\\c&H" + colour + "&}" + ass_escape(t) + "{\\r}") if is_keyword(t, skin.get("keywords") or [])
                            else ass_escape(t) for _a, _b, t in words)
            events.append(f"Dialogue: 0,{ass_timestamp(start)},{ass_timestamp(end)},Caption,,0,0,{margin},,{rtl_line(line)}")
            continue
        for k, (w0, _w1, _t) in enumerate(words):
            k_start = start if k == 0 else w0
            k_end = words[k + 1][0] if k + 1 < len(words) else end
            if k_end <= k_start:
                continue
            parts = [("{\\c&H" + colour + "&}" + ass_escape(t) + "{\\r}") if j == k else ass_escape(t)
                     for j, (_a, _b, t) in enumerate(words)]
            events.append(f"Dialogue: 0,{ass_timestamp(k_start)},{ass_timestamp(k_end)},Caption,,0,0,{margin},,{rtl_line(' '.join(parts))}")
    return header + "\n".join(events) + "\n"


def write_styled_captions(edl: dict[str, Any], edit_dir: Path, canvas: tuple[int, int], boxes: list[dict], out: Path) -> Path:
    out.write_text(build_styled_ass(edl, edit_dir, canvas, boxes), encoding="utf-8")
    print(f"captions → {out.name} (brand caption skin)")
    return out


# ---------------------------------------------------------------- CoreText caption track

def needs_coretext_track(edl: dict[str, Any], edit_dir: Path) -> bool:
    """Arabic + word highlight needs whole-line bidi; libass lays out per run."""
    from graphics import text_engine
    if text_engine() == "pil":
        from text_render import raqm_available
        if not raqm_available():
            return False
    skin = caption_skin(edl, edit_dir)
    if not skin.get("highlight"):
        return False
    for _a, _b, words in caption_cues(edl, edit_dir, int(skin["max_words"])):
        if any("\u0590" <= ch <= "\u08ff" for _s, _e, t in words for ch in t):
            return True
    return False


PUNCTUATION = ".,،؛:!?؟…\"'«»()[]{}-–—"


def clean_word(text: str, skin: dict) -> str:
    """Caption word as shown: punctuation dropped when the skin asks for it."""
    if not skin.get("strip_punctuation"):
        return text
    return text.strip(PUNCTUATION).replace("…", "") or text


def is_keyword(text: str, keywords: list[str]) -> bool:
    bare = text.strip(" .,،؛:!?؟…\"'()[]").lower()
    return any(bare == k.lower() or (len(k) > 3 and k.lower() in bare) for k in keywords)


def keyword_mode(skin: dict) -> bool:
    return skin.get("highlight_mode") == "keywords"


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def build_caption_track(edl: dict[str, Any], edit_dir: Path, canvas: tuple[int, int], boxes: list[dict],
                        rate: str, total: float, out: Path) -> dict:
    """Render captions as one alpha MOV with CoreText (correct Arabic bidi with highlight)."""
    import hashlib
    from fractions import Fraction
    from PIL import Image
    from graphics import encode_alpha_mov, rasterize_cards
    skin = caption_skin(edl, edit_dir)
    w, h = canvas
    size = round(h * float(skin["size_ratio"]))
    side = round(w * 0.065)
    work = edit_dir / "captions_track"
    work.mkdir(parents=True, exist_ok=True)
    states: list[tuple[float, float, int, dict]] = []  # (start, end, margin, card)
    for start, end, words in caption_cues(edl, edit_dir, int(skin["max_words"])):
        words = [(a, b, clean_word(t, skin)) for a, b, t in words]
        if skin.get("skip_repeats") and repeats_graphic(words, (start, end), boxes or []):
            continue
        margin = caption_margin(canvas, skin, (start, end), boxes)
        if margin is None:
            continue
        texts = [t for _a, _b, t in words]
        line = " ".join(texts)
        karaoke = skin.get("highlight") and not keyword_mode(skin)
        targets = [(start if k == 0 else words[k][0], words[k + 1][0] if k + 1 < len(words) else end, k)
                   for k in range(len(words))] if karaoke else [(start, end, None)]
        for k_start, k_end, k in targets:
            if k_end <= k_start:
                continue
            spans = []
            if k is not None:
                offset = sum(_utf16_len(t) + 1 for t in texts[:k])
                spans = [{"start": offset, "length": _utf16_len(texts[k]), "color": skin["highlight"]}]
            elif keyword_mode(skin) and skin.get("highlight"):
                for j, t in enumerate(texts):
                    if is_keyword(t, skin.get("keywords") or []):
                        spans.append({"start": sum(_utf16_len(x) + 1 for x in texts[:j]), "length": _utf16_len(t),
                                      "color": skin["highlight"]})
            card: dict[str, Any] = {
                "lines": [{"text": line, "font": skin["font"], "size": size, "color": skin["text_color"],
                           "weight": "bold", "spans": spans}],
                "padX": round(size * 0.45), "padY": round(size * 0.22), "maxWidth": w - 2 * side,
                "radius": round(size * 0.3),
            }
            if skin.get("box"):
                alpha = round(max(0.0, min(1.0, float(skin["box_opacity"]))) * 255)
                card["fill"] = {"colors": [f"{skin['box_color'][:7]}{alpha:02X}"]}
            else:
                card["textStroke"] = {"color": skin["outline_color"], "blur": -4, "dx": 0, "dy": 0}
                card["shadow"] = {"color": "#00000080", "blur": round(size * 0.12), "dx": 0, "dy": round(size * 0.05)}
                if skin.get("glow"):  # a soft coloured halo, e.g. a brand blue
                    card["glow"] = {"color": skin["glow"], "blur": round(size * 0.35), "dx": 0, "dy": 0}
            key = hashlib.sha1(json.dumps(card, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]
            card["file"] = str(work / f"cap_{key}.png")
            states.append((k_start, k_end, margin, card))
    rasterize_cards([c for *_x, c in states], work)
    images: dict[str, Image.Image] = {}
    fps = float(Fraction(rate))
    frames_total = max(1, round(total * fps))
    cache: dict[tuple[str, int] | None, bytes] = {}

    def frames():
        index = 0
        for frame in range(frames_total):
            t = frame / fps
            while index < len(states) and states[index][1] <= t:
                index += 1
            active = states[index] if index < len(states) and states[index][0] <= t else None
            key = (active[3]["file"], active[2]) if active else None
            if key not in cache:
                canvas_img = Image.new("RGBA", canvas, (0, 0, 0, 0))
                if active:
                    path = active[3]["file"]
                    img = images.setdefault(path, Image.open(path).convert("RGBA"))
                    x = (w - img.width) // 2
                    y = h - active[2] - img.height
                    canvas_img.alpha_composite(img, (max(0, x), max(0, y)))
                if len(cache) > 64:
                    cache.clear()
                cache[key] = canvas_img.tobytes()
            yield cache[key]

    states.sort(key=lambda s: s[0])
    encode_alpha_mov(frames(), canvas, rate, out)
    print(f"captions → {out.name} (CoreText track, {len(states)} states)")
    return {"file": str(out), "start_in_output": 0.0, "duration": total, "x": 0, "y": 0, "type": "captions"}
