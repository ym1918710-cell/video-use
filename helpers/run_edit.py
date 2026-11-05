"""The preferred Video Use agent entry point: initialize, validate, render, QC."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from session_state import initialize, update
from validate_edl import validate_edl
from review_editorial_plan import review


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a guarded Video Use edit")
    parser.add_argument("edl", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--draft", action="store_true")
    parser.add_argument("--build-subtitles", action="store_true")
    parser.add_argument("--no-subtitles", action="store_true")
    parser.add_argument("--no-loudnorm", action="store_true")
    parser.add_argument("--fps")
    parser.add_argument("--visual-qc", action="store_true", help="Generate cut-boundary filmstrips for agent review")
    args = parser.parse_args()

    edl = args.edl.resolve()
    initialize(edl.parent)
    plan_path = edl.parent / "editorial_plan.json"
    if plan_path.exists():
        editorial = review(
            json.loads(edl.read_text(encoding="utf-8")),
            json.loads(plan_path.read_text(encoding="utf-8")),
        )
        review_path = edl.parent / "editorial_review.json"
        review_path.write_text(json.dumps(editorial, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if editorial["warnings"]:
            print("Editorial review has warnings; inspect editorial_review.json before rendering.", file=sys.stderr)
    validation = validate_edl(edl)
    if not validation["valid"]:
        update(edl.parent, status="validation_failed", latest_edl=str(edl))
        print("EDL validation failed; render was not started.", file=sys.stderr)
        raise SystemExit(1)
    update(edl.parent, status="rendering", latest_edl=str(edl))

    helper_dir = Path(__file__).parent
    render_command = [sys.executable, str(helper_dir / "render.py"), str(edl), "-o", str(args.output.resolve())]
    for flag, enabled in (("--preview", args.preview), ("--draft", args.draft), ("--build-subtitles", args.build_subtitles), ("--no-subtitles", args.no_subtitles), ("--no-loudnorm", args.no_loudnorm)):
        if enabled:
            render_command.append(flag)
    if args.fps:
        render_command.extend(["--fps", args.fps])
    subprocess.run(render_command, check=True)
    update(edl.parent, status="qc_pending", latest_preview=str(args.output.resolve()))

    qc_command = [sys.executable, str(helper_dir / "verify_render.py"), str(edl), str(args.output.resolve())]
    if args.visual_qc:
        qc_command.append("--visual")
    subprocess.run(qc_command, check=True)
    update(edl.parent, status="qc_passed", latest_qc_report=str(edl.parent / "qc_report.json"))


if __name__ == "__main__":
    main()
