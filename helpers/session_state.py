"""Create and maintain the durable state files used by Video Use.

The agent can still reason freely, but it records the brief, creator style, and
current lifecycle state as structured data.  Those files live beside the edit,
never beside the source footage or the Video Use skill.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


STATE_VERSION = 1


def _write_if_missing(path: Path, value: dict) -> bool:
    if path.exists():
        return False
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


def initialize(edit_dir: Path) -> list[Path]:
    """Create empty, explicit session artifacts without overwriting preferences."""
    edit_dir.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    now = datetime.now(timezone.utc).isoformat()
    templates = {
        "edit_brief.json": {
            "version": STATE_VERSION,
            "status": "intake",
            "created_at": now,
            "goal": "",
            "target": {"platform": None, "aspect_ratio": None, "duration_s": None},
            "language": None,
            "must_keep": [],
            "must_avoid": [],
            "approval": {"strategy_approved": False, "approved_at": None},
        },
        "style_profile.json": {
            "version": STATE_VERSION,
            "base_editorial_memory": "video-use/references/editorial-memory.md",
            "style": {"recipe": None, "adaptations": [], "alternatives_considered": []},
            "brand_kit": "brand_kit.json",
            "caption": {"language": None, "font": None, "style": None, "safe_area": "auto"},
            "pacing": {"pace": None, "cut_padding_ms": None},
            "visual": {"grade": None, "punch_in": "restrained"},
            "audio": {"loudness_target_lufs": -14},
        },
        "brand_kit.json": {
            "version": STATE_VERSION,
            "client": "",
            "personality": [],
            "colors": {},
            "fonts": {},
            "logo": {},
            "caption_skin": {},
            "audio": {"loudness_lufs": -14, "true_peak_dbtp": -1},
            "preferred_recipes": [],
            "do": [],
            "dont": [],
            "references": [],
        },
        "editorial_plan.json": {
            "version": STATE_VERSION,
            "status": "draft",
            "protected_moments": [],
            "punch_ins": [],
            "b_roll": [],
            "sound_effects": [],
            "decisions": [],
        },
        "session_state.json": {
            "version": STATE_VERSION,
            "status": "intake",
            "created_at": now,
            "last_updated_at": now,
            "latest_edl": None,
            "latest_preview": None,
            "latest_qc_report": None,
            "self_eval_passes": 0,
        },
    }
    for name, value in templates.items():
        path = edit_dir / name
        if _write_if_missing(path, value):
            created.append(path)
    return created


def update(edit_dir: Path, **changes: object) -> dict:
    """Update lifecycle facts while preserving user-authored brief/style files."""
    initialize(edit_dir)
    state_path = edit_dir / "session_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update(changes)
    state["last_updated_at"] = datetime.now(timezone.utc).isoformat()
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return state


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize Video Use structured session state")
    parser.add_argument("edit_dir", type=Path, help="The session's edit directory")
    args = parser.parse_args()
    created = initialize(args.edit_dir.resolve())
    if created:
        print("created:")
        for path in created:
            print(f"  {path}")
    else:
        print("session state already exists; nothing overwritten")


if __name__ == "__main__":
    main()
