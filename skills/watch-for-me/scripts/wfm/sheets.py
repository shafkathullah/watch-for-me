"""Labelled contact sheets with Pillow (spec 4.4): grid choice, tile labels,
token estimate, tile map. SYNC; cli runs it via asyncio.to_thread under the CPU sem.

Port of scratchpad/ingest/frames.py `sheets` with the spec changes:
est_tokens = ceil(w/28) * ceil(h/28) (not w*h/750), font fallbacks, 2000 px clamp,
repeats excluded, per-aspect grids.

Also the --tldr "light visuals" helper (founder decision Q8): make_light_sheets builds
<= 2 extra sheets (sheets/light_NNN.jpg) from chapter-start tiles + the highest-novelty
tiles, so the main agent can read them itself without a V fan-out.
"""

from __future__ import annotations

import math
import os
from functools import cache
from pathlib import Path
from typing import Any

from .types import MAX_IMAGE_SIDE, Grid, Segment, Sheet, fmt_ts

GAP = 4
JPEG_Q = 80
TOKEN_PX = 28
LANDSCAPE_MIN_RATIO = 1.2  # w/h >= 1.2
PORTRAIT_MAX_RATIO = 0.8  # w/h <= 0.8
DEFAULT_LANDSCAPE_W = 1456  # env WFM_SHEET_WIDTH overrides the LANDSCAPE width only
PORTRAIT_W = 1092
SQUARE_W = 1064
MIN_SHEET_W = 256
LABEL_FG = "yellow"
LABEL_BG = "black"
LABEL_PAD = 4  # label text offset from the tile corner
LABEL_BOX_PAD = 3  # black box margin around the label text
MIN_FONT = 13
FONT_DIVISOR = 30
LIGHT_MAX_SHEETS = 2
LIGHT_NOVELTY_SHARE = 3  # >= 1/3 of light tiles are always highest-novelty picks
CHAPTER_SLACK_S = 10.0
FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",  # macOS
    "/Library/Fonts/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",  # Debian/Ubuntu
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",  # Fedora
    "DejaVuSans-Bold.ttf",
)


def sheet_width_env() -> int:
    """WFM_SHEET_WIDTH (int) or DEFAULT_LANDSCAPE_W, clamped to [256, types.MAX_IMAGE_SIDE]."""
    raw = os.environ.get("WFM_SHEET_WIDTH", "").strip()
    try:
        w = int(raw) if raw else DEFAULT_LANDSCAPE_W
    except ValueError:
        w = DEFAULT_LANDSCAPE_W
    return max(MIN_SHEET_W, min(MAX_IMAGE_SIDE, w))


def choose_grid(src_w: int, src_h: int, hires: bool) -> Grid:
    """Spec 4.4 table:
        landscape (w/h >= 1.2): 3x3 @ sheet_width_env()   | hires: 2x2 @ sheet_width_env()
        portrait  (w/h <= 0.8): 4x2 @ 1092 (4 cols, 2 rows)| hires: 3x1 @ 1092
        square-ish:             3x3 @ 1064                 | hires: 2x2 @ 1064
    Grid label "CxR" = cols x rows."""
    ratio = src_w / src_h if src_h > 0 else 16 / 9
    if ratio >= LANDSCAPE_MIN_RATIO:
        w = sheet_width_env()
        return Grid(2, 2, w) if hires else Grid(3, 3, w)
    if ratio <= PORTRAIT_MAX_RATIO:
        return Grid(3, 1, PORTRAIT_W) if hires else Grid(4, 2, PORTRAIT_W)
    return Grid(2, 2, SQUARE_W) if hires else Grid(3, 3, SQUARE_W)


def est_tokens(w: int, h: int) -> int:
    """ceil(w/28) * ceil(h/28); 1456x821 -> 1560."""
    return math.ceil(w / TOKEN_PX) * math.ceil(h / TOKEN_PX)


def tile_label(n: int, t0: float, t1: float) -> str:
    """"#n mm:ss-mm:ss" (types.fmt_ts per value, h:mm:ss at >= 1 h)."""
    return f"#{n} {fmt_ts(t0)}-{fmt_ts(t1)}"


@cache
def _font_path() -> str | None:
    """First FONT_CANDIDATES entry FreeType can open (resolved once per process)."""
    from PIL import ImageFont

    for cand in FONT_CANDIDATES:
        try:
            ImageFont.truetype(cand, MIN_FONT)
        except OSError:
            continue
        return cand
    return None


def load_font(size: int) -> Any:
    """First loadable FONT_CANDIDATES truetype at `size`, else ImageFont.load_default(size=size).
    A fresh font object per call: FreeType faces are not shared across the CPU threads."""
    from PIL import ImageFont

    path = _font_path()
    if path is not None:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1: bitmap default font, fixed size
        return ImageFont.load_default()


def tile_size(grid: Grid, src_w: int, src_h: int) -> tuple[int, int]:
    """tile_w = (grid.width - GAP*(cols-1)) // cols; tile_h = round(tile_w * src_h / src_w).
    Sheet height = rows*tile_h + GAP*(rows-1); if that would exceed MAX_IMAGE_SIDE,
    tile_w is reduced until it fits (the sheet then gets narrower than grid.width)."""
    src_w, src_h = max(1, src_w), max(1, src_h)
    tw = max(1, (min(grid.width, MAX_IMAGE_SIDE) - GAP * (grid.cols - 1)) // grid.cols)
    th = max(1, round(tw * src_h / src_w))
    while tw > 1 and grid.rows * th + GAP * (grid.rows - 1) > MAX_IMAGE_SIDE:
        tw -= 1
        th = max(1, round(tw * src_h / src_w))
    return tw, th


def _render(tiles: list[Segment], view_dir: Path, out: Path, grid: Grid, tw: int, th: int) -> tuple[int, int]:
    """One sheet: tiles in grid order, letterboxed into tw x th, labelled. Returns (w, h)."""
    from PIL import Image, ImageDraw, ImageOps

    rows = (len(tiles) + grid.cols - 1) // grid.cols
    cols = min(grid.cols, len(tiles)) if rows == 1 else grid.cols
    w = cols * tw + GAP * (cols - 1)
    if cols == grid.cols and 0 < grid.width - w < grid.cols:
        w = grid.width  # integer-division remainder (1454 -> 1456): pad right, keep the spec size
    h = rows * th + GAP * (rows - 1)
    sheet = Image.new("RGB", (w, h), LABEL_BG)
    draw = ImageDraw.Draw(sheet)
    font = load_font(max(MIN_FONT, tw // FONT_DIVISOR))
    for i, seg in enumerate(tiles):
        x, y = (i % grid.cols) * (tw + GAP), (i // grid.cols) * (th + GAP)
        with Image.open(view_dir / seg.file) as src:
            im = ImageOps.contain(src.convert("RGB"), (tw, th), Image.Resampling.LANCZOS)
        sheet.paste(im, (x + (tw - im.width) // 2, y + (th - im.height) // 2))
        label = tile_label(seg.n, seg.t0, seg.t1)
        pos = (x + LABEL_PAD, y + LABEL_PAD)
        bb = draw.textbbox(pos, label, font=font)
        draw.rectangle((bb[0] - LABEL_BOX_PAD, bb[1] - LABEL_BOX_PAD,
                        bb[2] + LABEL_BOX_PAD, bb[3] + LABEL_BOX_PAD), fill=LABEL_BG)
        draw.text(pos, label, fill=LABEL_FG, font=font)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.stem}.tmp{os.getpid()}.jpg")
    sheet.save(tmp, format="JPEG", quality=JPEG_Q)
    os.replace(tmp, out)
    return w, h


def _render_all(tiles: list[Segment], view_dir: Path, prefix: str, *, src_w: int, src_h: int,
                hires: bool) -> list[Sheet]:
    sdir = view_dir / "sheets"
    sdir.mkdir(parents=True, exist_ok=True)
    for p in sdir.glob(f"{prefix}_*.jpg"):
        p.unlink(missing_ok=True)
    if not tiles:
        return []
    grid = choose_grid(src_w, src_h, hires)
    tw, th = tile_size(grid, src_w, src_h)
    per = grid.cols * grid.rows
    out: list[Sheet] = []
    for i in range(0, len(tiles), per):
        chunk = tiles[i:i + per]
        n = i // per + 1
        rel = f"sheets/{prefix}_{n:03d}.jpg"
        w, h = _render(chunk, view_dir, view_dir / rel, grid, tw, th)
        out.append(Sheet(n=n, file=rel, grid=grid.label, w=w, h=h, est_tokens=est_tokens(w, h),
                         tiles=[s.n for s in chunk]))
    return out


def make_sheets(segments: list[Segment], view_dir: Path, *, src_w: int, src_h: int,
                hires: bool) -> list[Sheet]:
    """Render sheets for all NON-repeat segments (repeat_of is None) in order.

    Files: <view_dir>/sheets/sheet_NNN.jpg (1-based, 3 digits); stale sheet_*.jpg deleted
    first. Tiles read from <view_dir>/<segment.file>. Black background, GAP px gaps, JPEG q
    JPEG_Q; label per tile at (x+4, y+4) with a black box, font size max(13, tile_w // 30).
    A last partial sheet uses only the rows it needs (and only the columns it needs when it
    has a single row).
    Side effect: sets seg.sheet = sheet n on every placed segment (None on repeats).
    Returns Sheet list (file relative to view_dir, e.g. "sheets/sheet_001.jpg", grid label,
    real w/h, est_tokens, tiles = segment numbers).
    """
    placed = [s for s in segments if s.repeat_of is None]
    for s in segments:
        s.sheet = None
    sheets = _render_all(placed, Path(view_dir), "sheet", src_w=src_w, src_h=src_h, hires=hires)
    by_n = {s.n: s for s in placed}
    for sh in sheets:
        for n in sh.tiles:
            by_n[n].sheet = sh.n
    return sheets


# --------------------------------------------------------------------------
# --tldr light visuals (founder decision Q8)
# --------------------------------------------------------------------------
def _chapter_starts(chapters: list[Any] | None) -> list[float]:
    """yt-dlp chapters ([{"start_time":..}, ...]) or plain numbers -> sorted start times."""
    out: list[float] = []
    for c in chapters or []:
        v = c.get("start_time") if isinstance(c, dict) else c
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out.append(float(v))
    return sorted(set(out))


def _even_sample(items: list[Any], k: int) -> list[Any]:
    if k <= 0:
        return []
    if len(items) <= k:
        return list(items)
    if k == 1:
        return [items[0]]
    return [items[round(i * (len(items) - 1) / (k - 1))] for i in range(k)]


def select_light_tiles(segments: list[Segment], chapters: list[Any] | None, max_tiles: int) -> list[Segment]:
    """Pick <= max_tiles non-repeat segments for the light sheets, returned in time order.

    1. Chapter starts first: for each chapter start, the first segment starting within
       CHAPTER_SLACK_S after it (chapter marks usually precede the slide change by a few
       seconds), else the segment whose [t0, t1) contains it (a repeat resolves to its
       original). Chapters get at most max_tiles - max_tiles // LIGHT_NOVELTY_SHARE slots
       (18 -> 12), so novelty tiles always keep a share; more chapters -> evenly sampled.
    2. Fill the rest with the highest-novelty segments (ties: longer span, then earlier).
    The first segment has novelty 256, so the opening frame is always in.
    """
    placed = [s for s in segments if s.repeat_of is None]
    if max_tiles <= 0 or not placed:
        return []
    if len(placed) <= max_tiles:
        return placed
    by_n = {s.n: s for s in segments}
    chosen: dict[int, Segment] = {}
    chapter_hits: list[Segment] = []
    for start in _chapter_starts(chapters):
        hit = next((s for s in segments if start <= s.t0 < start + CHAPTER_SLACK_S), None)
        if hit is None:
            hit = next((s for s in segments if s.t0 <= start < s.t1), None)
        if hit is None:
            continue
        if hit.repeat_of is not None:
            hit = by_n.get(hit.repeat_of, hit)
        if hit.n not in {h.n for h in chapter_hits}:
            chapter_hits.append(hit)
    for s in _even_sample(chapter_hits, max_tiles - max_tiles // LIGHT_NOVELTY_SHARE):
        chosen[s.n] = s
    rest = sorted((s for s in placed if s.n not in chosen), key=lambda s: (-s.novelty, -(s.t1 - s.t0), s.n))
    for s in rest[: max_tiles - len(chosen)]:
        chosen[s.n] = s
    return sorted(chosen.values(), key=lambda s: s.n)


def make_light_sheets(segments: list[Segment], view_dir: Path, *, src_w: int, src_h: int, hires: bool,
                      chapters: list[Any] | None = None, max_sheets: int = LIGHT_MAX_SHEETS) -> list[Sheet]:
    """Render <= max_sheets "light" sheets (sheets/light_NNN.jpg) for --tldr: same grid,
    size and labels as make_sheets, tiles from select_light_tiles (max_sheets x tiles per
    sheet). Always rendered when there is at least one tile, so a consumer never needs a
    fallback (a short video's light sheets simply repeat its regular sheets). Does NOT
    touch seg.sheet. Returns Sheet entries for frames.json `light_sheets`."""
    grid = choose_grid(src_w, src_h, hires)
    tiles = select_light_tiles(segments, chapters, max_sheets * grid.cols * grid.rows)
    return _render_all(tiles, Path(view_dir), "light", src_w=src_w, src_h=src_h, hires=hires)
