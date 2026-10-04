"""wfm.codefiles: CODE block parsing and the deterministic stitcher behind `--code`, checked
against the scrolling-code fixture's ground truth (tests/fixtures/code_scroll/), plus the
fixture's own pure helpers and the scorer used by the agent-level evals."""

from __future__ import annotations

import sys
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"
if str(FIXTURES) not in sys.path:
    sys.path.insert(0, str(FIXTURES))

import make_code_scroll as fx
import score_code_files as sc
from wfm import codefiles as cf

INV = fx.source_lines("inventory.py")
TOML = fx.source_lines("settings.toml")
KEYFRAMES = [0.0, 7.0, 14.0, 21.0, 28.0, 35.0, 42.0, 49.05, 56.1]  # the 7 s floor on the fixture


def _block(n: int, t: float, *, numbered: bool, k: int = 0, only: tuple[int, int] | None = None) -> str:
    """What a perfect V reader writes for the frame at t (optionally only lines a-b of it)."""
    name, a, b = fx.visible_lines(t)
    if only:
        a, b = max(a, only[0]), min(b, only[1])
    src = fx.source_lines(name)
    lang = "python" if name.endswith(".py") else "toml"
    ref = f"{n}.{k}" if k else str(n)
    head = f"CODE#{ref} {lang} 00:{int(t):02d} zoom=f_{int(t * 1000):09d}.jpg file={name}"
    if numbered:
        return "\n".join([f"{head} lines={a}-{b}", *(f"{i}|{src[i - 1]}" for i in range(a, b + 1))])
    return "\n".join([head, *src[a - 1:b]])


def _visual(blocks: list[str]) -> str:
    return "V V01 local-x 00:00-00:58\n#1 00:00-00:07 | editor (see CODE#1)\n" + "\n".join(blocks) + "\nEND\n"


def _stitch(blocks: list[str]) -> list[cf.CodeFile]:
    return cf.stitch(cf.split_visual(_visual(blocks))[1])


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------
def test_split_visual_moves_bodies_out_and_keeps_headers() -> None:
    text = ('V V01 k 00:00-01:00\n#1 00:00-00:10 | code (see CODE#1)\nCODE#1 sql 00:00 zoom=a.jpg\n'
            'BEGIN\nEND\nCOMMIT;\nCODE#2 c 00:30 zoom=b.jpg file=My File.c lines=3-4\n3|int x;\n4|\nEND\n'
            'V V02 k 01:00-02:00\n#9 01:00-01:10 | slide\nEND\n')
    kept, blocks = cf.split_visual(text)
    assert kept == ('V V01 k 00:00-01:00\n#1 00:00-00:10 | code (see CODE#1)\nCODE#1 sql 00:00 zoom=a.jpg\n'
                    'CODE#2 c 00:30 zoom=b.jpg file=My File.c lines=3-4\nEND\n'
                    'V V02 k 01:00-02:00\n#9 01:00-01:10 | slide\nEND\n')
    assert blocks[0].body == ["BEGIN", "END", "COMMIT;"]  # an END followed by code is code
    assert blocks[0].file is None and blocks[0].numbered is None  # old format: plain, nameless
    assert blocks[1].file == "My File.c" and blocks[1].numbered == [(3, "int x;"), (4, "")]
    assert blocks[1].ref == "#2" and blocks[1].attrs["zoom"] == "b.jpg"
    assert cf.split_visual("V V01 k 0-1\n#1 00:00-00:10 | slide\nEND\n")[1] == []


def test_numbered_body_tolerates_reader_habits() -> None:
    # one pad space after the bar on every line (odd minimum indent) is stripped
    _, (b,) = cf.split_visual("CODE#1 python 00:00 file=a.py lines=5-7\n5| def f():\n6|     return 1\n7|\nEND\n")
    assert b.numbered == [(5, "def f():"), (6, "    return 1"), (7, "")]
    # real indentation is kept
    _, (b,) = cf.split_visual("CODE#1 python 00:00 file=a.py lines=5-6\n5|    x = 1\n6|    y = 2\nEND\n")
    assert b.numbered == [(5, "    x = 1"), (6, "    y = 2")]
    # range given, numbers left off: counted
    _, (b,) = cf.split_visual("CODE#1 python 00:00 file=a.py lines=5-6\nx = 1\ny = 2\nEND\n")
    assert b.numbered == [(5, "x = 1"), (6, "y = 2")]
    # range that does not fit unnumbered lines: plain
    _, (b,) = cf.split_visual("CODE#1 python 00:00 file=a.py lines=5-9\nx = 1\ny = 2\nEND\n")
    assert b.numbered is None and b.body == ["x = 1", "y = 2"]
    # probe block id, cut-off marker line ignored, `?` = no name
    _, (b,) = cf.split_visual("CODE#4.2 js 00:09 file=? lines=8-9\n8|a()\n# [cut off]\n9|b()\nEND\n")
    assert (b.n, b.k, b.ref, b.file) == (4, 2, "#4.2", None) and b.numbered == [(8, "a()"), (9, "b()")]


# --------------------------------------------------------------------------
# stitching the fixture (ground truth = tests/fixtures/code_scroll/)
# --------------------------------------------------------------------------
def test_fixture_keyframes_miss_a_page() -> None:
    """Why gap recovery exists: scene detection does not fire on a dark editor, so the frames
    are the 7 s floor, and the page held for 1.2 s (lines 61-93) is in none of them."""
    missing = fx.coverage(KEYFRAMES)
    assert fx.line_ranges(missing["inventory.py"]) == [(64, 90)] and missing["settings.toml"] == []
    assert fx.visible_lines(9.6) == ("inventory.py", 61, 93)
    assert fx.visible_lines(21.0) == ("settings.toml", 1, 33)
    assert abs(fx.DURATION - 58.1) < 0.05 and fx.VISIBLE == 33 and len(INV) == 203


def test_stitch_numbered_scroll_flags_the_gap() -> None:
    files = _stitch([_block(i + 1, t, numbered=True) for i, t in enumerate(KEYFRAMES)])
    assert [(f.name, f.lang) for f in files] == [("inventory.py", "python"), ("settings.toml", "toml")]
    inv, toml = files
    assert inv.gaps() == [(64, 90)] and toml.gaps() == [] and inv.edited == 0
    body = cf.file_body(inv)
    assert body[63] == "# [gap: lines 64-90 not shown in the video]"
    assert body[:63] == INV[:63] and body[64:] == INV[90:]  # nothing invented, nothing else lost
    assert cf.file_body(toml) == TOML
    s = sc.score(INV, *sc.split_written("\n".join(body)))
    assert (s["correct"], s["missing"], s["missing_flagged"], s["gaps_silent"], s["invented"]) == (176, 27, 27, 0, 0)
    md = cf.render_code_md("local-x", "full-1080", "0.0.0", files)
    assert md.splitlines()[0] == "# code local-x full-1080 skill=0.0.0 files=2"
    assert ("=== 01 inventory.py | python | 00:00-00:56 | lines 1-203 | gaps: lines 64-90 | "
            "from CODE#1,#2,#3,#5,#6,#7,#8,#9 ===") in md
    assert "=== 02 settings.toml | toml | 00:21 | lines 1-33 | gaps: none | from CODE#4 ===" in md


def test_stitch_numbered_probe_block_closes_the_gap() -> None:
    blocks = [_block(i + 1, t, numbered=True) for i, t in enumerate(KEYFRAMES)]
    blocks.append(_block(2, 9.6, numbered=True, k=1, only=(64, 90)))  # the reader's extra frame
    inv = _stitch(blocks)[0]
    assert inv.gaps() == [] and cf.file_body(inv) == INV
    assert inv.refs[:3] == ["#1", "#2", "#2.1"]  # tile order: the probe sits after its tile


def test_stitch_plain_scroll_by_overlap() -> None:
    files = _stitch([_block(i + 1, t, numbered=False) for i, t in enumerate(KEYFRAMES)])
    inv, toml = files
    assert inv.gaps() == 1 and cf.file_body(toml) == TOML
    body = cf.file_body(inv)
    assert body[63] == "# [gap: lines not shown in the video]"
    assert body[:63] == INV[:63] and body[64:] == INV[90:]
    assert "no line numbers on screen | gaps: 1 (no shared line between frames)" in cf.section_header(1, inv)
    # the whole extra frame (lines 61-93) overlaps both sides: the hole closes
    blocks = [_block(i + 1, t, numbered=False) for i, t in enumerate(KEYFRAMES)]
    blocks.append(_block(2, 9.6, numbered=False, k=1))
    inv = _stitch(blocks)[0]
    assert inv.gaps() == 0 and cf.file_body(inv) == INV


def test_stitch_plain_scroll_up_and_out_of_order_pages() -> None:
    def blk(n: int, a: int, b: int) -> str:
        return "\n".join([f"CODE#{n} python 00:0{n} file=inventory.py", *INV[a - 1:b]])
    inv = _stitch([blk(1, 40, 72), blk(2, 20, 50), blk(3, 1, 30), blk(4, 60, 100)])[0]
    assert inv.gaps() == 0 and cf.file_body(inv) == INV[:100]


# --------------------------------------------------------------------------
# edits, versions, nameless blocks
# --------------------------------------------------------------------------
def _num(n: int, name: str, first: int, lines: list[str], lang: str = "python") -> str:
    head = f"CODE#{n} {lang} 00:{n:02d} file={name} lines={first}-{first + len(lines) - 1}"
    return "\n".join([head, *(f"{first + i}|{x}" for i, x in enumerate(lines))])


def test_later_frame_wins_and_typed_lines_shift_the_rest() -> None:
    old = [f"line_{i} = {i}" for i in range(1, 41)]
    edited = [*old[:9], "line_10 = 'changed'", *old[10:20]]
    f = _stitch([_num(1, "a.py", 1, old), _num(2, "a.py", 1, edited)])[0]
    assert cf.file_body(f) == [*edited, *old[20:]] and f.edited == 1 and f.version == 1
    # 3 lines typed at line 15 while lines 10-30 are on screen: 31-40 become 34-43
    typed = [*old[9:14], "new_a = 1", "new_b = 2", "new_c = 3", *old[14:27]]
    f = _stitch([_num(1, "a.py", 1, old), _num(2, "a.py", 10, typed)])[0]
    assert cf.file_body(f) == [*old[:14], "new_a = 1", "new_b = 2", "new_c = 3", *old[14:]]
    assert f.gaps() == [] and max(f.num or {}) == 43
    # 2 lines deleted in the window: the lines below move up
    cut = [*old[9:14], *old[16:32]]
    f = _stitch([_num(1, "a.py", 1, old), _num(2, "a.py", 10, cut)])[0]
    assert cf.file_body(f) == [*old[:14], *old[16:]]


def test_same_numbers_other_code_is_a_new_version() -> None:
    one = ["fn main() {", '    println!("hello");', "}"]
    two = ["use std::fs;", "fn main() {", '    let s = fs::read_to_string("a.txt");', "}"]
    files = _stitch([_num(1, "main.rs", 1, one, "rust"), _num(2, "main.rs", 1, two, "rust"),
                     _num(3, "main.rs", 1, one, "rust")])
    assert [(f.name, f.version, f.refs) for f in files] == [("main.rs", 1, ["#1", "#3"]), ("main.rs", 2, ["#2"])]
    assert cf.file_body(files[0]) == one and cf.file_body(files[1]) == two
    assert cf.section_header(2, files[1]).startswith("=== 02 main.rs (version 2) | rust")


def test_nameless_blocks_merge_only_on_evidence() -> None:
    a = "CODE#1 python 00:01\ndef alpha():\n    return compute_alpha(1)\n"
    b = "CODE#2 python 00:02\ndef alpha():\n    return compute_alpha(1)\n\ndef beta():\n    return 2"
    c = "CODE#3 python 00:03\nimport os\nprint(os.getcwd())"
    d = "CODE#4 bash 00:04\npip install flask"
    files = _stitch([a, b, c, d])
    assert [(f.name, f.lang, f.refs) for f in files] == [
        (None, "python", ["#1", "#2"]), (None, "python", ["#3"]), (None, "bash", ["#4"])]
    assert cf.file_body(files[0])[-1] == "    return 2" and files[0].gaps() == 0
    assert cf.section_header(1, files[0]).startswith("=== 01 ? | python | 00:01-00:02 | 5 lines")
    # look-alike short lines are not an overlap: a named file gets a gap, never a wrong join
    x = "CODE#1 js 00:01 file=a.js\nfunction one() {\n  return 1\n}"
    y = "CODE#2 js 00:02 file=a.js\nfunction two() {\n  go()\n}"
    f = _stitch([x, y])[0]
    assert f.gaps() == 1 and cf.file_body(f)[3] == "// [gap: lines not shown in the video]"


def test_leading_gap_and_comment_syntax() -> None:
    f = _stitch([_num(1, "big.go", 120, ["x := 1", "y := 2"], "go")])[0]
    assert cf.file_body(f) == ["// [gap: lines 1-119 not shown in the video]", "x := 1", "y := 2"]
    assert cf.comment_line("q.sql", "sql", "g") == "-- g" and cf.comment_line("a.html", "html", "g") == "<!-- g -->"
    assert cf.comment_line(None, "python", "g") == "# g" and cf.comment_line("s.css", "css", "g") == "/* g */"
    assert cf.comment_line("Dockerfile", "dockerfile", "g") == "# g" and cf.comment_line(None, "", "g") == "# g"
    f = _stitch([_num(1, "a.py", 1, ["a = 1"]), _num(2, "a.py", 3, ["c = 3"])])[0]
    assert cf.file_body(f) == ["a = 1", "# [gap: line 2 not shown in the video]", "c = 3"]


# --------------------------------------------------------------------------
# scorer
# --------------------------------------------------------------------------
def test_scorer_counts() -> None:
    header = "# transcribed from video at 00:00, code_scroll.mp4; check before running"
    perfect = sc.score(INV, *sc.split_written("\n".join([header, *INV])))
    assert perfect["correct"] == 203 and not any(perfect[k] for k in ("wrong", "missing", "invented", "false_gaps"))
    silent = sc.score(INV, *sc.split_written("\n".join([*INV[:63], *INV[90:]])))
    assert (silent["missing"], silent["gaps_silent"], silent["gaps_flagged"]) == (27, 1, 0)
    guessed = [*INV[:63], *("    pass  # guessed" for _ in range(27)), *INV[90:]]
    g = sc.score(INV, *sc.split_written("\n".join(guessed)))
    assert g["wrong"] == 27 and g["correct"] == 176 and g["missing"] == 0
    extra = sc.score(INV, *sc.split_written("\n".join([*INV, "print('made up')"])))
    assert extra["invented"] == 1 and extra["correct"] == 203
    i = next(i for i, x in enumerate(INV) if x.startswith("        "))
    bad_indent = sc.score(INV, *sc.split_written("\n".join([*INV[:i], INV[i].lstrip(), *INV[i + 1:]])))
    assert bad_indent["wrong"] == 1 and bad_indent["indent_only"] == 1
    needless = sc.score(INV, *sc.split_written("\n".join([*INV[:10], "# [gap: lines not shown in the video]", *INV[10:]])))
    assert needless["false_gaps"] == 1 and needless["correct"] == 203
    repo = [*INV[:63], "# [lines 64-90 from repo github.com/a/b@0123456789ab src/inventory.py, not shown in the video]",
            *INV[63:90], "# [end of lines from repo]", *INV[90:]]
    filled = sc.score(INV, *sc.split_written("\n".join(repo)))
    assert filled["correct"] == 203 and filled["invented"] == 0 and filled["false_gaps"] == 0
