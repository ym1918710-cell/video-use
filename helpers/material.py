"""The user's material folder: every asset the tool may draw on, indexed once.

Editors keep a folder of licensed material — sound effects, music, fonts,
graphics (alpha MOVs, green-screen clips), backgrounds, transitions, LUTs,
templates. Register it once and the tool knows what is there::

    python helpers/material.py add ~/Desktop/material        # remember + scan
    python helpers/material.py scan                          # rescan every registered folder
    python helpers/material.py search graphic "question mark"
    python helpers/material.py summary

What it gives the edit:
- **sfx** → ``sfx_library`` picks a sound for every effect by role (whoosh, pop,
  impact, shimmer, zoom...), loudness-matched.
- **music** → ``library.py suggest`` ranks beds by recipe (bpm, energy).
- **font** → a brand kit may name a font family ("Cairo Black"); it resolves to
  the file here. Arabic coverage is recorded so Arabic text never gets a
  Latin-only font.
- **graphic / background / transition** → each clip records alpha, green- or
  blue-screen, size and duration, so ``overlay_fx.py`` knows whether to key it.
- **lut / template** → listed (grading and .mogrt/.aep need their own tools).

Registered folders live in ``~/video-use-library/config.json`` (or the
``VIDEO_USE_MATERIAL`` env var, ``:``-separated); the index shares
``library.json`` with ``library.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from library import AUDIO_EXTS, item_id, library_path, load, save  # noqa: E402

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
FONT_EXTS = {".ttf", ".otf", ".ttc"}
LUT_EXTS = {".cube", ".3dl", ".zip"}
TEMPLATE_EXTS = {".mogrt", ".aep", ".prproj", ".psd", ".prfpset"}
FOLDER_KINDS = [  # folder-name words → asset kind (first match wins)
    ("sfx", ("sound effect", "sound effects", "sfx", "soundfx")),
    ("music", ("music", "songs", "tracks", "beds")),
    ("font", ("font",)),
    ("transition", ("transition",)),
    ("background", ("bg", "background", "backdrop")),
    ("lut", ("lut",)),
    ("template", ("template", "mogrt")),
    ("graphic", ("graphic", "overlay", "element", "asset", "vfx", "stock")),
    ("skip", ("plugin", "plugn", "installer")),
]
WEIGHTS = ["thin", "extralight", "light", "regular", "medium", "semibold", "bold", "extrabold", "black", "heavy"]


def config_path() -> Path:
    return library_path().with_name("config.json")


def folders() -> list[Path]:
    env = os.environ.get("VIDEO_USE_MATERIAL")
    paths = [Path(p).expanduser() for p in env.split(":") if p] if env else []
    if config_path().exists():
        paths += [Path(p) for p in json.loads(config_path().read_text(encoding="utf-8")).get("material", [])]
    seen, out = set(), []
    for p in paths:
        if str(p) not in seen and p.exists():
            seen.add(str(p))
            out.append(p)
    return out


def remember(folder: Path) -> None:
    cfg = json.loads(config_path().read_text(encoding="utf-8")) if config_path().exists() else {}
    cfg["material"] = sorted(set(cfg.get("material", [])) | {str(folder.resolve())})
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def kind_for(path: Path, root: Path) -> str | None:
    rel = str(path.relative_to(root).parent).lower()
    ext = path.suffix.lower()
    for kind, words in FOLDER_KINDS:
        if any(w in rel for w in words):
            if kind == "skip":
                return None
            # A folder hint must still fit the file type.
            if kind in ("sfx", "music") and ext not in AUDIO_EXTS:
                return "transition" if kind == "sfx" and ext in VIDEO_EXTS else None
            if kind == "font" and ext not in FONT_EXTS:
                return None
            if kind in ("graphic", "background", "transition") and ext not in VIDEO_EXTS | IMAGE_EXTS:
                return "sfx" if ext in AUDIO_EXTS else None
            return kind
    if ext in AUDIO_EXTS:
        return "audio"  # decided by duration below
    if ext in FONT_EXTS:
        return "font"
    if ext in VIDEO_EXTS | IMAGE_EXTS:
        return "graphic"
    if ext in LUT_EXTS:
        return "lut"
    if ext in TEMPLATE_EXTS:
        return "template"
    return None


def tags_from_name(name: str) -> list[str]:
    words = re.split(r"[\s_\-\.\(\)\[\]@#!,]+", name.lower())
    return [w for w in words if len(w) > 1 and not w.isdigit() and not re.fullmatch(r"\d{4,}.*|utc|hd|fps|\d+p", w)]


def probe(path: Path) -> dict[str, Any]:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height,pix_fmt,codec_name:format=duration",
                          "-of", "json", str(path)], capture_output=True, text=True, errors="replace").stdout
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {}
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    audio = any(s.get("codec_type") == "audio" for s in data.get("streams", []))
    try:
        duration = float(data.get("format", {}).get("duration") or 0)
    except ValueError:
        duration = 0.0
    pix = str(video.get("pix_fmt") or "")
    return {"width": video.get("width"), "height": video.get("height"), "duration_s": round(duration, 2),
            "alpha": any(k in pix for k in ("rgba", "argb", "bgra", "yuva", "gbrap", "ya8")), "has_audio": audio,
            "codec": video.get("codec_name")}


def screen_colour(path: Path, duration: float) -> str | None:
    """'green' / 'blue' when a frame is mostly chroma-key colour."""
    t = max(0.0, min(duration * 0.5, duration - 0.05)) if duration else 0
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1", "-vf", "scale=64:36",
                          "-pix_fmt", "rgb24", "-f", "rawvideo", "-"], capture_output=True).stdout
    if len(raw) < 64 * 36 * 3:
        return None
    px = [raw[k:k + 3] for k in range(0, len(raw), 3)]
    green = sum(1 for r, g, b in px if g > 140 and g > r * 1.6 and g > b * 1.6) / len(px)
    blue = sum(1 for r, g, b in px if b > 140 and b > r * 1.8 and b > g * 1.4) / len(px)
    return "green" if green > 0.35 else "blue" if blue > 0.35 else None


def font_info(path: Path) -> dict[str, Any]:
    info: dict[str, Any] = {"family": path.stem, "weight": "regular", "arabic": False}
    try:
        from fontTools.ttLib import TTCollection, TTFont
        font = TTCollection(str(path)).fonts[0] if path.suffix.lower() == ".ttc" else TTFont(str(path), lazy=True)
        names = font["name"]
        family = names.getDebugName(16) or names.getDebugName(1)
        sub = names.getDebugName(17) or names.getDebugName(2) or ""
        full = names.getDebugName(4) or f"{family} {sub}"
        cmap = font.getBestCmap() or {}
        info.update(family=family or path.stem, style=sub, full_name=full, arabic=0x0628 in cmap and 0x0644 in cmap)
    except Exception:
        pass
    low = (info.get("full_name") or path.stem).lower().replace(" ", "").replace("-", "")
    for w in reversed(WEIGHTS):
        if w in low:
            info["weight"] = {"heavy": "black", "extrabold": "heavy"}.get(w, w)
            break
    return info


def scan_folder(root: Path, library: dict[str, Any], refresh: bool = False) -> dict[str, int]:
    from library import analyze
    from sfx_library import classify
    counts: dict[str, int] = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file() and not p.name.startswith(("._", "."))):
        kind = kind_for(path, root)
        if kind is None:
            continue
        key = item_id(path)
        item = library["items"].get(key)
        if item and not refresh and item.get("mtime") == int(path.stat().st_mtime):
            counts[item["type"]] = counts.get(item["type"], 0) + 1
            continue
        item = dict(item or {"moods": [], "notes": ""})
        item.update({"id": key, "file": str(path.resolve()), "name": path.stem, "mtime": int(path.stat().st_mtime),
                     "source": str(root), "tags": tags_from_name(path.stem)})
        try:
            if kind in ("sfx", "music", "audio"):
                item.update(analyze(path))
                if kind == "audio":
                    kind = "sfx" if float(item.get("duration_s") or 0) < 12 else "music"
                if kind == "sfx":
                    item["role"] = classify(path.stem)
            elif kind == "font":
                item.update(font_info(path))
            elif kind in ("graphic", "background", "transition"):
                item.update(probe(path))
                item["media"] = "image" if path.suffix.lower() in IMAGE_EXTS else "video"
                if item["media"] == "video" and not item.get("alpha"):
                    item["screen"] = screen_colour(path, float(item.get("duration_s") or 0))
                elif item["media"] == "image" and path.suffix.lower() == ".png":
                    item["alpha"] = True  # PNG overlays are almost always cut out
        except Exception as exc:  # one broken file never stops the scan
            item["error"] = str(exc)[:200]
        item["type"] = kind
        library["items"][key] = item
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def search(kind: str | None, query: str, library: dict[str, Any] | None = None, limit: int = 10) -> list[dict[str, Any]]:
    """Rank assets of ``kind`` by how many query words their name/tags hold."""
    library = library or load()
    words = [w for w in re.split(r"\s+", query.lower().strip()) if w]
    scored = []
    for item in library.get("items", {}).values():
        if kind and item.get("type") != kind:
            continue
        hay = " ".join([item.get("name", ""), " ".join(item.get("tags") or []), str(item.get("role") or ""),
                        str(item.get("family") or ""), " ".join(item.get("moods") or [])]).lower()
        score = sum(2 if re.search(rf"\b{re.escape(w)}\b", hay) else 1 if w in hay else 0 for w in words) if words else 1
        if score:
            scored.append((score, item))
    scored.sort(key=lambda s: (-s[0], s[1].get("name", "")))
    return [item for _s, item in scored[:limit]]


def find_font(name: str, arabic: bool | None = None) -> str | None:
    """Resolve a font family/full name ("Cairo Black", "Cairo-Bold") to a file."""
    want = re.sub(r"[\s_\-]+", "", name.lower())
    partial: list[dict] = []
    for item in load().get("items", {}).values():
        if item.get("type") != "font" or (arabic and not item.get("arabic")):
            continue
        names = [item.get("name", ""), item.get("full_name") or "", f"{item.get('family', '')}{item.get('weight', '')}"]
        keys = [re.sub(r"[\s_\-]+", "", n.lower()) for n in names]
        if want in keys:
            return item["file"]
        if any(want in k or k in want for k in keys if k):
            partial.append(item)
    if not partial:
        return None
    # A bare family name means its everyday weight, not ExtraLight or Black.
    order = {"bold": 0, "regular": 1, "semibold": 2, "medium": 3, "extrabold": 4, "black": 5}
    return min(partial, key=lambda i: (order.get(i.get("weight", ""), 9), i.get("name", "")))["file"]


def summary(library: dict[str, Any] | None = None) -> dict[str, Any]:
    library = library or load()
    out: dict[str, Any] = {}
    for item in library.get("items", {}).values():
        kind = item.get("type", "?")
        entry = out.setdefault(kind, {"count": 0})
        entry["count"] += 1
        if kind == "sfx" and item.get("role"):
            entry.setdefault("roles", {}).setdefault(item["role"], 0)
            entry["roles"][item["role"]] += 1
        if kind in ("graphic", "transition", "background"):
            key = "alpha" if item.get("alpha") else (f"{item['screen']}_screen" if item.get("screen") else "plain")
            entry.setdefault("keying", {}).setdefault(key, 0)
            entry["keying"][key] += 1
        if kind == "font":
            entry.setdefault("arabic", 0)
            entry["arabic"] += 1 if item.get("arabic") else 0
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Index the user's material folder")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_add = sub.add_parser("add", help="register a folder and scan it")
    p_add.add_argument("folder", type=Path)
    p_scan = sub.add_parser("scan", help="rescan registered folders (only changed files)")
    p_scan.add_argument("--refresh", action="store_true")
    p_search = sub.add_parser("search")
    p_search.add_argument("type", choices=["sfx", "music", "font", "graphic", "background", "transition", "lut", "template", "any"])
    p_search.add_argument("query", nargs="?", default="")
    p_search.add_argument("--limit", type=int, default=10)
    sub.add_parser("summary")
    args = parser.parse_args()

    if args.cmd in ("add", "scan"):
        if args.cmd == "add":
            remember(args.folder.expanduser())
        library = load()
        for folder in folders():
            counts = scan_folder(folder, library, refresh=getattr(args, "refresh", False))
            print(f"{folder}: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
        save(library)
        print(json.dumps(summary(library), ensure_ascii=False, indent=2))
    elif args.cmd == "search":
        for item in search(None if args.type == "any" else args.type, args.query, limit=args.limit):
            extra = item.get("role") or item.get("screen") or ("alpha" if item.get("alpha") else "") or item.get("family") or ""
            print(f"  {item['id']}  {item['type']:<10} {item.get('duration_s', '') or '':>6}  {extra:<10} {item['file']}")
    else:
        print(json.dumps(summary(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
