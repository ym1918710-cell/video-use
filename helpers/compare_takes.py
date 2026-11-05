"""Find repeated lines across takes and rank them, so the best take is chosen
on evidence: completeness, fillers, hesitations, and pace.

Reads ``<edit>/transcripts/*.json`` (word-level) and writes
``take_comparison.md`` + ``take_comparison.json``.
"""

from __future__ import annotations

import argparse
import json
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

FILLERS = {"um", "uh", "erm", "ah", "eh", "like", "اه", "آه", "ام", "امم", "اممم", "ايه", "يعني", "ممم", "هممم"}
DIACRITICS = re.compile(r"[ً-ْٰـ]")


def normalize(word: str) -> str:
    word = DIACRITICS.sub("", word.lower())
    word = re.sub(r"[أإآ]", "ا", word).replace("ى", "ي").replace("ة", "ه")
    return re.sub(r"[^\w]", "", word)


def phrases(transcript: dict[str, Any], source: str, gap: float = 0.5) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    for word in transcript.get("words", []):
        if word.get("type", "word") != "word" or word.get("start") is None:
            continue
        if current and (float(word["start"]) - float(current[-1]["end"]) >= gap
                        or str(current[-1]["text"]).rstrip().endswith((".", "؟", "?", "!"))):
            out.append(current)
            current = []
        current.append(word)
    if current:
        out.append(current)
    result = []
    for words in out:
        tokens = [normalize(str(w["text"])) for w in words]
        result.append({
            "source": source, "start": float(words[0]["start"]), "end": float(words[-1]["end"]),
            "text": " ".join(str(w["text"]) for w in words), "tokens": [t for t in tokens if t],
            "fillers": sum(t in FILLERS for t in tokens),
            "hesitations": sum(1 for a, b in zip(words, words[1:]) if float(b["start"]) - float(a["end"]) > 0.35),
        })
    return result


def similar(a: list[str], b: list[str]) -> float:
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def group_takes(items: list[dict[str, Any]], threshold: float = 0.6, min_words: int = 4) -> list[list[dict[str, Any]]]:
    candidates = [p for p in items if len(p["tokens"]) >= min_words]
    parent = list(range(len(candidates)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(candidates)):
        for j in range(i + 1, len(candidates)):
            if similar(candidates[i]["tokens"], candidates[j]["tokens"]) >= threshold:
                parent[find(i)] = find(j)
    groups: dict[int, list[dict[str, Any]]] = {}
    for i, item in enumerate(candidates):
        groups.setdefault(find(i), []).append(item)
    return sorted((g for g in groups.values() if len(g) > 1), key=lambda g: min(p["start"] for p in g))


def score(take: dict[str, Any], longest: int) -> float:
    completeness = len(take["tokens"]) / max(1, longest)
    duration = max(0.1, take["end"] - take["start"])
    pace = len(take["tokens"]) / duration  # words per second
    pace_penalty = abs(pace - 2.6) * 0.15
    return round(completeness * 3 - take["fillers"] * 0.6 - take["hesitations"] * 0.4 - pace_penalty, 3)


def compare(edit_dir: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for path in sorted((edit_dir / "transcripts").glob("*.json")):
        items += phrases(json.loads(path.read_text(encoding="utf-8")), path.stem)
    report = []
    for group in group_takes(items):
        longest = max(len(p["tokens"]) for p in group)
        ranked = sorted(({**p, "score": score(p, longest)} for p in group), key=lambda p: -p["score"])
        for p in ranked:
            p.pop("tokens", None)
        report.append({"line": ranked[0]["text"], "takes": ranked})
    return report


def to_markdown(report: list[dict[str, Any]]) -> str:
    lines = ["# Take comparison", ""]
    for index, group in enumerate(report, start=1):
        lines.append(f"## {index}. {group['line']}")
        lines.append("| rank | source | range | fillers | hesitations | score | text |")
        lines.append("|---|---|---|---|---|---|---|")
        for rank, t in enumerate(group["takes"], start=1):
            lines.append(f"| {rank} | {t['source']} | {t['start']:.2f}-{t['end']:.2f} | {t['fillers']} | "
                         f"{t['hesitations']} | {t['score']} | {t['text']} |")
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Rank repeated takes across transcripts")
    parser.add_argument("--edit-dir", type=Path, required=True)
    args = parser.parse_args()
    edit_dir = args.edit_dir.resolve()
    report = compare(edit_dir)
    (edit_dir / "take_comparison.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (edit_dir / "take_comparison.md").write_text(to_markdown(report), encoding="utf-8")
    print(f"{len(report)} repeated line(s) → {edit_dir / 'take_comparison.md'}")


if __name__ == "__main__":
    main()
