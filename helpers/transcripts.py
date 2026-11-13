"""Find the cached transcript for an EDL source id.

Transcription caches by *file stem* (``transcripts/C0103.json``), while an
EDL may give a source any short id (``"T1": ".../IMG_0829.MOV"``). Every
consumer resolves through here so captions, word anchors, validation, and
QC all find the same file.
"""

from __future__ import annotations

from pathlib import Path


def transcript_path(edit_dir: Path, edl: dict, source_id: str) -> Path:
    folder = edit_dir / "transcripts"
    direct = folder / f"{source_id}.json"
    if direct.exists():
        return direct
    raw = (edl.get("sources") or {}).get(source_id)
    if isinstance(raw, str):
        by_stem = folder / f"{Path(raw).stem}.json"
        if by_stem.exists():
            return by_stem
    return direct
