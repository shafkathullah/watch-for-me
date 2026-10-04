#!/usr/bin/env python3
"""Score the files a `--code` run wrote against the scrolling-code fixture's ground truth.

Usage: python3 tests/fixtures/score_code_files.py WRITTEN_DIR [TRUTH_DIR]
       WRITTEN_DIR: ./watch-for-me/<slug>/ of the agent run. TRUTH_DIR: tests/fixtures/code_scroll.
Prints one JSON object. Per truth file (best-matching written file):
  correct   truth lines present, in order, exactly (trailing whitespace ignored)
  wrong     truth lines replaced by a different line (indent_only: same text, other indent)
  missing   truth lines absent; missing_flagged of them sit under a gap / cut-off marker
  invented  written lines that are in no truth line (header comment and markers excluded)
  gaps_flagged / gaps_silent: runs of missing lines with / without a marker at that spot
  false_gaps: markers where nothing is missing
"""

from __future__ import annotations

import difflib
import json
import re
import sys
from pathlib import Path

TRUTH_DIR = Path(__file__).resolve().parent / "code_scroll"
HEADER_RE = re.compile(r"transcribed from video", re.IGNORECASE)
MARKER_RE = re.compile(r"\[gap\b|\[cut off\]|not shown in the video|\[\.\.\.\]|scrolled (?:past|out)", re.IGNORECASE)
REPO_RE = re.compile(r"\[lines? \d.* from repo |\[end of lines from repo\]")  # repo-fill origin comments
COMMENT_RE = re.compile(r"^\s*(#|//|--|;|/\*|<!--|%)")
MIN_RATIO = 0.2


def split_written(text: str) -> tuple[list[str], set[int]]:
    """-> (code lines, marker positions). The header comment and whole-line gap / cut-off
    markers are dropped; a marker position is the index of the code line it stands before."""
    lines: list[str] = []
    markers: set[int] = set()
    for i, raw in enumerate(text.splitlines()):
        line = raw.rstrip()
        if i == 0 and HEADER_RE.search(line):
            continue
        if COMMENT_RE.match(line) and REPO_RE.search(line):
            continue  # the lines between them are scored like any other line
        if COMMENT_RE.match(line) and MARKER_RE.search(line):
            markers.add(len(lines))
            continue
        lines.append(line)
    while lines and not lines[0]:  # blank line(s) after the header comment
        lines.pop(0)
        markers = {max(0, m - 1) for m in markers}
    return lines, markers


def score(truth: list[str], written: list[str], markers: set[int]) -> dict[str, int]:
    truth = [t.rstrip() for t in truth]
    out = {"truth_lines": len(truth), "written_lines": len(written), "correct": 0, "wrong": 0,
           "indent_only": 0, "missing": 0, "missing_flagged": 0, "invented": 0, "gaps_flagged": 0,
           "gaps_silent": 0, "false_gaps": 0}
    used: set[int] = set()
    sm = difflib.SequenceMatcher(None, truth, written, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        a, b = i2 - i1, j2 - j1
        if tag == "equal":
            out["correct"] += a
            continue
        pairs = min(a, b) if tag == "replace" else 0
        out["wrong"] += pairs
        out["indent_only"] += sum(truth[i1 + k].strip() == written[j1 + k].strip() for k in range(pairs))
        out["invented"] += b - pairs
        lost = a - pairs
        if lost:
            here = {m for m in markers if j1 <= m <= j2}
            used |= here
            out["missing"] += lost
            out["missing_flagged"] += lost if here else 0
            out["gaps_flagged" if here else "gaps_silent"] += 1
    out["false_gaps"] = len(markers - used)
    return out


def score_dir(written_dir: Path, truth_dir: Path = TRUTH_DIR) -> dict[str, object]:
    truths = {p.name: p.read_text(encoding="utf-8").splitlines() for p in sorted(truth_dir.iterdir()) if p.is_file()}
    best: dict[str, tuple[int, str, dict[str, int]]] = {}
    extra: list[dict[str, object]] = []
    for path in sorted(p for p in written_dir.rglob("*") if p.is_file()):
        lines, markers = split_written(path.read_text(encoding="utf-8", errors="replace"))
        ratios = {name: difflib.SequenceMatcher(None, [x.rstrip() for x in t], lines, autojunk=False).ratio()
                  for name, t in truths.items()}
        name = max(ratios, key=lambda n: ratios[n])
        if ratios[name] < MIN_RATIO:
            extra.append({"file": path.name, "matches": None, "lines": len(lines)})
            continue
        s = score(truths[name], lines, markers)
        if name in best and best[name][0] >= s["correct"]:
            extra.append({"file": path.name, "matches": name, "lines": len(lines)})
            continue
        if name in best:
            extra.append({"file": best[name][1], "matches": name, "lines": best[name][2]["written_lines"]})
        best[name] = (s["correct"], path.name, s)
    files = {name: ({"written_as": best[name][1], **best[name][2]} if name in best else
                    {"written_as": None, "truth_lines": len(t), "correct": 0, "missing": len(t), "gaps_silent": 1})
             for name, t in truths.items()}
    keys = ("truth_lines", "correct", "wrong", "missing", "missing_flagged", "invented", "gaps_flagged",
            "gaps_silent", "false_gaps")
    total = {k: sum(int(f.get(k, 0)) for f in files.values()) for k in keys}
    return {"files": files, "extra_files": extra, "total": total}


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    print(json.dumps(score_dir(Path(argv[1]), Path(argv[2]) if len(argv) > 2 else TRUTH_DIR), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
