"""wfm.sheets: grid choice, token estimate, labels, tile sizes, rendering, repeats excluded,
and the --tldr light-sheet selection. Pillow only; tiles are generated into tmp_path."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image
from wfm import sheets
from wfm.types import MAX_IMAGE_SIDE, Grid, Segment


# --------------------------------------------------------------------------
# pure helpers
# --------------------------------------------------------------------------
def test_est_tokens() -> None:
    assert sheets.est_tokens(1456, 821) == 1560
    assert sheets.est_tokens(1092, 964) == 1365
    assert sheets.est_tokens(1920, 1080) == 2691
    assert sheets.est_tokens(28, 28) == 1 and sheets.est_tokens(29, 1) == 2


def test_choose_grid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WFM_SHEET_WIDTH", raising=False)
    assert sheets.choose_grid(1280, 720, False) == Grid(3, 3, 1456)
    assert sheets.choose_grid(1280, 720, True) == Grid(2, 2, 1456)
    assert sheets.choose_grid(720, 1280, False) == Grid(4, 2, 1092)
    assert sheets.choose_grid(720, 1280, True) == Grid(3, 1, 1092)
    assert sheets.choose_grid(720, 900, False) == Grid(4, 2, 1092)  # 0.8 is portrait
    assert sheets.choose_grid(1080, 1080, False) == Grid(3, 3, 1064)
    assert sheets.choose_grid(1080, 1080, True) == Grid(2, 2, 1064)
    assert sheets.choose_grid(1200, 1000, False) == Grid(3, 3, 1456)  # 1.2 exactly is landscape
    assert sheets.choose_grid(1200, 1001, False) == Grid(3, 3, 1064)  # just below: square-ish
    assert Grid(4, 2, 1092).label == "4x2"


def test_sheet_width_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WFM_SHEET_WIDTH", "1800")
    assert sheets.sheet_width_env() == 1800
    assert sheets.choose_grid(1920, 1080, False).width == 1800
    assert sheets.choose_grid(720, 1280, False).width == 1092  # env is landscape-only
    monkeypatch.setenv("WFM_SHEET_WIDTH", "4000")
    assert sheets.sheet_width_env() == MAX_IMAGE_SIDE
    monkeypatch.setenv("WFM_SHEET_WIDTH", "12")
    assert sheets.sheet_width_env() == 256
    monkeypatch.setenv("WFM_SHEET_WIDTH", "wide")
    assert sheets.sheet_width_env() == 1456


def test_tile_label() -> None:
    assert sheets.tile_label(1, 0, 12.4) == "#1 00:00-00:12"
    assert sheets.tile_label(73, 3540.2, 3601) == "#73 59:00-1:00:01"


def test_tile_size() -> None:
    assert sheets.tile_size(Grid(3, 3, 1456), 1280, 720) == (482, 271)  # 3*271+8 = 821
    assert sheets.tile_size(Grid(4, 2, 1092), 720, 1280) == (270, 480)  # sheet h 964
    assert sheets.tile_size(Grid(2, 2, 1456), 1920, 1080) == (726, 408)
    # extreme portrait: height clamp shrinks tiles so the sheet stays <= 2000 px
    tw, th = sheets.tile_size(Grid(3, 3, 1064), 400, 2000)
    assert 3 * th + 8 <= MAX_IMAGE_SIDE and tw < (1064 - 8) // 3


def test_load_font() -> None:
    f = sheets.load_font(16)
    assert f.getbbox("#1 00:00-00:12")[2] > 0


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------
def _segments(view_dir: Path, n: int, *, size: tuple[int, int] = (1280, 720),
              repeats: dict[int, int] | None = None) -> list[Segment]:
    fdir = view_dir / "frames"
    fdir.mkdir(parents=True, exist_ok=True)
    segs = []
    for i in range(1, n + 1):
        t0 = (i - 1) * 10.0
        name = f"f_{round((t0 + 9) * 1000):09d}.jpg"
        Image.new("RGB", size, ((i * 40) % 256, (i * 90) % 256, (i * 20) % 256)).save(fdir / name)
        segs.append(Segment(n=i, t0=t0, t1=t0 + 10, t=t0 + 9, file=f"frames/{name}", novelty=10 + i,
                            repeat_of=(repeats or {}).get(i)))
    return segs


def test_make_sheets_landscape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WFM_SHEET_WIDTH", raising=False)
    segs = _segments(tmp_path, 12, repeats={5: 1})
    (tmp_path / "sheets").mkdir()
    (tmp_path / "sheets" / "sheet_009.jpg").write_bytes(b"stale")
    out = sheets.make_sheets(segs, tmp_path, src_w=1280, src_h=720, hires=False)
    assert [s.n for s in out] == [1, 2]
    assert out[0].file == "sheets/sheet_001.jpg" and out[0].grid == "3x3"
    assert (out[0].w, out[0].h, out[0].est_tokens) == (1456, 821, 1560)
    assert out[0].tiles == [1, 2, 3, 4, 6, 7, 8, 9, 10]  # repeat #5 not placed
    assert out[1].tiles == [11, 12]
    assert (out[1].w, out[1].h) == (482 * 2 + 4, 271)  # partial single row: only what it needs
    assert segs[4].sheet is None and segs[5].sheet == 1 and segs[11].sheet == 2
    files = sorted(p.name for p in (tmp_path / "sheets").iterdir())
    assert files == ["sheet_001.jpg", "sheet_002.jpg"]
    with Image.open(tmp_path / out[0].file) as im:
        assert im.size == (1456, 821)
        assert sum(im.getpixel((1455, 400))) < 30  # 2 px right pad is black
        # label box: yellow text pixels in the top-left corner of tile 1
        crop = im.crop((4, 4, 120, 24)).convert("RGB")
        raw = crop.tobytes()
        px = [tuple(raw[i:i + 3]) for i in range(0, len(raw), 3)]
        assert any(r > 180 and g > 180 and b < 90 for r, g, b in px)


def test_make_sheets_partial_rows_and_portrait(tmp_path: Path) -> None:
    segs = _segments(tmp_path, 5, size=(720, 1280))
    out = sheets.make_sheets(segs, tmp_path, src_w=720, src_h=1280, hires=False)
    assert len(out) == 1 and out[0].grid == "4x2"
    assert (out[0].w, out[0].h) == (1092, 2 * 480 + 4)
    hires = sheets.make_sheets(segs, tmp_path, src_w=720, src_h=1280, hires=True)
    assert [s.tiles for s in hires] == [[1, 2, 3], [4, 5]] and hires[0].grid == "3x1"
    assert all(s.w <= MAX_IMAGE_SIDE and s.h <= MAX_IMAGE_SIDE for s in hires)
    assert sorted(p.name for p in (tmp_path / "sheets").iterdir()) == ["sheet_001.jpg", "sheet_002.jpg"]


def test_make_sheets_mixed_tile_sizes_letterboxed(tmp_path: Path) -> None:
    segs = _segments(tmp_path, 2)
    Image.new("RGB", (640, 640), "white").save(tmp_path / segs[1].file)  # resolution change mid-video
    out = sheets.make_sheets(segs, tmp_path, src_w=1280, src_h=720, hires=True)
    assert (out[0].w, out[0].h) == (726 * 2 + 4, 408)


def test_make_sheets_empty(tmp_path: Path) -> None:
    assert sheets.make_sheets([], tmp_path, src_w=1280, src_h=720, hires=False) == []


# --------------------------------------------------------------------------
# --tldr light visuals
# --------------------------------------------------------------------------
def _plain(n: int) -> list[Segment]:
    return [Segment(n=i, t0=(i - 1) * 60.0, t1=i * 60.0, t=i * 60.0 - 1, file="", novelty=(i * 37) % 100 + 1)
            for i in range(1, n + 1)]


def test_select_light_tiles_small_video_keeps_all() -> None:
    segs = _plain(6)
    segs[3].repeat_of = 1
    assert [s.n for s in sheets.select_light_tiles(segs, None, 18)] == [1, 2, 3, 5, 6]


def test_select_light_tiles_novelty_and_order() -> None:
    segs = _plain(40)
    segs[0].novelty = 256
    got = sheets.select_light_tiles(segs, None, 10)
    assert len(got) == 10 and got[0].n == 1
    assert [s.n for s in got] == sorted(s.n for s in got)
    top = sorted((s for s in segs[1:]), key=lambda s: (-s.novelty, -(s.t1 - s.t0), s.n))[:9]
    assert {s.n for s in got[1:]} == {s.n for s in top}


def test_select_light_tiles_chapters_first() -> None:
    segs = _plain(40)
    for s in segs:
        s.novelty = 200  # all equal: only chapters decide besides the tie-breaks
    segs[29].repeat_of = 3  # chapter landing on a repeat resolves to its original
    chapters = [{"start_time": 0.0}, {"start_time": 595.0, "title": "x"}, {"start_time": 1745.0},
                {"start_time": 2000.5}]
    got = [s.n for s in sheets.select_light_tiles(segs, chapters, 6)]
    # 6 slots = 4 chapter slots + 2 novelty. 595 -> seg 11 starts at 600 (within 10 s slack);
    # 1745 -> seg 30 (t0 1740) is a repeat of 3; 2000.5 -> no seg starts within 10 s, contained
    # in seg 34 (1980-2040). Novelty ties (all 200, equal spans) -> earliest: 2, 4.
    assert got == [1, 2, 3, 4, 11, 34]


def test_select_light_tiles_many_chapters_sampled() -> None:
    segs = _plain(40)
    for s in segs:
        s.novelty = 1
    segs[16].novelty = 99  # seg 17: the single highest-novelty pick
    chapters = [float(i * 60) for i in range(40)]
    got = sheets.select_light_tiles(segs, chapters, 5)
    # 5 slots = 4 chapter slots (evenly sampled from 40 hits) + 1 novelty slot
    assert [s.n for s in got] == [1, 14, 17, 27, 40]


def test_make_light_sheets(tmp_path: Path) -> None:
    segs = _segments(tmp_path, 30, repeats={7: 2})
    reg = sheets.make_sheets(segs, tmp_path, src_w=1280, src_h=720, hires=False)
    before = [s.sheet for s in segs]
    light = sheets.make_light_sheets(segs, tmp_path, src_w=1280, src_h=720, hires=False,
                                     chapters=[{"start_time": 100.0}])
    assert len(light) == 2 and [s.file for s in light] == ["sheets/light_001.jpg", "sheets/light_002.jpg"]
    tiles = [n for s in light for n in s.tiles]
    assert len(tiles) == 18 and 7 not in tiles and 11 in tiles and tiles == sorted(tiles)
    assert [s.sheet for s in segs] == before  # light sheets never touch seg.sheet
    assert (tmp_path / reg[0].file).is_file()  # regular sheets untouched
    one = sheets.make_light_sheets(segs[:3], tmp_path, src_w=1280, src_h=720, hires=True, max_sheets=1)
    assert len(one) == 1 and one[0].tiles == [1, 2, 3]
    assert not (tmp_path / "sheets" / "light_002.jpg").exists()  # stale light sheets removed


def test_load_font_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sheets, "FONT_CANDIDATES", ("/nonexistent/Arial Bold.ttf",))
    sheets._font_path.cache_clear()
    try:
        f = sheets.load_font(20)  # ImageFont.load_default(size=20)
        assert f.getbbox("#1 00:00-00:12")[3] >= 12
    finally:
        sheets._font_path.cache_clear()
