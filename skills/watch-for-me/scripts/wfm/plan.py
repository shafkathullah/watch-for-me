"""Agent-facing files: views/<vtag>/transcript-<rtag>.md (transcripts/<rtag>.md without frames),
views/<vtag>/context.md,
views/<vtag>/windows/Tnn.md, views/<vtag>/visual.md (visual-put), and
runs/<id>/plan.json (spec 3 file formats, spec 4.6 rules).

Pure render functions (render_*, build_plan, window_bounds, visual_batches) are
separated from writers (write_*) so tests need no filesystem.

plan.json shape (spec 4.6 + skeleton additions marked +):
{"v":1,"run_id","stage":"frames"|"done"(+),"mode":"visual"|"windowed"|null(+null at frames),
 "transcript_tokens_est":int|null(+null at frames),"code":bool,
 "views":{key: view_dir}(+),
 "inline_sheets":[{"key","sheets":[abs...]}],
 "visual_batches":[{"id":"V01","key","sheets":[abs...],"tiles":[first_n,last_n],"t0","t1"}],
 "transcript_windows":[{"id":"T01","key","file":abs,"t0","t1","words"}](+shape),
 "tasks_dir":abs(+, write_tasks), "light_sheets":[...](+, cli)}
- V ids are numbered across the whole run (input order, then time); T ids are per
  video and equal the window file name (windows/T01.md), so (key, id) is unique.
- Videos without frames (audio-only, no_video, frames error) contribute no visual
  entries; videos with status "error" contribute nothing.

Subagent task files (write_tasks, spec 4.6): runs/<id>/tasks/<key>.<id>.json, one per V batch,
T window and (windowed mode) one M merge per video. The agent's subagent prompt names the file
instead of carrying the batch JSON, the FRAME command and the output path; each V/T subagent
writes its output to the task's `out` (runs/<id>/parts/<key>.<id>.md) and replies `stored <id>`,
so its text reaches the main agent once at most (visual.md via `visual-put --run`).
"""

from __future__ import annotations

import re
from itertools import pairwise
from pathlib import Path
from typing import Any

import asr_common

from .cache import KeyPaths, ViewPaths, atomic_write_json, atomic_write_text
from .types import fmt_ts

MAX_SHEETS_PER_BATCH = 4
INLINE_MAX_SHEETS = 2  # a video with <= 2 sheets goes to inline_sheets
# transcript_tokens_est measures the transcript .md the agent would Read (estimate_read_tokens).
# Calibrated on the 1 h talk (#1): 71,869 chars / 728 lines read as ~25.7k tokens in Claude Code
# (the `[mm:ss]` prefixes, `-- #n --` markers and the Read tool's line numbers all cost tokens;
# words x 1.35 said 16.2k). 15k keeps a visual-mode run (transcript + V outputs + ~6k skill
# overhead) inside the 25k main-context budget (spec 4.7); a 1 h talk goes windowed.
CHARS_PER_TOKEN = 3.0
TOKENS_PER_LINE = 2.0  # Read's "   123\t" line-number prefix + newline
WINDOWED_THRESHOLD = 15_000  # transcript_tokens_est > this -> windowed
WINDOW_TARGET_S = 900.0  # ~15 min
WINDOW_SLACK_S = 300.0  # snap a boundary to an anchor within +-5 min of the target
DESCRIPTION_CHARS = 600
VISUAL_HEADER_PREFIX = "# visual "


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------
def estimate_read_tokens(text: str) -> int:
    """Main-context cost of Reading this transcript .md: non-CJK chars / CHARS_PER_TOKEN +
    1 token per Han/kana/Hangul char + TOKENS_PER_LINE per line (calibration in the
    constants above). Empty text -> 0."""
    if not text:
        return 0
    cjk = len(asr_common._CJK_RE.findall(text))
    lines = text.count("\n") + (0 if text.endswith("\n") else 1)
    return round((len(text) - cjk) / CHARS_PER_TOKEN + cjk + lines * TOKENS_PER_LINE)


def choose_mode(tokens_est: int) -> str:
    """"visual" if tokens_est <= WINDOWED_THRESHOLD else "windowed"."""
    return "visual" if tokens_est <= WINDOWED_THRESHOLD else "windowed"


def markers_from_frames(frames_json: dict[str, Any] | None) -> list[tuple[int, float]]:
    """[(n, t0)] for segments placed on sheets (repeat_of is None), time order."""
    if not frames_json:
        return []
    segs = [s for s in frames_json.get("segments") or [] if s.get("repeat_of") is None]
    return sorted(((int(s["n"]), float(s["t0"])) for s in segs), key=lambda m: (m[1], m[0]))


def _words(text: str) -> int:
    return asr_common.count_words(text)


def render_transcript_md(transcript: dict[str, Any], rtag: str,
                         markers: list[tuple[int, float]] | None = None,
                         t0: float | None = None, t1: float | None = None) -> str:
    """Spec 3 transcript.md:
    line 1 "# transcript <key> <rtag> lang=<primary_lang> words=<N>" (N = words in the
    rendered slice), then one line per segment "[<fmt_ts(t0)>] text"; a marker line
    "-- #<n> <fmt_ts(t)> --" is inserted before the first segment whose t0 >= marker t
    (markers after the last segment are appended at the end). Optional t0/t1 restrict
    the slice to segments STARTING in [t0, t1) (so windows never repeat a segment;
    t1 None = open end) and to markers in the same range. Trailing newline."""
    lo = float("-inf") if t0 is None else t0
    hi = float("inf") if t1 is None else t1
    segs = [s for s in transcript.get("segments") or [] if lo <= float(s["t0"]) < hi]
    marks = sorted((m for m in markers or [] if lo <= m[1] < hi), key=lambda m: (m[1], m[0]))
    body: list[str] = []
    mi = 0
    for s in segs:
        st = float(s["t0"])
        while mi < len(marks) and marks[mi][1] <= st:
            body.append(f"-- #{marks[mi][0]} {fmt_ts(marks[mi][1])} --")
            mi += 1
        text = " ".join(str(s.get("text", "")).split())
        body.append(f"[{fmt_ts(st)}] {text}")
    body += [f"-- #{n} {fmt_ts(t)} --" for n, t in marks[mi:]]
    words = sum(_words(str(s.get("text", ""))) for s in segs)
    header = (f"# transcript {transcript.get('key', '-')} {rtag} "
              f"lang={transcript.get('primary_lang') or '-'} words={words}")
    return "\n".join([header, *body]) + "\n"


def window_bounds(t0: float, t1: float, anchors: list[float], *,
                  target: float = WINDOW_TARGET_S, slack: float = WINDOW_SLACK_S) -> list[tuple[float, float]]:
    """Split [t0, t1) into ~target windows. anchors = chapter start times if the video has
    chapters, else frame-segment starts (t0 of placed segments). Each boundary = the anchor
    nearest prev + target within +-slack, else exactly prev + target. The last window
    absorbs a remainder < target / 2. Pure; returns contiguous [(a, b), ...]."""
    if t1 <= t0:
        return [(t0, t1)]
    pts = sorted(a for a in anchors if t0 < a < t1)
    bounds = [t0]
    prev = t0
    while t1 - prev > target * 1.5:
        ideal = prev + target
        near = [a for a in pts if abs(a - ideal) <= slack and a - prev >= target / 2]
        cut = min(near, key=lambda a: (abs(a - ideal), a)) if near else ideal
        if t1 - cut < target / 2:  # the remainder would be tiny: stop, last window absorbs it
            break
        bounds.append(cut)
        prev = cut
    bounds.append(t1)
    return list(pairwise(bounds))


def visual_batches(key: str, frames_json: dict[str, Any] | None, view_dir: Path,
                   start_id: int) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """-> (batches, inline_entry). <= INLINE_MAX_SHEETS sheets -> ([], {"key","sheets":[abs]}).
    Else consecutive groups of MAX_SHEETS_PER_BATCH sheets -> batch dicts with id
    f"V{start_id + i:02d}", abs sheet paths, tiles [first n, last n], t0 of first tile's
    segment, t1 of last tile's segment. Zero sheets -> ([], None)."""
    sheets = (frames_json or {}).get("sheets") or []
    if not sheets:
        return [], None
    paths = [str(Path(view_dir) / sh["file"]) for sh in sheets]
    if len(sheets) <= INLINE_MAX_SHEETS:
        return [], {"key": key, "sheets": paths}
    by_n = {int(s["n"]): s for s in (frames_json or {}).get("segments") or []}
    batches: list[dict[str, Any]] = []
    for i, lo in enumerate(range(0, len(sheets), MAX_SHEETS_PER_BATCH)):
        group = sheets[lo:lo + MAX_SHEETS_PER_BATCH]
        tiles = [n for sh in group for n in sh.get("tiles") or []]
        first, last = (min(tiles), max(tiles)) if tiles else (None, None)
        t0 = by_n[first]["t0"] if first in by_n else None
        t1 = by_n[last]["t1"] if last in by_n else None
        batches.append({"id": f"V{start_id + i:02d}", "key": key,
                        "sheets": paths[lo:lo + MAX_SHEETS_PER_BATCH],
                        "tiles": [first, last], "t0": t0, "t1": t1})
    return batches, None


def build_plan(run_id: str, stage: str, code: bool, videos: list[dict[str, Any]]) -> dict[str, Any]:
    """Assemble plan.json (module docstring shape).

    videos: one entry per VideoResult with status != "error", in input order:
      {"key", "view_dir": str, "frames_json": dict|None, "words": int|None,
       "tokens": int|None (estimate_read_tokens of the transcript .md this run wrote),
       "windows": [{"id","file","t0","t1","words"}] (already written, windowed mode only)}
    stage "frames": mode/transcript_tokens_est null, transcript_windows [].
    stage "done": transcript_tokens_est = sum of tokens; mode via choose_mode; windows
    included only when mode == "windowed".
    """
    inline: list[dict[str, Any]] = []
    batches: list[dict[str, Any]] = []
    for v in videos:
        b, inl = visual_batches(v["key"], v.get("frames_json"), Path(v["view_dir"]), len(batches) + 1)
        batches += b
        if inl:
            inline.append(inl)
    mode = tokens = None
    windows: list[dict[str, Any]] = []
    if stage == "done":
        tokens = sum(int(v.get("tokens") or 0) for v in videos)
        mode = choose_mode(tokens)
        if mode == "windowed":
            windows = [{"key": v["key"], **w} for v in videos for w in v.get("windows") or []]
    return {"v": 1, "run_id": run_id, "stage": stage, "mode": mode, "transcript_tokens_est": tokens,
            "code": bool(code), "views": {v["key"]: str(v["view_dir"]) for v in videos},
            "inline_sheets": inline, "visual_batches": batches, "transcript_windows": windows}


_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)


def _fmt_date(d: Any) -> str:
    s = str(d or "")
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if re.fullmatch(r"\d{8}", s) else (s or "-")


def _one_line(s: Any) -> str:
    return " ".join(str(s).split()) if s not in (None, "") else "-"


def engines_line(transcript: dict[str, Any]) -> str:
    """"parakeet en 58 chunks, whisper fr 2" (spec 3 context.md); chunks with no speech are
    counted too (they were routed). Ordered by first appearance."""
    if (transcript.get("stats") or {}).get("words") == 0:  # music / noise: LID labels are noise
        return "no speech"
    counts: dict[tuple[str, str], int] = {}
    for c in transcript.get("chunks") or []:
        k = (str(c.get("engine") or "-"), str(c.get("lang") or "-"))
        counts[k] = counts.get(k, 0) + 1
    parts = [f"{e} {lang} {n}" for (e, lang), n in counts.items()]
    if parts:
        parts[0] += " chunks"
    return ", ".join(parts) or "no speech"


def render_context_md(meta: dict[str, Any], vtag: str, transcript: dict[str, Any] | None,
                      frames_json: dict[str, Any] | None, paths: dict[str, str | None]) -> str:
    """Spec 3 context.md (small; read first by the agent):
        # <title>
        source: <webpage_url or local path> | <uploader> | <upload_date> | <h:mm:ss> | <views> views
        view: <vtag> | transcript: <engines, e.g. "parakeet en 58 chunks, whisper fr 2"> | frames: <N> segments, <S> sheets <grid>
        chapters: 00:00 Intro; 11:22 LLM training; ...        (omitted when none)
        description: <first 600 chars, URLs stripped, whitespace collapsed>   (omitted when empty)
        files: transcript=<abs>, frames.json=<abs>, sheets=<abs dir>
    Missing fields render as "-"; "transcript: none" / "frames: none" when absent.
    paths keys: "transcript", "frames_json", "sheets_dir"."""
    title = _one_line(meta.get("title"))
    dur = meta.get("duration")
    views = meta.get("view_count")
    source = [
        _one_line(meta.get("webpage_url") or meta.get("local_path")),
        _one_line(meta.get("uploader") or meta.get("channel")),
        _fmt_date(meta.get("upload_date")),
        fmt_ts(float(dur)) if isinstance(dur, (int, float)) else "-",
        f"{views:,} views" if isinstance(views, int) else "-",
    ]
    tpart = f"transcript: {engines_line(transcript)}" if transcript else "transcript: none"
    if frames_json and frames_json.get("sheets") is not None:
        segs = frames_json.get("segments") or []
        sheets = frames_json.get("sheets") or []
        grid = sheets[0].get("grid", "-") if sheets else "-"
        fpart = f"frames: {len(segs)} segments, {len(sheets)} sheets {grid}"
    else:
        fpart = "frames: none"
    lines = [f"# {title}", "source: " + " | ".join(source), f"view: {vtag} | {tpart} | {fpart}"]
    chapters = [c for c in meta.get("chapters") or [] if isinstance(c, dict)]
    if chapters:
        items = [f"{fmt_ts(float(c.get('start_time') or 0))} {_one_line(c.get('title'))}" for c in chapters]
        lines.append("chapters: " + "; ".join(items))
    desc = " ".join(_URL_RE.sub("", str(meta.get("description") or "")).split())
    if desc:
        lines.append("description: " + desc[:DESCRIPTION_CHARS] + ("…" if len(desc) > DESCRIPTION_CHARS else ""))
    lines.append("files: " + ", ".join([
        f"transcript={paths.get('transcript') or '-'}",
        f"frames.json={paths.get('frames_json') or '-'}",
        f"sheets={paths.get('sheets_dir') or '-'}",
    ]))
    return "\n".join(lines) + "\n"


def visual_header(key: str, vtag: str, flags: str, version: str) -> str:
    """"# visual <key> <vtag> flags=<flags or -> skill=<version>" (spec 3 visual-put)."""
    f = "".join((flags or "").split()) or "-"
    return f"{VISUAL_HEADER_PREFIX}{key} {vtag} flags={f} skill={version}"


def parse_visual_header(line: str) -> dict[str, str] | None:
    """Inverse of visual_header -> {"key","vtag","flags","skill"} or None."""
    if not line.startswith(VISUAL_HEADER_PREFIX):
        return None
    parts = line[len(VISUAL_HEADER_PREFIX):].strip().split()
    if len(parts) != 4 or not parts[2].startswith("flags=") or not parts[3].startswith("skill="):
        return None
    return {"key": parts[0], "vtag": parts[1], "flags": parts[2][6:], "skill": parts[3][6:]}


# --------------------------------------------------------------------------
# Writers (atomic via cache.atomic_write_*)
# --------------------------------------------------------------------------
def write_transcript_md(kp: KeyPaths, rtag: str, transcript: dict[str, Any],
                        markers: list[tuple[int, float]] | None, view: ViewPaths | None = None) -> Path:
    """Write the agent-facing transcript. With a view: views/<vtag>/transcript-<rtag>.md, whose
    `-- #n --` markers match that view's tiles (720p and 1080p views never share one file).
    Without: transcripts/<rtag>.md (no frames, so no markers; identical for every run)."""
    path = view.dir / f"transcript-{rtag}.md" if view is not None else kp.transcript_md(rtag)
    atomic_write_text(path, render_transcript_md(transcript, rtag, markers))
    return path


def write_windows(view: ViewPaths, transcript: dict[str, Any], rtag: str,
                  bounds: list[tuple[float, float]],
                  markers: list[tuple[int, float]] | None) -> list[dict[str, Any]]:
    """Write views/<vtag>/windows/T01.md... (render_transcript_md restricted to each window;
    the first window is open at the start and the last open at the end so no segment is lost;
    stale T*.md deleted first). Returns [{"id","file","t0","t1","words"}]."""
    wdir = view.windows_dir
    wdir.mkdir(parents=True, exist_ok=True)
    for old in wdir.glob("T*.md"):
        old.unlink(missing_ok=True)
    out: list[dict[str, Any]] = []
    for i, (a, b) in enumerate(bounds, start=1):
        lo = None if i == 1 else a
        hi = None if i == len(bounds) else b
        text = render_transcript_md(transcript, rtag, markers, lo, hi)
        wid = f"T{i:02d}"
        path = wdir / f"{wid}.md"
        atomic_write_text(path, text)
        words = int(text.split("\n", 1)[0].rsplit("words=", 1)[-1])
        out.append({"id": wid, "file": str(path), "t0": round(a, 3), "t1": round(b, 3), "words": words})
    return out


def write_context_md(view: ViewPaths, text: str) -> Path:
    atomic_write_text(view.context_md, text)
    return view.context_md


def write_plan(run_dir: Path, plan: dict[str, Any]) -> Path:
    """runs/<id>/plan.json, atomic."""
    path = Path(run_dir) / "plan.json"
    atomic_write_json(path, plan)
    return path


def write_visual_md(view: ViewPaths, key: str, flags: str, body: str, version: str) -> Path:
    """`visual-put`: header line + "\\n" + body (stripped, trailing newline) -> views/<vtag>/visual.md.
    Raises FileNotFoundError when the view dir does not exist (unknown KEY/VTAG)."""
    if not view.dir.is_dir():
        raise FileNotFoundError(str(view.dir))
    text = visual_header(key, view.vtag, flags, version) + "\n" + body.strip() + "\n"
    atomic_write_text(view.visual_md, text)
    return view.visual_md


# --------------------------------------------------------------------------
# Subagent task files + compact plan (WFM_WAIT)
# --------------------------------------------------------------------------
TASKS_DIR = "tasks"
PARTS_DIR = "parts"


def task_name(key: str, tid: str) -> str:
    """"<key>.<id>" (V ids are run-wide, T ids per video, M once per video: always unique)."""
    return f"{key}.{tid}"


def shell_quote(s: str) -> str:
    """POSIX single quotes, embedded ' as '\\'' (SKILL.md section 1 quoting rule)."""
    return "'" + s.replace("'", "'\\''") + "'"


def frame_command(watch_py: Path | str, key: str) -> str:
    """The exact FRAME command a V reader may run (only --t/--crop/--width change)."""
    return f"uv run --script {shell_quote(str(watch_py))} frame {shell_quote(key)} --t <SECONDS>"


def build_tasks(p: dict[str, Any], run_dir: Path, watch_py: Path | str) -> dict[str, dict[str, Any]]:
    """{name: task dict} for every V batch, T window and (windowed) M merge in plan p.
    V: {"role":"V","id","key","sheets","tiles","t0","t1","code","frame","out"}
    T: {"role":"T","id","key","file","t0","t1","words","out"}
    M: {"role":"M","id":"M","key","context","visual"(abs visual.md or null without frames),
        "digests":[T outs, time order]}
    `out` = runs/<id>/parts/<name>.md. Pure."""
    parts = Path(run_dir) / PARTS_DIR
    views = p.get("views") or {}
    out: dict[str, dict[str, Any]] = {}
    for b in p.get("visual_batches") or []:
        name = task_name(b["key"], b["id"])
        out[name] = {"role": "V", **b, "code": bool(p.get("code")),
                     "frame": frame_command(watch_py, b["key"]), "out": str(parts / f"{name}.md")}
    digests: dict[str, list[str]] = {}
    for w in p.get("transcript_windows") or []:
        name = task_name(w["key"], w["id"])
        out[name] = {"role": "T", **w, "out": str(parts / f"{name}.md")}
        digests.setdefault(w["key"], []).append(out[name]["out"])
    has_frames = {b["key"] for b in p.get("visual_batches") or []} | {
        e["key"] for e in p.get("inline_sheets") or []}
    for key, outs in digests.items():
        vdir = Path(views.get(key) or "")
        out[task_name(key, "M")] = {
            "role": "M", "id": "M", "key": key, "context": str(vdir / "context.md"),
            "visual": str(vdir / "visual.md") if key in has_frames else None, "digests": outs}
    return out


def write_tasks(run_dir: Path, p: dict[str, Any], watch_py: Path | str) -> Path:
    """Write runs/<id>/tasks/<name>.json for build_tasks(p), create runs/<id>/parts/ (the
    subagents' Write targets), set p["tasks_dir"]. Returns the tasks dir."""
    tdir = Path(run_dir) / TASKS_DIR
    tdir.mkdir(parents=True, exist_ok=True)
    (Path(run_dir) / PARTS_DIR).mkdir(parents=True, exist_ok=True)
    for name, task in build_tasks(p, run_dir, watch_py).items():
        atomic_write_json(tdir / f"{name}.json", task)
    p["tasks_dir"] = str(tdir)
    return tdir


def compact_plan(p: dict[str, Any] | None) -> dict[str, Any] | None:
    """What WFM_WAIT prints instead of the whole plan.json (a 2 h video's plan is ~7k chars
    and every wait printed it): task names per role (prompt: `<tasks_dir>/<name>.json`) and
    the few sheet paths the main agent reads itself."""
    if not p:
        return p
    batches = p.get("visual_batches") or []
    windows = p.get("transcript_windows") or []
    m_keys = list(dict.fromkeys(w["key"] for w in windows))
    return {
        "stage": p.get("stage"), "mode": p.get("mode"), "transcript_tokens_est": p.get("transcript_tokens_est"),
        "code": p.get("code"), "tasks_dir": p.get("tasks_dir"),
        "parts_dir": str(Path(p["tasks_dir"]).parent / PARTS_DIR) if p.get("tasks_dir") else None,
        "v_tasks": [task_name(b["key"], b["id"]) for b in batches],
        "t_tasks": [task_name(w["key"], w["id"]) for w in windows],
        "m_tasks": [task_name(k, "M") for k in m_keys],
        "inline_sheets": p.get("inline_sheets") or [], "light_sheets": p.get("light_sheets") or [],
    }


def assemble_visual(run_dir: Path, p: dict[str, Any]) -> dict[str, tuple[str, str, list[str]]]:
    """Per key with V batches: (view_dir, body, missing names). body = the V parts
    (runs/<id>/parts/<key>.<Vnn>.md) stripped and joined in batch (= time) order; a part that
    is absent or blank counts as missing."""
    parts = Path(run_dir) / PARTS_DIR
    views = p.get("views") or {}
    acc: dict[str, tuple[list[str], list[str]]] = {}
    for b in p.get("visual_batches") or []:
        name = task_name(b["key"], b["id"])
        texts, missing = acc.setdefault(b["key"], ([], []))
        f = parts / f"{name}.md"
        try:
            text = f.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            text = ""
        if text:
            texts.append(text)
        else:
            missing.append(name)
    return {k: (str(views.get(k) or ""), "\n".join(t) + "\n" if t else "", m) for k, (t, m) in acc.items()}
