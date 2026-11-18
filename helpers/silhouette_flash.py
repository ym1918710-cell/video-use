"""The silhouette flash on a jump cut (the editor's own transition).

When the camera does not move but the person is somewhere else after the cut,
he masks the person on the 5 frames before and the 5 after the cut, gives the
mask a white Drop Shadow with no softness and no distance, "Shadow Only" (so
only a white silhouette of the person shows), and flickers it frame by frame:
the first frame at 100 % opacity, the next at 0 %, and so on.

This makes that layer as one alpha .mov per cut (local macOS Vision masks) and
finds the cuts it belongs on by itself: the background around the person is the
same on both sides (a locked-off camera) and the person has moved.

    python helpers/silhouette_flash.py find  <edl.json>              # which cuts qualify, and why
    python helpers/silhouette_flash.py build <edl.json> [--cuts 3,4]  # render them (default: the ones found)
    python helpers/silhouette_flash.py place <edl.json> --sequence "<name>" [--track 1]   # put them in Premiere

Place them on a track right above the picture and UNDER the zoom Adjustment
Layer, so a zoom scales the silhouette with the person.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))

SWIFT = Path(__file__).with_name("person_mask_images.swift")
BIN = Path(tempfile.gettempdir()) / "video-use-person-mask-images"
FRAMES_EACH_SIDE = 5
BG_STATIC = 6.0       # median |difference| (0–255) of the background around the person: below = locked-off camera
                      # (median, so a moving object the mask leaves out, like a laptop in his hand, doesn't read as a camera move)
PERSON_MOVED = 0.06   # the person counts as moved when centre moves this much (of the frame) or the overlap drops
OVERLAP_KEEP = 0.70   # … or when less than this share of the two silhouettes overlaps


def _bin() -> Path:
    if sys.platform != "darwin" or not shutil.which("swiftc"):
        raise SystemExit("silhouette_flash needs macOS Vision (swiftc)")
    if not BIN.exists() or BIN.stat().st_mtime < SWIFT.stat().st_mtime:
        subprocess.run(["swiftc", str(SWIFT), "-o", str(BIN), "-framework", "Vision", "-framework", "AppKit",
                        "-framework", "CoreImage"], check=True, capture_output=True)
    return BIN


def _resolve(p: str, base: Path) -> Path:
    q = Path(p)
    return q if q.is_absolute() else base / q


def _frame(src: Path, t: float, rot: str | None, out: Path, size: tuple[int, int]) -> Path:
    vf = {"cw": "transpose=1,", "ccw": "transpose=2,"}.get(rot or "", "")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{max(0.0, t):.4f}", "-i", str(src), "-frames:v", "1",
                    "-vf", f"{vf}scale={size[0]}:{size[1]}:force_original_aspect_ratio=increase,crop={size[0]}:{size[1]}",
                    str(out)], check=True)
    return out


def _masks(pairs: list[tuple[Path, Path]], quality: str = "accurate") -> None:
    args = [str(_bin()), quality]
    for a, b in pairs:
        args += [str(a), str(b)]
    subprocess.run(args, check=True, capture_output=True)


def cuts(edl: dict) -> list[dict]:
    """Every cut between two picture ranges, with output time and the source frames on each side."""
    from render import segment_timing
    fps = 25.0
    ranges = edl["ranges"]
    tm = segment_timing(ranges, "25")
    out = []
    for k in range(1, len(ranges)):
        a, b = ranges[k - 1], ranges[k]
        if a.get("outro") or b.get("outro") or a.get("broll") or b.get("broll"):
            continue
        out.append({"index": k, "t": tm[k][1], "fps": fps,
                    "out": {"source": a["source"], "end": float(a["start"]) + tm[k - 1][2]},
                    "in": {"source": b["source"], "start": float(b["start"])}})
    return out


def judge(edl: dict, base: Path, work: Path) -> list[dict]:
    """Which cuts are a locked-off camera with the person moved."""
    work.mkdir(parents=True, exist_ok=True)
    rot = edl.get("source_rotations") or {}
    size = (270, 480)
    rows, pairs = [], []
    for c in cuts(edl):
        f = 1 / c["fps"]
        fa = _frame(_resolve(edl["sources"][c["out"]["source"]], base), c["out"]["end"] - f, rot.get(c["out"]["source"]),
                    work / f"j{c['index']:02d}a.png", size)
        fb = _frame(_resolve(edl["sources"][c["in"]["source"]], base), c["in"]["start"], rot.get(c["in"]["source"]),
                    work / f"j{c['index']:02d}b.png", size)
        pairs += [(fa, work / f"j{c['index']:02d}a_m.png"), (fb, work / f"j{c['index']:02d}b_m.png")]
        rows.append((c, fa, fb))
    _masks(pairs, "balanced")
    found = []
    for c, fa, fb in rows:
        A = np.asarray(Image.open(fa).convert("L"), np.float32)
        B = np.asarray(Image.open(fb).convert("L"), np.float32)
        ma = np.asarray(Image.open(str(fa)[:-4] + "_m.png").convert("L"), np.float32) / 255
        mb = np.asarray(Image.open(str(fb)[:-4] + "_m.png").convert("L"), np.float32) / 255
        bg = (ma < 0.2) & (mb < 0.2)
        if bg.any():  # spread of the difference around its median: an exposure shift between two takes moves every
            d = (A - B)[bg]  # background pixel the same way and isn't a camera move; a camera move scatters them
            bg_diff = float(np.median(np.abs(d - np.median(d))))
        else:
            bg_diff = 255.0
        def centre(m):
            ys, xs = np.nonzero(m > 0.5)
            return (xs.mean() / m.shape[1], ys.mean() / m.shape[0]) if len(xs) else (0.5, 0.5)
        ca, cb = centre(ma), centre(mb)
        shift = float(np.hypot(ca[0] - cb[0], ca[1] - cb[1]))
        inter = float(((ma > 0.5) & (mb > 0.5)).sum())
        overlap = inter / max(1.0, float(min((ma > 0.5).sum(), (mb > 0.5).sum())))
        static = bg_diff < BG_STATIC
        moved = shift > PERSON_MOVED or overlap < OVERLAP_KEEP
        found.append({**c, "bg_diff": round(bg_diff, 1), "person_shift": round(shift, 3), "overlap": round(overlap, 2),
                      "locked_off": static, "moved": moved, "use": bool(static and moved)})
    return found


def build_one(edl: dict, base: Path, c: dict, out: Path, work: Path) -> Path:
    """Render the flicker for one cut: 5 frames of the outgoing shot, 5 of the incoming, white silhouette, 100/0/100…"""
    W, H = edl["canvas"]["width"], edl["canvas"]["height"]
    rot = edl.get("source_rotations") or {}
    f = 1 / c["fps"]
    shots = [(c["out"]["source"], c["out"]["end"] - (FRAMES_EACH_SIDE - i) * f) for i in range(FRAMES_EACH_SIDE)] + \
            [(c["in"]["source"], c["in"]["start"] + i * f) for i in range(FRAMES_EACH_SIDE)]
    work.mkdir(parents=True, exist_ok=True)
    pairs = []
    for i, (s, t) in enumerate(shots):
        fr = _frame(_resolve(edl["sources"][s], base), t, rot.get(s), work / f"c{c['index']:02d}_{i}.png", (W, H))
        pairs.append((fr, work / f"c{c['index']:02d}_{i}_m.png"))
    _masks(pairs, "accurate")
    p = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{W}x{H}",
                          "-r", str(int(c["fps"])), "-i", "-", "-c:v", "qtrle", "-pix_fmt", "argb", str(out)], stdin=subprocess.PIPE)
    for i, (_, m) in enumerate(pairs):
        a = np.asarray(Image.open(m).convert("L"), np.uint8) if m.exists() else np.zeros((H, W), np.uint8)
        frame = np.zeros((H, W, 4), np.uint8)
        frame[..., :3] = 255                               # white: the shadow colour
        frame[..., 3] = a if i % 2 == 0 else 0             # first frame 100 %, next 0 %, and so on
        p.stdin.write(frame.tobytes())
    p.stdin.close()
    if p.wait():
        raise SystemExit(f"ffmpeg failed for {out}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=("find", "build", "place"))
    ap.add_argument("edl", type=Path)
    ap.add_argument("--cuts", help="comma-separated cut indices (the range that starts after the cut); default: the ones found")
    ap.add_argument("--sequence")
    ap.add_argument("--track", type=int, default=1, help="video track index for place (0 = V1); default V2")
    a = ap.parse_args()
    edl = json.loads(a.edl.read_text(encoding="utf-8"))
    base = a.edl.resolve().parent
    out_dir = base / "silhouette_flash"
    work = out_dir / "work"
    report = out_dir / "cuts.json"
    if a.cmd == "find":
        rows = judge(edl, base, work)
        out_dir.mkdir(exist_ok=True)
        report.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        for r in rows:
            print(f"cut {r['index']:2d} @ {r['t']:6.2f}s  bg {r['bg_diff']:5.1f}  shift {r['person_shift']:.3f}  "
                  f"overlap {r['overlap']:.2f}  → {'FLASH' if r['use'] else '-'}")
        return
    rows = json.loads(report.read_text(encoding="utf-8")) if report.exists() else judge(edl, base, work)
    want = {int(x) for x in a.cuts.split(",")} if a.cuts else {r["index"] for r in rows if r["use"]}
    chosen = [r for r in rows if r["index"] in want]
    if a.cmd == "build":
        for r in chosen:
            print(build_one(edl, base, r, out_dir / f"flash_cut{r['index']:02d}.mov", work))
        return
    from premiere import Bridge
    b = Bridge()
    b.sequence = a.sequence
    b.fps = 25
    files = [str(out_dir / f"flash_cut{r['index']:02d}.mov") for r in chosen]
    b("importFiles", paths=files, bin=a.sequence)
    b("ensureTracks", video=a.track + 1, audio=1)
    for r, f in zip(chosen, files):
        st = round(r["t"] - FRAMES_EACH_SIDE / r["fps"], 3)
        b("place", path=f, kind="video", track=a.track, start=st, **{"in": 0.0, "out": 2 * FRAMES_EACH_SIDE / r["fps"]},
          name=f"silhouette flash (cut {r['index']})")
        print("placed", f, "at", st)


if __name__ == "__main__":
    main()
