"""Review an editorial plan against an EDL before render.

This catches decision-contract violations without attempting to replace an
editor's taste with a scoring algorithm. It is advisory: the agent reads the
report and explains any intentional exception before seeking approval.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> bool:
    return a_start < b_end and b_start < a_end


def review(edl: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    warnings: list[dict[str, str]] = []
    ranges = edl.get("ranges") or []
    for index, moment in enumerate(plan.get("protected_moments") or []):
        try:
            source = moment["source"]
            start, end = float(moment["start"]), float(moment["end"])
        except (KeyError, TypeError, ValueError):
            warnings.append({"code": "invalid_protected_moment", "path": f"protected_moments[{index}]", "message": "A protected moment needs source, start, and end."})
            continue
        covering = [item for item in ranges if item.get("source") == source and float(item.get("start", 0)) <= start and float(item.get("end", 0)) >= end]
        partial = [item for item in ranges if item.get("source") == source and overlap(float(item.get("start", 0)), float(item.get("end", 0)), start, end)]
        if partial and not covering:
            warnings.append({"code": "protected_moment_partially_cut", "path": f"protected_moments[{index}]", "message": "A kept range intersects this protected moment but does not contain all of it."})
        elif not partial:
            warnings.append({"code": "protected_moment_omitted", "path": f"protected_moments[{index}]", "message": "No selected range contains this protected moment."})

    for kind, entries in (("b_roll", plan.get("b_roll") or []), ("sound_effects", plan.get("sound_effects") or [])):
        for index, entry in enumerate(entries):
            if entry.get("status") not in {"proposed", "approved", "provided"}:
                warnings.append({"code": "asset_status_missing", "path": f"{kind}[{index}]", "message": "Asset plans must record status: proposed, approved, or provided."})
            if entry.get("status") == "proposed" and entry.get("file"):
                warnings.append({"code": "unapproved_asset_has_file", "path": f"{kind}[{index}]", "message": "A proposed asset cannot be treated as a supplied media file."})
    return {"passed": not warnings, "warnings": warnings}


def main() -> None:
    parser = argparse.ArgumentParser(description="Review Video Use protected moments and proposed assets")
    parser.add_argument("edl", type=Path)
    parser.add_argument("--plan", type=Path, default=None)
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()
    edl_path = args.edl.resolve()
    plan_path = (args.plan or edl_path.parent / "editorial_plan.json").resolve()
    if not plan_path.exists():
        print("No editorial plan exists; nothing to review.")
        raise SystemExit(0)
    report = review(json.loads(edl_path.read_text(encoding="utf-8")), json.loads(plan_path.read_text(encoding="utf-8")))
    report_path = (args.report or edl_path.parent / "editorial_review.json").resolve()
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"editorial review → {report_path} ({len(report['warnings'])} warning(s))")


if __name__ == "__main__":
    main()
