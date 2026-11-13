"""Reel review from the EDL, before any render (works for both paths).

Short-form rules (weekly learning, 2026-11-13; general practice, not measured):
- the hook moves or asks within the first 3 s;
- something changes on screen every 2–3 s (a cut, a zoom move, a graphic,
  an overlay, a B-roll, a transition);
- J/L cuts keep the joins smooth (reported, not forced).

    python helpers/reel_review.py edit/edl.json [--max-gap 3.0]

Prints the visual-change timeline and every stretch longer than ``--max-gap``
with no change, so the plan can add a zoom, a graphic or a cut there.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def visual_events(edl: dict, edit_dir: Path, rate: str = "25/1") -> tuple[list[tuple[float, str]], float]:
    from graphics import Tokens, graphic_start, load_brand_kit
    from render import outro_index, segment_timing
    ranges = edl.get("ranges") or []
    timing = segment_timing(ranges, rate)
    oi = outro_index(edl, edit_dir)
    end = timing[oi][1] if oi is not None else (timing[-1][1] + timing[-1][2] if timing else 0.0)
    events: list[tuple[float, str]] = []
    for k, (r, (_n, start, _d)) in enumerate(zip(ranges, timing)):
        if k == oi:
            continue
        if k:
            same = r.get("source") == ranges[k - 1].get("source") and r.get("broll") == ranges[k - 1].get("broll")
            label = "B-roll" if r.get("broll") else ("cut" if same else "new shot")
            events.append((start, label + (f" + {r['transition']['type']}" if r.get("transition") else "")))
        keys = r.get("camera") if isinstance(r.get("camera"), list) else []
        for a, b in zip(keys, keys[1:]):
            if abs(float(b.get("scale", 1)) - float(a.get("scale", 1))) >= 0.05:
                events.append((start + float(a.get("t", 0)), f"zoom {a.get('scale')}→{b.get('scale')}"))
    tokens = Tokens(load_brand_kit(edl, edit_dir), edit_dir)
    for g in edl.get("graphics") or []:
        try:
            t = graphic_start(g, edl, tokens, rate)
        except Exception:
            continue
        events.append((t, f"graphic: {str(g.get('text') or g.get('type'))[:24]}"))
    for o in edl.get("overlays") or []:
        events.append((float(o.get("start_in_output", 0)), f"overlay: {Path(str(o.get('file'))).stem[:24]}"))
    for b in edl.get("behind_subject_overlays") or []:
        events.append((float(b.get("start_in_output", 0)), "behind-subject graphic"))
    return sorted((round(t, 2), e) for t, e in events if 0 <= t < end), end


def moving_spans(edl: dict, edit_dir: Path, rate: str = "25/1") -> list[tuple[float, float]]:
    """Time covered by something that animates by itself (overlays, B-roll):
    the picture is changing there even with no new event."""
    from render import segment_timing
    spans = [(float(o.get("start_in_output", 0)), float(o.get("start_in_output", 0)) + float(o.get("duration", 0)))
             for o in (edl.get("overlays") or []) + (edl.get("behind_subject_overlays") or [])]
    for r, (_n, start, dur) in zip(edl.get("ranges") or [], segment_timing(edl.get("ranges") or [], rate)):
        if r.get("broll") and not r.get("outro"):
            spans.append((start, start + dur))
    return spans


def review(edl: dict, edit_dir: Path, max_gap: float = 3.0, hook_s: float = 3.0, rate: str = "25/1") -> dict:
    events, end = visual_events(edl, edit_dir, rate)
    times = [0.0] + [t for t, _ in events] + [end]
    moving = moving_spans(edl, edit_dir, rate)

    def still_parts(a: float, b: float) -> list[tuple[float, float]]:
        parts = [(a, b)]
        for m0, m1 in moving:
            parts = [q for x, y in parts for q in ((x, min(y, m0)), (max(x, m1), y)) if q[1] - q[0] > 1e-3]
        return parts
    gaps = [g for a, b in zip(times, times[1:]) for g in still_parts(a, b) if g[1] - g[0] > max_gap]
    hook = [e for t, e in events if t <= hook_s]
    return {"duration_before_outro": round(end, 2), "changes": len(events),
            "changes_per_10s": round(10 * len(events) / max(end, 0.1), 1),
            "hook_change_in_3s": bool(hook), "hook_events": hook,
            "static_stretches": [{"from": round(a, 2), "to": round(b, 2), "length": round(b - a, 2)} for a, b in gaps],
            "events": events}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("edl", type=Path)
    ap.add_argument("--max-gap", type=float, default=3.0)
    ap.add_argument("--fps", default=None)
    args = ap.parse_args()
    edl_path = args.edl.resolve()
    edl = json.loads(edl_path.read_text(encoding="utf-8"))
    from render import resolve_output_rate
    rate = args.fps or resolve_output_rate(edl, edl_path.parent, None)
    out = review(edl, edl_path.parent, args.max_gap, rate=rate)
    print(f"{out['changes']} visual changes in {out['duration_before_outro']} s ({out['changes_per_10s']} per 10 s)")
    print("hook: " + ("moves in the first 3 s — " + ", ".join(out["hook_events"]) if out["hook_change_in_3s"] else "NOTHING moves in the first 3 s"))
    for g in out["static_stretches"]:
        print(f"  static {g['from']:6.2f}–{g['to']:6.2f}  ({g['length']} s): add a zoom, a graphic or a cut")
    if not out["static_stretches"]:
        print(f"  no stretch longer than {args.max_gap} s without a change")
    (edl_path.parent / "reel_review.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
