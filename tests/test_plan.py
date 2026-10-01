"""plan.json mode threshold + batching (<= 2 sheets inline, 4 per batch, V ids across the run),
windows at chapters, transcript.md markers, context.md, visual-put header (spec 3, 4.6)."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from wfm import cache, cli, plan


def _frames_json(n_sheets: int, per_sheet: int = 9, seg_s: float = 10.0) -> dict:
    segs, sheets = [], []
    n = 0
    for s in range(1, n_sheets + 1):
        tiles = []
        for _ in range(per_sheet):
            n += 1
            segs.append({"n": n, "t0": (n - 1) * seg_s, "t1": n * seg_s, "t": n * seg_s - 1,
                         "file": f"frames/f_{n:09d}.jpg", "novelty": 50, "repeat_of": None, "sheet": s})
            tiles.append(n)
        sheets.append({"n": s, "file": f"sheets/sheet_{s:03d}.jpg", "grid": "3x3", "w": 1456, "h": 821,
                       "est_tokens": 1560, "tiles": tiles})
    return {"v": 1, "params": {}, "raw_count": n, "segments": segs, "sheets": sheets}


def _transcript(n: int, step: float = 5.0, words_per: int = 3) -> dict:
    return {"v": 1, "key": "k", "duration": n * step, "range": None, "backend": "mlx", "primary_lang": "en",
            "chunks": [{"t0": 0.0, "t1": n * step, "lang": "en", "lang_p": 0.99, "engine": "parakeet", "model": "m"}],
            "segments": [{"i": i, "t0": i * step, "t1": i * step + step - 0.5,
                          "text": " ".join(["w"] * words_per), "lang": "en", "engine": "parakeet"}
                         for i in range(n)],
            "stats": {"words": n * words_per}}


# --------------------------------------------------------------------------
# mode + batching
# --------------------------------------------------------------------------
def test_tokens_and_mode_threshold() -> None:
    assert plan.choose_mode(15_000) == "visual"  # boundary stays visual
    assert plan.choose_mode(15_001) == "windowed"
    assert plan.estimate_read_tokens("") == 0
    # 3 chars / token + 2 per line (Read's line-number prefix)
    assert plan.estimate_read_tokens("a" * 30 + "\n") == 12
    assert plan.estimate_read_tokens("a" * 30) == 12  # no trailing newline: still one line
    assert plan.estimate_read_tokens("ab\ncd\n") == round(6 / 3 + 2 * 2)
    # CJK: one token per Han/kana char, newline counted as a char of the rest
    assert plan.estimate_read_tokens("日本語のテキスト\n") == round(1 / 3 + 8 + 2)


def test_estimate_matches_measured_1h_talk() -> None:
    """Calibration (evals 2026-10-01): the 1 h talk's transcript-full.md, 71,869 chars in 728
    lines, cost ~25.7k main-context tokens when Read (words x 1.35 said 16.2k, so it never went
    windowed). A synthetic transcript of the same shape must land within 5% and go windowed."""
    line = "[12:34] " + "x" * 90  # ~98.7 chars per line, like the real file
    text = "\n".join([line] * 728) + "\n"
    assert abs(len(text) - 71_869) < 1_000
    est = plan.estimate_read_tokens(text)
    assert abs(est - 25_700) / 25_700 < 0.05
    assert plan.choose_mode(est) == "windowed"
    # a ~25 min talk (~40% of it) stays visual
    assert plan.choose_mode(plan.estimate_read_tokens("\n".join([line] * 290) + "\n")) == "visual"


def test_visual_batches_inline_and_groups(tmp_path: Path) -> None:
    b, inline = plan.visual_batches("k", _frames_json(2), tmp_path, 1)
    assert b == [] and inline == {"key": "k", "sheets": [str(tmp_path / "sheets/sheet_001.jpg"),
                                                         str(tmp_path / "sheets/sheet_002.jpg")]}
    assert plan.visual_batches("k", _frames_json(0), tmp_path, 1) == ([], None)
    assert plan.visual_batches("k", None, tmp_path, 1) == ([], None)
    b, inline = plan.visual_batches("k", _frames_json(7), tmp_path, 3)
    assert inline is None and [x["id"] for x in b] == ["V03", "V04"]
    assert [len(x["sheets"]) for x in b] == [4, 3]
    assert b[0]["tiles"] == [1, 36] and b[1]["tiles"] == [37, 63]
    assert b[0]["t0"] == 0.0 and b[0]["t1"] == 360.0 and b[1]["t1"] == 630.0


def test_build_plan_frames_then_done(tmp_path: Path) -> None:
    vids = [
        {"key": "a", "view_dir": str(tmp_path / "a"), "frames_json": _frames_json(7), "words": 9000,
         "tokens": 12_000},
        {"key": "b", "view_dir": str(tmp_path / "b"), "frames_json": _frames_json(1), "words": 500, "tokens": 700},
        {"key": "c", "view_dir": str(tmp_path / "c"), "frames_json": _frames_json(5), "words": None},
        {"key": "d", "view_dir": str(tmp_path / "d"), "frames_json": None, "words": 100,  # audio-only
         "tokens": 150},
    ]
    p = plan.build_plan("wfmx", "frames", True, vids)
    assert p["stage"] == "frames" and p["mode"] is None and p["transcript_tokens_est"] is None
    assert p["code"] is True and p["transcript_windows"] == []
    assert [x["id"] for x in p["visual_batches"]] == ["V01", "V02", "V03", "V04"]  # numbered across videos
    assert [x["key"] for x in p["visual_batches"]] == ["a", "a", "c", "c"]
    assert p["inline_sheets"] == [{"key": "b", "sheets": [str(tmp_path / "b" / "sheets/sheet_001.jpg")]}]
    assert p["views"] == {k: str(tmp_path / k) for k in "abcd"}
    d = plan.build_plan("wfmx", "done", False, vids)
    assert d["mode"] == "visual" and d["transcript_tokens_est"] == 12_850  # sum of measured tokens
    vids[0]["tokens"] = 20_000
    vids[0]["windows"] = [{"id": "T01", "file": "/w/T01.md", "t0": 0, "t1": 900, "words": 10}]
    d = plan.build_plan("wfmx", "done", False, vids)
    assert d["mode"] == "windowed"
    assert d["transcript_windows"] == [{"key": "a", "id": "T01", "file": "/w/T01.md", "t0": 0, "t1": 900, "words": 10}]


# --------------------------------------------------------------------------
# windows
# --------------------------------------------------------------------------
def test_window_bounds_plain() -> None:
    assert plan.window_bounds(0, 3600, []) == [(0, 900), (900, 1800), (1800, 2700), (2700, 3600)]
    assert plan.window_bounds(0, 1200, []) == [(0, 1200)]  # <= 1.5 x target: one window
    # remainder < target/2 is absorbed by the last window
    assert plan.window_bounds(0, 2200, []) == [(0, 900), (900, 2200)]


def test_window_bounds_snap_to_chapters() -> None:
    chapters = [0, 682, 1150, 1500, 2400, 3300]
    assert plan.window_bounds(0, 3600, chapters) == [(0, 682), (682, 1500), (1500, 2400), (2400, 3600)]
    # anchors farther than the slack are ignored -> exact target cuts
    assert plan.window_bounds(0, 2700, [100, 1300]) == [(0, 900), (900, 1800), (1800, 2700)]
    # sub-range views: bounds stay absolute
    assert plan.window_bounds(600, 2400, []) == [(600, 1500), (1500, 2400)]


def test_write_windows_never_loses_or_repeats_segments(wfm_cache: Path) -> None:
    kp = cache.key_paths("k")
    view = kp.view("full-720")
    view.dir.mkdir(parents=True)
    t = _transcript(400, step=5.0)  # 2000 s
    bounds = plan.window_bounds(0, 2000, [])
    (view.windows_dir).mkdir(parents=True)
    (view.windows_dir / "T09.md").write_text("stale")
    wins = plan.write_windows(view, t, "full", bounds, [(1, 0.0), (2, 950.0)])
    assert [w["id"] for w in wins] == ["T01", "T02"] and not (view.windows_dir / "T09.md").exists()
    assert sum(w["words"] for w in wins) == 1200
    t2 = Path(wins[1]["file"]).read_text().splitlines()
    assert t2[0] == "# transcript k full lang=en words=" + str(wins[1]["words"])
    assert t2[1] == "[15:00] w w w" and "-- #2 15:50 --" in t2
    lines = [ln for w in wins for ln in Path(w["file"]).read_text().splitlines() if ln.startswith("[")]
    assert len(lines) == 400


# --------------------------------------------------------------------------
# transcript.md / context.md
# --------------------------------------------------------------------------
def test_render_transcript_md_markers() -> None:
    t = {"key": "youtube-x", "primary_lang": "en", "segments": [
        {"t0": 0.4, "t1": 4.1, "text": "Hi  everyone."},
        {"t0": 12.0, "t1": 15.0, "text": "Next\nslide."},
        {"t0": 3700.0, "t1": 3702.0, "text": "Late."},
    ]}
    md = plan.render_transcript_md(t, "full", [(1, 0.0), (2, 12.0), (5, 30.0), (9, 4000.0)])
    assert md == (
        "# transcript youtube-x full lang=en words=5\n"
        "-- #1 00:00 --\n"
        "[00:00] Hi everyone.\n"
        "-- #2 00:12 --\n"
        "[00:12] Next slide.\n"
        "-- #5 00:30 --\n"
        "[1:01:40] Late.\n"
        "-- #9 1:06:40 --\n"
    )
    assert plan.render_transcript_md(t, "full", None, 10, 20).splitlines() == [
        "# transcript youtube-x full lang=en words=2", "[00:12] Next slide."]


def test_markers_skip_repeats() -> None:
    fj = _frames_json(1, per_sheet=3)
    fj["segments"][1]["repeat_of"] = 1
    assert plan.markers_from_frames(fj) == [(1, 0.0), (3, 20.0)]
    assert plan.markers_from_frames(None) == []


def test_render_context_md() -> None:
    meta = {"title": "Intro to\nLLMs", "webpage_url": "https://www.youtube.com/watch?v=zjkBMFhNj_g",
            "uploader": "Andrej Karpathy", "upload_date": "20231122", "duration": 3588, "view_count": 3000000,
            "chapters": [{"start_time": 0, "title": "Intro"}, {"start_time": 682, "title": "LLM training"}],
            "description": "Talk. Slides: https://x.test/s and www.y.test/z\n\nEnjoy " + "a" * 700}
    t = {"chunks": [{"engine": "parakeet", "lang": "en"}] * 58 + [{"engine": "whisper", "lang": "fr"}] * 2}
    fj = _frames_json(7)
    md = plan.render_context_md(meta, "full-720", t, fj, {"transcript": "/c/t.md", "frames_json": "/c/f.json",
                                                          "sheets_dir": "/c/sheets"})
    lines = md.splitlines()
    assert lines[0] == "# Intro to LLMs"
    assert lines[1] == ("source: https://www.youtube.com/watch?v=zjkBMFhNj_g | Andrej Karpathy | 2023-11-22 | "
                        "59:48 | 3,000,000 views")
    assert lines[2] == ("view: full-720 | transcript: parakeet en 58 chunks, whisper fr 2 | "
                        "frames: 63 segments, 7 sheets 3x3")
    assert lines[3] == "chapters: 00:00 Intro; 11:22 LLM training"
    assert lines[4].startswith("description: Talk. Slides: and Enjoy aaa") and "http" not in lines[4]
    assert len(lines[4]) <= len("description: ") + 601
    assert lines[5] == "files: transcript=/c/t.md, frames.json=/c/f.json, sheets=/c/sheets"
    bare = plan.render_context_md({"local_path": "/v.mp4"}, "full-720", None, None, {})
    assert bare.splitlines() == ["# -", "source: /v.mp4 | - | - | - | -",
                                 "view: full-720 | transcript: none | frames: none",
                                 "files: transcript=-, frames.json=-, sheets=-"]


# --------------------------------------------------------------------------
# visual-put
# --------------------------------------------------------------------------
def test_visual_header_roundtrip() -> None:
    h = plan.visual_header("youtube-x", "full-720", "code,ask", "0.1.0")
    assert h == "# visual youtube-x full-720 flags=code,ask skill=0.1.0"
    assert plan.parse_visual_header(h) == {"key": "youtube-x", "vtag": "full-720", "flags": "code,ask",
                                           "skill": "0.1.0"}
    assert plan.visual_header("k", "v", "", "0.1.0").endswith("flags=- skill=0.1.0")
    assert plan.parse_visual_header("# transcript k") is None


def test_visual_put_cli(wfm_cache: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    kp = cache.key_paths("youtube-x")
    kp.view("full-720").dir.mkdir(parents=True)

    class _Stdin:
        def __init__(self, data: str) -> None:
            self.buffer = io.BytesIO(data.encode())

    monkeypatch.setattr("sys.stdin", _Stdin("V V01 youtube-x 00:00-01:00\n#1 00:00-00:12 | title \u00e9\nEND\n\n"))
    assert cli.main(["visual-put", "youtube-x", "--view", "full-720", "--flags", "code"]) == 0
    path = Path(capsys.readouterr().out.strip())
    assert path == kp.view("full-720").visual_md
    text = path.read_text()
    assert text.splitlines()[0] == "# visual youtube-x full-720 flags=code skill=0.1.0"
    assert text.endswith("END\n") and "\u00e9" in text
    monkeypatch.setattr("sys.stdin", _Stdin("x"))
    assert cli.main(["visual-put", "youtube-x", "--view", "full-1080", "--flags", ""]) == 2  # unknown view
    assert cli.main(["visual-put", "nope", "--view", "full-720"]) == 2
    monkeypatch.setattr("sys.stdin", _Stdin("  \n"))
    assert cli.main(["visual-put", "youtube-x", "--view", "full-720"]) == 2  # empty body


def test_visual_put_from_file(wfm_cache: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """--from: code with braces + quotes never goes through the shell; a draft inside the
    key's cache dir is removed after the store, one outside it is kept."""
    kp = cache.key_paths("youtube-x")
    view = kp.view("full-1080")
    view.dir.mkdir(parents=True)
    body = 'V V01 youtube-x 00:00-01:00\nCODE#1 rust 00:10 zoom=f.jpg\nprintln!("{}", x);\nEND\n'
    draft = view.dir / "visual.draft.md"
    draft.write_text(body)
    assert cli.main(["visual-put", "youtube-x", "--view", "full-1080", "--flags", "code", "--from", str(draft)]) == 0
    assert Path(capsys.readouterr().out.strip()).read_text().endswith(body) and not draft.exists()
    outside = tmp_path / "v.md"
    outside.write_text(body)
    assert cli.main(["visual-put", "youtube-x", "--view", "full-1080", "--from", str(outside)]) == 0
    assert outside.exists()
    assert cli.main(["visual-put", "youtube-x", "--view", "full-1080", "--from", str(tmp_path / "nope.md")]) == 2
    (tmp_path / "empty.md").write_text("\n")
    assert cli.main(["visual-put", "youtube-x", "--view", "full-1080", "--from", str(tmp_path / "empty.md")]) == 2


def test_transcript_md_is_per_view(tmp_path: Path) -> None:
    """Review #9: the markers-bearing transcript lives in the view, so 720 and 1080 runs
    never overwrite each other's markers; without frames it stays in transcripts/."""
    kp = cache.KeyPaths(tmp_path, "youtube-x")
    t = {"lang": "en", "segments": [{"t0": 0.0, "t1": 5.0, "text": "hello"}]}
    v720, v1080 = kp.view("full-720"), kp.view("full-1080")
    v720.dir.mkdir(parents=True)
    v1080.dir.mkdir(parents=True)
    a = plan.write_transcript_md(kp, "full", t, [(1, 0.0)], v720)
    b = plan.write_transcript_md(kp, "full", t, [(1, 0.0), (2, 3.0)], v1080)
    assert a == v720.dir / "transcript-full.md" and b == v1080.dir / "transcript-full.md"
    assert "#2" not in a.read_text() and "#2" in b.read_text()
    kp.transcripts_dir.mkdir()
    assert plan.write_transcript_md(kp, "full", t, None) == kp.transcript_md("full")


def test_write_plan_atomic(tmp_path: Path) -> None:
    p = plan.write_plan(tmp_path, {"v": 1})
    assert p == tmp_path / "plan.json" and cache.read_json(p) == {"v": 1}


def test_engines_line_no_speech() -> None:
    """Music / noise: chunks were routed (LID guessed "nn") but nothing was transcribed."""
    t = {"chunks": [{"engine": "parakeet", "lang": "nn"}], "segments": [], "stats": {"words": 0}}
    assert plan.engines_line(t) == "no speech"
    t["stats"]["words"] = 3
    assert plan.engines_line(t) == "parakeet nn 1 chunks"


def test_render_transcript_md_counts_cjk_words() -> None:
    """Regression (#9): words= (and so transcript_tokens_est / visual-vs-windowed) used a
    whitespace split, so 9 min of Japanese reported words=198 / tokens 267."""
    t = {"key": "k", "primary_lang": "ja",
         "segments": [{"t0": 0.0, "t1": 5.0, "text": "プログラミングにメンターがいるっていうのは誰が決めたんですか。"}]}
    head = plan.render_transcript_md(t, "full").split("\n", 1)[0]
    assert head.endswith("lang=ja words=22")  # 30 kana/Han chars x 0.75


# --------------------------------------------------------------------------
# subagent task files + visual-put --run (main-context budget, spec 4.6/4.7)
# --------------------------------------------------------------------------
def _windowed_plan(tmp_path: Path) -> dict:
    vids = [{"key": "youtube-a", "view_dir": str(tmp_path / "a" / "views" / "full-720"),
             "frames_json": _frames_json(7), "words": 9000, "tokens": 30_000,
             "windows": [{"id": "T01", "file": "/w/T01.md", "t0": 0, "t1": 900, "words": 10},
                         {"id": "T02", "file": "/w/T02.md", "t0": 900, "t1": 1800, "words": 10}]},
            {"key": "local-b", "view_dir": str(tmp_path / "b" / "views" / "full-720"), "frames_json": None,
             "words": 100, "tokens": 150,
             "windows": [{"id": "T01", "file": "/w/b/T01.md", "t0": 0, "t1": 60, "words": 10}]}]
    return plan.build_plan("wfmx", "done", True, vids)


def test_build_tasks(tmp_path: Path) -> None:
    p = _windowed_plan(tmp_path)
    rdir = tmp_path / "runs" / "wfmx"
    tasks = plan.build_tasks(p, rdir, "/skill dir/it's/watch.py")
    assert sorted(tasks) == ["local-b.M", "local-b.T01", "youtube-a.M", "youtube-a.T01", "youtube-a.T02",
                             "youtube-a.V01", "youtube-a.V02"]
    v = tasks["youtube-a.V01"]
    assert v["role"] == "V" and v["code"] is True and v["tiles"] == [1, 36] and len(v["sheets"]) == 4
    assert v["out"] == str(rdir / "parts" / "youtube-a.V01.md")
    # exact FRAME line, path single-quoted with the '\'' escape (SKILL.md section 1)
    assert v["frame"] == "uv run --script '/skill dir/it'\\''s/watch.py' frame 'youtube-a' --t <SECONDS>"
    t = tasks["local-b.T01"]
    assert t["role"] == "T" and t["file"] == "/w/b/T01.md" and t["out"] == str(rdir / "parts" / "local-b.T01.md")
    m = tasks["youtube-a.M"]
    assert m["context"] == str(tmp_path / "a" / "views" / "full-720" / "context.md")
    assert m["visual"] == str(tmp_path / "a" / "views" / "full-720" / "visual.md")
    assert m["digests"] == [tasks["youtube-a.T01"]["out"], tasks["youtube-a.T02"]["out"]]
    assert tasks["local-b.M"]["visual"] is None  # no frames: the merger skips visuals
    # visual mode: no T / M tasks
    vis = plan.build_plan("wfmx", "frames", False, [{"key": "k", "view_dir": "/v", "frames_json": _frames_json(5)}])
    assert sorted(plan.build_tasks(vis, rdir, "/w.py")) == ["k.V01", "k.V02"]


def test_write_tasks_and_compact_plan(tmp_path: Path) -> None:
    p = _windowed_plan(tmp_path)
    rdir = tmp_path / "runs" / "wfmx"
    tdir = plan.write_tasks(rdir, p, "/w.py")
    assert p["tasks_dir"] == str(tdir) and (rdir / "parts").is_dir()
    assert cache.read_json(tdir / "youtube-a.T02.json")["id"] == "T02"
    c = plan.compact_plan(p)
    assert c["v_tasks"] == ["youtube-a.V01", "youtube-a.V02"]
    assert c["t_tasks"] == ["youtube-a.T01", "youtube-a.T02", "local-b.T01"]
    assert c["m_tasks"] == ["youtube-a.M", "local-b.M"]
    assert c["parts_dir"] == str(rdir / "parts") and c["mode"] == "windowed"
    assert all((tdir / f"{n}.json").is_file() for n in c["v_tasks"] + c["t_tasks"] + c["m_tasks"])
    assert plan.compact_plan(None) is None


def _put_run(wfm_cache: Path, n_sheets: int = 7) -> tuple[Path, dict, cache.ViewPaths]:
    kp = cache.key_paths("youtube-x")
    view = kp.view("full-720")
    view.dir.mkdir(parents=True)
    rdir = cache.run_dir("wfmput01")
    p = plan.build_plan("wfmput01", "frames", False, [{"key": "youtube-x", "view_dir": str(view.dir),
                                                       "frames_json": _frames_json(n_sheets)}])
    plan.write_tasks(rdir, p, "/w.py")
    plan.write_plan(rdir, p)
    return rdir, p, view


def test_visual_put_run_joins_parts(wfm_cache: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rdir, _, view = _put_run(wfm_cache)
    v1 = "V V01 youtube-x 00:00-06:00\n#1 00:00-00:10 | slide \"A\"\nEND\n"
    v2 = 'V V02 youtube-x 06:00-10:30\nCODE#40 rust 06:40 zoom=f.jpg\nprintln!("{}", x);\nEND\n\n'
    (rdir / "parts" / "youtube-x.V02.md").write_text(v2)
    # V01 missing: nothing stored, exit 4, the line names the part path to write
    assert cli.main(["visual-put", "--run", "wfmput01", "--flags", "code"]) == 4
    out = capsys.readouterr().out.strip()
    assert out == f"missing youtube-x youtube-x.V01 {rdir / 'parts' / 'youtube-x.V01.md'}"
    assert not view.visual_md.exists()
    (rdir / "parts" / "youtube-x.V01.md").write_text(v1)
    assert cli.main(["visual-put", "--run", "wfmput01", "--flags", "code"]) == 0
    assert Path(capsys.readouterr().out.strip()) == view.visual_md
    text = view.visual_md.read_text()
    assert text.splitlines()[0] == "# visual youtube-x full-720 flags=code skill=0.1.0"
    assert text.endswith(v1 + v2.strip() + "\n")  # time order, verbatim
    # usage errors
    assert cli.main(["visual-put", "--run", "nope0001", "--flags", "x"]) == 2
    assert cli.main(["visual-put", "youtube-x", "--run", "wfmput01"]) == 2
    assert cli.main(["visual-put", "youtube-x"]) == 2  # no --view, no --run


def test_visual_put_run_nothing_to_store(wfm_cache: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _put_run(wfm_cache, n_sheets=2)  # inline sheets only: no V batches
    assert cli.main(["visual-put", "--run", "wfmput01", "--flags", "none"]) == 0
    assert capsys.readouterr().out.strip() == "nothing to store"


def test_light_sheets_fallback() -> None:
    fj = _frames_json(5)
    e = {"key": "k", "view_dir": "/v", "frames_json": fj}
    assert cli._light_sheets([e]) == [{"key": "k", "sheets": ["/v/sheets/sheet_001.jpg", "/v/sheets/sheet_003.jpg"]}]
    fj1 = _frames_json(1)
    assert cli._light_sheets([dict(e, frames_json=fj1)]) == [{"key": "k", "sheets": ["/v/sheets/sheet_001.jpg"]}]
    fj["light_sheets"] = [{"file": "sheets/light_001.jpg"}]
    assert cli._light_sheets([e]) == [{"key": "k", "sheets": ["/v/sheets/light_001.jpg"]}]
    assert cli._light_sheets([dict(e, frames_json=None)]) == []


def test_doctor_brief(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from wfm import doctor

    rep = doctor.Report(backend="mlx", checks=[doctor.Check("ffmpeg", False, None, True, "brew install ffmpeg"),
                                               doctor.Check("uv", True, "0.9", True, None)],
                        models=[{"role": "parakeet", "cached": False, "mb": 2510},
                                {"role": "lid", "cached": True, "mb": 74},
                                {"role": "whisper", "cached": False, "mb": 1610}],
                        asr_env_installed=True)
    b = doctor.to_brief(rep)
    assert b["exit"] == 3 and b["ok"] is False
    assert b["failed"] == [{"name": "ffmpeg", "blocking": True, "hint": "brew install ffmpeg"}]
    assert b["models_missing"] == ["parakeet", "whisper"]
    assert b["download_mb"] == {"english": 2510, "other_languages": 4120}
    ok = doctor.Report(backend="mlx", models=[{"role": "parakeet", "cached": True, "mb": 1}], asr_env_installed=True)
    assert doctor.to_brief(ok)["download_mb"] is None and doctor.to_brief(ok)["failed"] == []
    monkeypatch.setattr(doctor, "run_checks", lambda quick=False: rep)
    assert cli.main(["doctor", "--quick", "--brief"]) == 3
    line = capsys.readouterr().out.strip()
    assert json.loads(line) == b and len(line) < 300
