#!/usr/bin/env python3
"""Generate the scrolling-code fixture for `--code` (evals/cases.md, "scrolling code").

A 1080p editor-like screen recording of two known source files (tests/fixtures/code_scroll/):
tab bar with the file name, line-number gutter, status bar. The timeline (TIMELINE below):
  a. inventory.py in page steps with pauses (one page is held for only 1.2 s)
  b. settings.toml, static
  c. inventory.py again, then a continuous scroll at reading speed to the end of the file
The sources are the ground truth: score the files an agent wrote with score_code_files.py.

Usage: uv run --with pillow tests/fixtures/make_code_scroll.py [OUT_DIR]
       default OUT_DIR: ${TMPDIR:-/tmp}/wfm-fixtures. Writes code_scroll.mp4 (line-number gutter),
       code_scroll_plain.mp4 (same recording without the gutter: the harder case, nothing on
       screen says which lines are missing) and code_scroll.json (timeline + per-second visible
       line ranges). No audio. Never writes into the repo.
Needs: Pillow, ffmpeg (libx264), a monospace font (Menlo or DejaVu Sans Mono).
The pure helpers (TIMELINE, visible_lines, coverage) import without Pillow or ffmpeg.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC_DIR = HERE / "code_scroll"
FILES = {"inventory.py": SRC_DIR / "inventory.py", "settings.toml": SRC_DIR / "settings.toml"}

W, H = 1920, 1080
FPS = 20
TAB_H, STATUS_H = 46, 30
LINE_H, FONT_PX = 30, 22
GUTTER_W, PAD_X = 96, 18
CODE_H = H - TAB_H - STATUS_H
VISIBLE = CODE_H // LINE_H  # whole lines in the code area (33)
SCROLL_LINES_PER_S = 2.75

# (seconds, file, top line at start, top line at end); top line is 1-based, a float while scrolling.
TIMELINE: list[tuple[float, str, float, float]] = [
    (5.0, "inventory.py", 1, 1),
    (4.0, "inventory.py", 31, 31),
    (1.2, "inventory.py", 61, 61),
    (4.8, "inventory.py", 91, 91),
    (8.0, "settings.toml", 1, 1),
    (2.0, "inventory.py", 91, 91),
    (80 / SCROLL_LINES_PER_S, "inventory.py", 91, 171),
    (4.0, "inventory.py", 171, 171),
]
DURATION = sum(step[0] for step in TIMELINE)

FONT_CANDIDATES = [
    ("/System/Library/Fonts/Menlo.ttc", 0),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 0),
    ("/usr/share/fonts/dejavu/DejaVuSansMono.ttf", 0),
    ("/usr/share/fonts/TTF/DejaVuSansMono.ttf", 0),
]
BG, GUTTER_FG, FG = (30, 30, 30), (110, 118, 129), (212, 212, 212)
TAB_BG, TAB_ACTIVE, STATUS_BG = (37, 37, 38), (30, 30, 30), (0, 102, 170)
COLORS = {"kw": (197, 134, 192), "str": (206, 145, 120), "com": (106, 153, 85), "num": (181, 206, 168)}
KEYWORDS = ["from", "import", "class", "def", "return", "if", "not", "or", "and", "raise", "for", "in",
            "with", "as", "try", "except", "continue", "None", "True", "False", "self", "cls", "lambda"]
TOKEN_RE = re.compile(
    r"(?P<com>#.*$)|(?P<str>f?\"\"\".*?\"\"\"|f?\"[^\"]*\"|'[^']*')|(?P<num>\b\d+(?:\.\d+)?\b)"
    r"|(?P<kw>\b(?:" + "|".join(KEYWORDS) + r")\b)")


# --------------------------------------------------------------------------
# Pure helpers (no Pillow / ffmpeg)
# --------------------------------------------------------------------------
def source_lines(name: str) -> list[str]:
    return FILES[name].read_text(encoding="utf-8").splitlines()


def state_at(t: float) -> tuple[str, float]:
    """(file name, top line as a float) on screen at time t."""
    t = min(max(t, 0.0), DURATION - 1e-6)
    start = 0.0
    for dur, name, a, b in TIMELINE:
        if t < start + dur:
            return name, a + (b - a) * (t - start) / dur
        start += dur
    dur, name, a, b = TIMELINE[-1]
    return name, b


def visible_lines(t: float, *, total: int | None = None) -> tuple[str, int, int]:
    """(file, first, last): the lines FULLY visible at time t (1-based, inclusive)."""
    name, top = state_at(t)
    n = total if total is not None else len(source_lines(name))
    first = int(top) if float(top).is_integer() else int(top) + 1
    last = int(top + VISIBLE - 1)  # floor: a line cut by the status bar does not count
    return name, first, min(last, n)


def coverage(times: list[float]) -> dict[str, list[int]]:
    """Lines of each file that are fully visible in none of the frames at `times`."""
    seen: dict[str, set[int]] = {name: set() for name in FILES}
    for t in times:
        name, a, b = visible_lines(t)
        seen[name].update(range(a, b + 1))
    return {name: [i for i in range(1, len(source_lines(name)) + 1) if i not in seen[name]]
            for name in FILES}


def line_ranges(nums: list[int]) -> list[tuple[int, int]]:
    """[1,2,3,7,8] -> [(1,3),(7,8)]."""
    out: list[tuple[int, int]] = []
    for n in nums:
        if out and n == out[-1][1] + 1:
            out[-1] = (out[-1][0], n)
        else:
            out.append((n, n))
    return out


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
def _font(px: int):
    from PIL import ImageFont

    for path, index in FONT_CANDIDATES:
        if os.path.isfile(path):
            return ImageFont.truetype(path, px, index=index)
    raise SystemExit("make_code_scroll: no monospace font found (Menlo / DejaVu Sans Mono)")


def _spans(line: str) -> list[tuple[str, tuple[int, int, int]]]:
    out: list[tuple[str, tuple[int, int, int]]] = []
    pos = 0
    for m in TOKEN_RE.finditer(line):
        if m.start() > pos:
            out.append((line[pos:m.start()], FG))
        out.append((m.group(), COLORS[m.lastgroup or "kw"]))
        pos = m.end()
    if pos < len(line):
        out.append((line[pos:], FG))
    return out


def render_strip(name: str, gutter: bool = True):
    """The whole file as one tall image (gutter + highlighted code), one LINE_H row per line."""
    from PIL import Image, ImageDraw

    lines = source_lines(name)
    font = _font(FONT_PX)
    cw = font.getlength("M")
    img = Image.new("RGB", (W, LINE_H * (len(lines) + VISIBLE)), BG)
    d = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        y = i * LINE_H + (LINE_H - FONT_PX) // 2 - 2
        num = str(i + 1)
        if gutter:
            d.text((GUTTER_W - PAD_X - cw * len(num), y), num, font=font, fill=GUTTER_FG)
        x = float(GUTTER_W + PAD_X if gutter else 2 * PAD_X)
        for text, color in _spans(line):
            d.text((x, y), text, font=font, fill=color)
            x += cw * len(text)
    return img


def render_chrome(name: str):
    """Tab bar (active tab = file name, the other file as an inactive tab) + status bar."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    font = _font(18)
    d.rectangle((0, 0, W, TAB_H), fill=TAB_BG)
    x = 0
    for tab in FILES:
        tw = int(font.getlength(tab)) + 56
        if tab == name:
            d.rectangle((x, 0, x + tw, TAB_H), fill=TAB_ACTIVE)
            d.rectangle((x, 0, x + tw, 2), fill=STATUS_BG)
        d.text((x + 28, 13), tab, font=font, fill=FG if tab == name else GUTTER_FG)
        x += tw + 1
    d.rectangle((0, H - STATUS_H, W, H), fill=STATUS_BG)
    lang = "Python" if name.endswith(".py") else "TOML"
    d.text((18, H - STATUS_H + 5), f"main    {name}    UTF-8    LF    {lang}", font=font, fill=(255, 255, 255))
    return img


def write_video(out: Path, gutter: bool = True) -> None:
    strips = {name: render_strip(name, gutter) for name in FILES}
    chromes = {name: render_chrome(name) for name in FILES}
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
           "-r", str(FPS), "-i", "-", "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
           "-pix_fmt", "yuv420p", "-g", "250", "-movflags", "+faststart", str(out)]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    assert p.stdin is not None
    last_key: tuple[str, int] | None = None
    buf = b""
    try:
        for i in range(round(DURATION * FPS)):
            name, top = state_at(i / FPS)
            y = round((top - 1) * LINE_H)
            if (name, y) != last_key:
                frame = chromes[name].copy()
                frame.paste(strips[name].crop((0, y, W, y + CODE_H)), (0, TAB_H))
                buf = frame.tobytes()
                last_key = (name, y)
            p.stdin.write(buf)
    finally:
        p.stdin.close()
        rc = p.wait()
    if rc != 0:
        raise SystemExit(f"make_code_scroll: ffmpeg exited {rc}")


def main(argv: list[str]) -> int:
    out_dir = Path(argv[1] if len(argv) > 1 else Path(os.environ.get("TMPDIR", "/tmp")) / "wfm-fixtures")
    out_dir = out_dir.expanduser().resolve()
    repo = HERE.parents[1]
    top = subprocess.run(["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True, check=False).stdout.strip()
    guard = Path(top).resolve() if top else repo
    if out_dir == guard or guard in out_dir.parents:
        raise SystemExit(f"make_code_scroll: refusing to write media inside the repo ({out_dir}); pass a temp dir")
    out_dir.mkdir(parents=True, exist_ok=True)
    video = out_dir / "code_scroll.mp4"
    plain = out_dir / "code_scroll_plain.mp4"
    write_video(video, gutter=True)
    write_video(plain, gutter=False)
    info = {
        "files": [video.name, plain.name], "duration": round(DURATION, 3), "fps": FPS, "visible_lines": VISIBLE,
        "sources": {name: {"path": str(path), "lines": len(source_lines(name))} for name, path in FILES.items()},
        "timeline": [{"seconds": round(d, 3), "file": n, "top_from": a, "top_to": b} for d, n, a, b in TIMELINE],
        "visible_per_second": [[s, *visible_lines(float(s))] for s in range(int(DURATION) + 1)],
    }
    (out_dir / "code_scroll.json").write_text(json.dumps(info, indent=1) + "\n", encoding="utf-8")
    print(video)
    print(plain)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
