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
    parser.add_argument("--deliverables", action="store_true",
                        help="Also render every EDL `deliverables` version (other canvases / framings)")
    parser.add_argument("--first-cut", action="store_true",
                        help="Approval stage: cuts, framing, B-roll, transitions and dialogue only — "
                             "no graphics, captions, overlays, music or SFX")
    args = parser.parse_args()

    edl = args.edl.resolve()
    if args.first_cut:
        edl = write_first_cut_edl(edl)
        print(f"first cut → {edl.name} (finishing layers removed)")
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

    if args.deliverables:
        for variant_path, output in write_deliverable_edls(edl, args.output.resolve()):
            print(f"deliverable → {output.name}")
            command = [sys.executable, str(helper_dir / "render.py"), str(variant_path), "-o", str(output)]
            command += [flag for flag, on in (("--preview", args.preview), ("--draft", args.draft),
                                              ("--no-subtitles", args.no_subtitles), ("--no-loudnorm", args.no_loudnorm)) if on]
            if args.fps:
                command += ["--fps", args.fps]
            subprocess.run(command, check=True)
            subprocess.run([sys.executable, str(helper_dir / "verify_render.py"), str(variant_path), str(output),
                            "--report", str(variant_path.with_name(f"qc_report_{variant_path.stem}.json"))], check=True)


FINISHING_KEYS = ("graphics", "captions", "subtitles", "overlays", "behind_subject_overlays", "subject_mask", "deliverables")


def write_first_cut_edl(edl_path: Path) -> Path:
    """The cut for approval: the story and pictures, nothing that hides them."""
    edl = json.loads(edl_path.read_text(encoding="utf-8"))
    cut = {k: v for k, v in edl.items() if k not in FINISHING_KEYS}
    audio = dict(cut.get("audio") or {})
    audio.pop("music", None)
    audio.pop("sfx", None)
    audio["auto_sfx"] = "first_cut"  # picture and dialogue only; sound comes with finishing
    if audio:
        cut["audio"] = audio
    else:
        cut.pop("audio", None)
    path = edl_path.with_name(f"{edl_path.stem}_first_cut.json")
    path.write_text(json.dumps(cut, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def write_deliverable_edls(edl_path: Path, output: Path) -> list[tuple[Path, Path]]:
    """One EDL per deliverable: same cut, its own canvas / framing / captions."""
    edl = json.loads(edl_path.read_text(encoding="utf-8"))
    pairs = []
    for item in edl.get("deliverables") or []:
        name = str(item["name"])
        variant = {k: v for k, v in edl.items() if k != "deliverables"}
        if item.get("canvas"):
            variant["canvas"] = item["canvas"]
        if item.get("source_framing"):
            variant["source_framing"] = {**(edl.get("source_framing") or {}), **item["source_framing"]}
        if "captions" in item:
            variant["captions"] = item["captions"]
        variant_path = edl_path.with_name(f"{edl_path.stem}_{name}.json")
        variant_path.write_text(json.dumps(variant, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        pairs.append((variant_path, output.with_name(f"{output.stem}_{name}{output.suffix}")))
    return pairs


if __name__ == "__main__":
    main()
