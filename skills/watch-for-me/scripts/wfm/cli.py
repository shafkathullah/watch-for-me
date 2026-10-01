"""argparse, subcommand dispatch, the `run` pipeline DAG, exit codes, signal handling (spec 3, 4.1).

Subcommands (spec 3 table): run | wait | visual-put | frame | doctor | setup | cache | cancel.
Only this module prints to stdout (through wfm.progress for `run`/`wait`).

`run` DAG (spec 4.1), implemented in run_pipeline():
    prune_runs + evict_lru(protect=index hits) -> write run.json (state running)
    -> AsrWorker.start() early if any input needs ASR work (not a cached transcript)
    per input, asyncio.gather(_process_one(...)):
        ingest.resolve (NET sem)                      WFM <key> meta done|cached|error
        with FileLock(<key>/run.lock) (async poll; second run on the same key waits):
          audio branch: ingest.download("audio"|"muxed") (NET) -> asr_client.transcribe (FIFO)
                        (or slice_transcript from "full" when a sub-range transcript is missing)
          video branch: ingest.download("video_720|1080"|"muxed") (NET)
                        -> to_thread(frames.extract_segments) -> to_thread(sheets.make_sheets
                        + make_light_sheets) -> frames.write_frames_json (CPU limiter)
                                                                         WFM <key> frames done
          muxed-only sites: ONE download task shared by both branches (ingest.media_plan).
          local files: both branches read the file directly (ingest.local_media).
        per video once both branches end: views/<vtag>/transcript-<rtag>.md (markers from this
        view; transcripts/<rtag>.md when there are no frames) + views/<vtag>/context.md, "done".
    all frames reached -> plan.build_plan(stage="frames") + write_plan; run.json frames_reached
    all done -> windows if windowed -> build_plan(stage="done") -> WFM - context done
    -> RunResult -> Progress.result() -> exit code (types.exit_code_for)
Every per-video stage transition: update VideoResult.stages + manifest stage (+save) +
write_run_json (so `wait` sees it) + one WFM line.

Stage values in VideoResult.stages: cache hits are "done" (the WFM line says "cached");
--audio-only -> video/frames "skipped"; --video-only -> audio/asr "skipped".
Non-fatal no_audio / no_video: status stays "done", code in VideoResult.warnings.

CPU backend rule (spec 4.1 review fix): with backend "cpu" and no explicit --jobs-cpu, the
CPU limiter drops to 1 concurrent ffmpeg/Pillow job while an ASR job runs, and ffmpeg is niced.

The only product-promo string literal in scripts/** is SETUP_TIP (TTY-only, spec 7 / test_no_promo).
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import contextlib
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import VERSION, cache, frames, ingest, plan, proc, sheets
from . import asr_client as asrc
from .asr_client import AsrWorker
from .progress import (
    TAG_STARTED,
    TAG_WAIT,
    Progress,
    format_json_line,
    last_lines_by_key,
)
from .types import (
    DEFAULT_JOBS_NET,
    DEFAULT_MAX_MINUTES,
    DEFAULT_WAIT_TIMEOUT_S,
    EXIT_ALL_FAILED,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_PREREQ,
    EXIT_USAGE,
    EXIT_WAIT_DEAD,
    EXIT_WAIT_TIMEOUT,
    MAX_IMAGE_SIDE,
    MAX_INPUTS,
    STAGE_FINAL,
    AsrJob,
    FailedEntry,
    MediaFile,
    Resolved,
    RunOptions,
    RunResult,
    VideoResult,
    WfmError,
    exit_code_for,
    manifest_stage_frames,
    manifest_stage_media,
    manifest_stage_transcript,
    parse_ts,
)

if TYPE_CHECKING:
    from typing import Self

SETUP_TIP = ("Ready. Try: /watch-for-me <link> --tldr. "
             "Tip: --save also keeps videos searchable in Deepmark (usedeepmark.com/?ref=watch-for-me).")
COOKIE_BROWSERS = ("chrome", "firefox", "safari", "edge", "brave", "chromium", "opera", "vivaldi")
WAIT_POLL_S = 0.5
KEY_LOCK_POLL_S = 0.5
CANCEL_GRACE_S = 5.0
LAUNCH_GRACE_S = 10.0
CHILDREN_FILE = "children.json"  # proc registry mirror: [[pid, spawn_ts], ...]
FRAMES_PARAMS_V = 1  # bump to invalidate every cached view
_LANG_RE = re.compile(r"^[a-z]{2}$")
_VTAG_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------
class UsageError(Exception):
    """-> one-line message on stderr, exit 2."""


class _Parser(argparse.ArgumentParser):
    """argparse that raises UsageError instead of printing usage + exiting."""

    def error(self, message: str) -> None:  # type: ignore[override]
        raise UsageError(f"{self.prog}: {message}")


def build_parser() -> argparse.ArgumentParser:
    """Subparsers exactly per spec 3:

    run INPUT... [--run-id ID] [--detach] [--hires] [--code] [--lang CODE] [--from T] [--to T]
        [--audio-only] [--video-only] [--cookies BROWSER] [--playlist N] [--max-minutes M]
        [--fresh] [--jobs-net N] [--jobs-cpu N] [--quiet]
    wait --run ID --until frames|done [--timeout 540]
    visual-put KEY --view VTAG --flags F [--from PATH]   (body on stdin, or from PATH)
    frame KEY --t SECONDS [--crop X,Y,W,H] [--width 1456]
    doctor [--json] [--quick]
    setup
    cache list | cache path [KEY] | cache clear (KEY | --all)
    cancel --run ID
    `--version` prints wfm.VERSION. argparse errors exit 2.
    The `run` subparser is exposed as parser.run_parser (main() parses it with
    parse_intermixed_args so flags may follow or sit between inputs).
    """
    p = _Parser(prog="watch.py", description="watch-for-me: local video understanding for agents")
    p.add_argument("--version", action="version", version=f"watch-for-me {VERSION}")
    sub = p.add_subparsers(dest="cmd", parser_class=_Parser, metavar="<command>")
    sub.required = True

    r = sub.add_parser("run", help="full pipeline")
    r.add_argument("inputs", nargs="+", metavar="INPUT", help="video link or local file (up to 10)")
    r.add_argument("--run-id", help="run id (default: generated; printed in WFM_STARTED)")
    r.add_argument("--detach", action="store_true", help="start in the background, print WFM_STARTED, exit")
    r.add_argument("--hires", action="store_true", help="1080p frames instead of 720p")
    r.add_argument("--code", action="store_true", help="code on screen: implies --hires, denser sheets")
    r.add_argument("--lang", metavar="XX", help="speech language (2-letter code), skips detection")
    r.add_argument("--from", dest="from_", metavar="T", help="start time (s, mm:ss or h:mm:ss)")
    r.add_argument("--to", metavar="T", help="end time (s, mm:ss or h:mm:ss)")
    r.add_argument("--audio-only", action="store_true", help="transcript only, no frames")
    r.add_argument("--video-only", action="store_true", help="frames only, no transcript")
    r.add_argument("--cookies", metavar="BROWSER", help=f"use this browser's cookies: {', '.join(COOKIE_BROWSERS)}")
    r.add_argument("--playlist", type=int, metavar="N", help="watch the first N entries of a playlist (<= 10)")
    r.add_argument("--max-minutes", type=int, default=DEFAULT_MAX_MINUTES, metavar="M",
                   help=f"refuse longer videos / ranges (default {DEFAULT_MAX_MINUTES})")
    r.add_argument("--fresh", action="store_true", help="ignore the cache and redo every stage")
    r.add_argument("--jobs-net", type=int, default=DEFAULT_JOBS_NET, metavar="N", help="parallel downloads")
    r.add_argument("--jobs-cpu", type=int, metavar="N", help="parallel ffmpeg/Pillow jobs (default cores/2)")
    r.add_argument("--quiet", action="store_true", help="no WFM lines on stdout (log only)")
    r.add_argument("--_detached", action="store_true", help=argparse.SUPPRESS)

    w = sub.add_parser("wait", help="block until a run reaches a stage")
    w.add_argument("--run", required=True)
    w.add_argument("--until", required=True, choices=("frames", "done"))
    w.add_argument("--timeout", type=float, default=float(DEFAULT_WAIT_TIMEOUT_S))

    v = sub.add_parser("visual-put", help="store the merged visual timeline (stdin or --from)")
    v.add_argument("key")
    v.add_argument("--view", required=True, help="view tag (last part of view_dir)")
    v.add_argument("--flags", default="", help="used subset of code,ask,steps, or none")
    v.add_argument("--from", dest="from_path", metavar="PATH",
                   help="read the timeline from this file instead of stdin (deleted after, when it "
                        "is inside the video's cache dir)")

    f = sub.add_parser("frame", help="full-res frame / crop from the cached video")
    f.add_argument("key")
    f.add_argument("--t", required=True, help="time (s, mm:ss or h:mm:ss)")
    f.add_argument("--crop", metavar="X,Y,W,H",
                   help="box in the pixels of the plain `frame KEY --t T` image (whatever --width is)")
    f.add_argument("--width", type=int, default=frames.ZOOM_DEFAULT_WIDTH,
                   help=f"output width, up to {MAX_IMAGE_SIDE} (default {frames.ZOOM_DEFAULT_WIDTH})")

    d = sub.add_parser("doctor", help="check prerequisites and cached models")
    d.add_argument("--json", action="store_true")
    d.add_argument("--quick", action="store_true")

    sub.add_parser("setup", help="prefetch this backend's speech models")

    c = sub.add_parser("cache", help="inspect / delete the cache")
    csub = c.add_subparsers(dest="cache_cmd", parser_class=_Parser, metavar="<list|path|clear>")
    csub.required = True
    csub.add_parser("list")
    cp = csub.add_parser("path")
    cp.add_argument("key", nargs="?")
    cc = csub.add_parser("clear")
    cc.add_argument("key", nargs="?")
    cc.add_argument("--all", action="store_true")

    k = sub.add_parser("cancel", help="stop a run and its ASR worker")
    k.add_argument("--run", required=True)

    p.run_parser = r  # type: ignore[attr-defined]
    return p


def _default_jobs_cpu() -> int:
    return max(2, (os.cpu_count() or 2) // 2)


def options_from_args(args: argparse.Namespace) -> RunOptions:
    """Validate `run` args -> RunOptions. Raises UsageError (exit 2) for: > 10 inputs,
    --audio-only with --video-only, --playlist outside 1..10, bad --from/--to (types.parse_ts),
    from >= to, --lang not 2 lowercase letters, --cookies not in COOKIE_BROWSERS,
    invalid --run-id. --code sets hires. jobs_cpu default max(2, cores // 2); backend
    from asr_client.select_backend(); run_id default cache.new_run_id()."""
    inputs = [s.strip() for s in args.inputs if s.strip()]
    if not inputs:
        raise UsageError("run: no input given")
    if len(inputs) > MAX_INPUTS:
        raise UsageError(f"run: at most {MAX_INPUTS} inputs per call (got {len(inputs)})")
    if args.audio_only and args.video_only:
        raise UsageError("run: --audio-only and --video-only are mutually exclusive")
    if args.playlist is not None and not 1 <= args.playlist <= MAX_INPUTS:
        raise UsageError(f"run: --playlist N must be 1..{MAX_INPUTS}")
    try:
        from_s = parse_ts(args.from_) if args.from_ else None
        to_s = parse_ts(args.to) if args.to else None
    except ValueError as e:
        raise UsageError(f"run: {e}") from None
    if from_s is not None and to_s is not None and from_s >= to_s:
        raise UsageError("run: --from must be before --to")
    lang = args.lang.strip().lower() if args.lang else None
    if lang is not None and not _LANG_RE.match(lang):
        raise UsageError("run: --lang takes a 2-letter ISO 639-1 code, e.g. en, fr, ja")
    cookies = args.cookies.strip().lower() if args.cookies else None
    if cookies is not None and cookies not in COOKIE_BROWSERS:
        raise UsageError(f"run: --cookies takes one of {', '.join(COOKIE_BROWSERS)}")
    run_id = args.run_id or cache.new_run_id()
    if not cache.valid_run_id(run_id):
        raise UsageError("run: --run-id must match [A-Za-z0-9_-]{1,64}")
    if args.max_minutes < 1:
        raise UsageError("run: --max-minutes must be >= 1")
    if args.jobs_net < 1 or (args.jobs_cpu is not None and args.jobs_cpu < 1):
        raise UsageError("run: --jobs-net / --jobs-cpu must be >= 1")
    try:
        backend = asrc.select_backend()
    except ValueError as e:
        raise UsageError(f"run: {e}") from None
    return RunOptions(
        inputs=inputs, run_id=run_id, detach=args.detach, hires=args.hires or args.code, code=args.code,
        lang=lang, from_s=from_s, to_s=to_s, audio_only=args.audio_only, video_only=args.video_only,
        cookies=cookies, playlist=args.playlist, max_minutes=args.max_minutes, fresh=args.fresh,
        jobs_net=args.jobs_net, jobs_cpu=args.jobs_cpu or _default_jobs_cpu(), quiet=args.quiet,
        backend=backend,  # type: ignore[arg-type]
    )


def _err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def main(argv: list[str]) -> int:
    """Parse, dispatch, return the exit code. UsageError -> 2; KeyboardInterrupt -> 130.
    Subcommand handlers below each return an int exit code."""
    parser = build_parser()
    try:
        if argv and argv[0] == "run":
            args = parser.run_parser.parse_intermixed_args(argv[1:])  # type: ignore[attr-defined]
            args.cmd = "run"
            args.raw_argv = argv[1:]
        else:
            args = parser.parse_args(argv)
        handler = {
            "run": cmd_run, "wait": cmd_wait, "visual-put": cmd_visual_put, "frame": cmd_frame,
            "doctor": cmd_doctor, "setup": cmd_setup, "cache": cmd_cache, "cancel": cmd_cancel,
        }[args.cmd]
        return handler(args)
    except UsageError as e:
        _err(str(e))
        return EXIT_USAGE
    except SystemExit as e:  # --help / --version
        return e.code if isinstance(e.code, int) else EXIT_OK
    except KeyboardInterrupt:
        proc.kill_children()
        return EXIT_INTERRUPTED


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------
def _missing_prereqs(opts: RunOptions) -> list[str]:
    tools = ["ffmpeg", "ffprobe"] + ([] if opts.video_only else ["uv"])
    return [t for t in tools if not proc.which(t)]


def cmd_run(args: argparse.Namespace) -> int:
    """`run`. Checks ffmpeg/ffprobe/uv presence first (exit 3 with hints on stderr).
    --detach -> detach(); else install_signal_handlers() and asyncio.run(run_pipeline(...)),
    Progress(log_path=runs/<id>/log). Last stdout line: WFM_RESULT."""
    from .doctor import install_hint

    opts = options_from_args(args)
    missing = _missing_prereqs(opts)
    if missing:
        for t in missing:
            _err(f"missing prerequisite: {t}. Install: {install_hint(t) or 'see README'}")
        return EXIT_PREREQ
    existing = cache.read_run_json(opts.run_id)
    if existing and existing.get("state") == "running" and cache.run_pid_alive(existing) \
            and existing.get("pid") != os.getpid():
        raise UsageError(f"run: run id {opts.run_id} is already running")
    if opts.detach and not args._detached:
        return detach(args.raw_argv, opts.run_id)

    detached = bool(args._detached)
    rdir = cache.run_dir(opts.run_id)
    proc.set_registry_file(rdir / CHILDREN_FILE)  # lets `cancel` reach children if we die
    progress = Progress(time.monotonic(), quiet=opts.quiet and not detached,
                        log_path=None if detached else rdir / "log")
    state: dict[str, Any] = {"detached": detached, "jobs_cpu_explicit": args.jobs_cpu is not None}

    async def _main() -> int:
        state["task"] = asyncio.current_task()
        install_signal_handlers(asyncio.get_running_loop(), state)
        try:
            result = await run_pipeline(opts, progress, state)
        except asyncio.CancelledError:
            return _finish_interrupted(opts, progress, state)
        progress.result(result.to_dict())
        return result.exit

    try:
        return asyncio.run(_main())
    except KeyboardInterrupt:
        return _finish_interrupted(opts, progress, state)


def _finish_interrupted(opts: RunOptions, progress: Progress, state: dict[str, Any]) -> int:
    w = state.get("worker")
    if w is not None:
        w.kill()
    proc.kill_children()
    rs: _Run | None = state.get("run")
    progress.event(None, "run", "error", code="interrupted")
    videos = rs.videos() if rs else []
    res = RunResult(opts.run_id, EXIT_INTERRUPTED, progress.elapsed(), opts.backend,
                    str(rs.plan_path) if rs and rs.plan_path.exists() else None, None, videos)
    if rs:
        rs.write(state="cancelled", exit_code=EXIT_INTERRUPTED, result=res.to_dict())
    progress.result(res.to_dict())
    return EXIT_INTERRUPTED


def watch_py_path() -> Path:
    """Absolute path of scripts/watch.py (for detach)."""
    return Path(__file__).resolve().parent.parent / "watch.py"


def detach(argv: list[str], run_id: str) -> int:
    """Fork a detached copy of this `run` (spec 3 --detach):
    [sys.executable, <abs watch.py>, "run", *argv without "--detach", "--run-id", run_id,
    "--_detached"] with start_new_session=True, stdin DEVNULL, stdout+stderr -> runs/<id>/log
    (append), env inherited. Writes a minimal run.json {pid, state:"running"} BEFORE printing
    so an immediate `wait` finds it. Prints `WFM_STARTED {"run_id":…,"pid":…}`; returns 0 in < 1 s."""
    rdir = cache.run_dir(run_id)
    child_args = [a for a in argv if a != "--detach"]
    cmd = [sys.executable, str(watch_py_path()), "run", *child_args, "--run-id", run_id, "--_detached"]
    kw: dict[str, Any] = {}
    if os.name == "posix":
        kw["start_new_session"] = True
    else:
        kw["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    # run.json exists before the child starts (pid null = launching; `wait` treats a
    # null pid younger than LAUNCH_GRACE_S as alive). The child always writes its own pid;
    # the parent fills it in only if the child has not written yet, so a fast (cached)
    # child's final state is never clobbered.
    stub = {
        "v": 1, "run_id": run_id, "pid": None, "worker_pid": None, "detached": True,
        "started": cache._now_iso(), "started_ts": time.time(), "inputs": [], "keys": [], "flags": {},
        "state": "running", "frames_reached": False, "exit": None, "videos": [], "result": None,
    }
    cache.write_run_json(run_id, stub)
    with open(rdir / "log", "ab") as log:
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                             close_fds=True, env=env, **kw)
    spawned = time.time()
    cur = cache.read_run_json(run_id)
    if cur is not None and cur.get("pid") is None:
        cache.write_run_json(run_id, dict(cur, pid=p.pid, pid_start=spawned))
    print(format_json_line(TAG_STARTED, {"run_id": run_id, "pid": p.pid, "log": str(rdir / "log")}), flush=True)
    return EXIT_OK


def install_signal_handlers(loop: asyncio.AbstractEventLoop, state: dict[str, Any]) -> None:
    """SIGINT/SIGTERM (and atexit): kill the ASR worker (AsrWorker.kill) and every registered
    child (wfm.proc.kill_children), then cancel the main task; cmd_run marks run.json state
    "cancelled" with exit 130, emits `WFM <t> - run error code=interrupted` and returns 130.
    Never signals our own process group."""

    def stop(*_: object) -> None:
        if state.get("stopping"):
            return
        state["stopping"] = True
        w = state.get("worker")
        if w is not None:
            w.kill()
        proc.kill_children()
        task = state.get("task")
        if task is not None and not task.done():
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop)
        except (NotImplementedError, RuntimeError, ValueError):  # Windows / non-main thread
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop))

    def _atexit() -> None:
        w = state.get("worker")
        if w is not None:
            with contextlib.suppress(Exception):
                w.kill()
        proc.kill_children()

    atexit.register(_atexit)


# --------------------------------------------------------------------------
# run: shared state
# --------------------------------------------------------------------------
class CpuLimiter:
    """Async limiter for ffmpeg/Pillow jobs. Capacity `n`, or 1 while an ASR job runs when
    `asr_exclusive` (cpu backend with the default --jobs-cpu; spec 4.1 review fix)."""

    def __init__(self, n: int, *, asr_exclusive: bool) -> None:
        self.n = max(1, n)
        self.asr_exclusive = asr_exclusive
        self.active = 0
        self.asr_jobs = 0
        self._cond = asyncio.Condition()

    def capacity(self) -> int:
        return 1 if self.asr_exclusive and self.asr_jobs > 0 else self.n

    async def __aenter__(self) -> Self:
        async with self._cond:
            await self._cond.wait_for(lambda: self.active < self.capacity())
            self.active += 1
        return self

    async def __aexit__(self, *exc: object) -> None:
        async with self._cond:
            self.active -= 1
            self._cond.notify_all()

    async def asr_begin(self) -> None:
        async with self._cond:
            self.asr_jobs += 1

    async def asr_end(self) -> None:
        async with self._cond:
            self.asr_jobs -= 1
            self._cond.notify_all()


@dataclass
class _Run:
    """Per-run mutable state shared by every per-input task (event-loop thread only)."""

    opts: RunOptions
    root: Path
    rdir: Path
    progress: Progress
    detached: bool
    started: str = field(default_factory=lambda: cache._now_iso())
    started_ts: float = field(default_factory=time.time)
    slots: list[list[VideoResult]] = field(default_factory=list)
    resolved: list[bool] = field(default_factory=list)
    worker: AsrWorker | None = None
    frames_reached: bool = False
    finals: dict[int, dict[str, Any]] = field(default_factory=dict)  # id(video) -> plan entry
    job_seq: int = 0

    @property
    def plan_path(self) -> Path:
        return self.rdir / "plan.json"

    def videos(self) -> list[VideoResult]:
        return [v for slot in self.slots for v in slot]

    def write(self, *, state: str = "running", exit_code: int | None = None,
              result: dict[str, Any] | None = None) -> None:
        vids = self.videos()
        cache.write_run_json(self.opts.run_id, {
            "v": 1, "run_id": self.opts.run_id, "pid": os.getpid(), "pid_start": _self_start(),
            "worker_pid": self.worker.pid if self.worker else None, "detached": self.detached,
            "started": self.started, "started_ts": self.started_ts, "inputs": self.opts.inputs,
            "keys": [v.key for v in vids if v.key], "flags": self.opts.flags_dict(), "state": state,
            "frames_reached": self.frames_reached, "exit": exit_code,
            "videos": [v.to_dict() for v in vids], "result": result,
        }, self.root)

    def changed(self) -> None:
        """Every transition: maybe publish the frames-stage plan, then rewrite run.json."""
        if not self.frames_reached and all(self.resolved) and all(
                v.status == "error" or v.stages.get("frames") in STAGE_FINAL for v in self.videos()):
            self.frames_reached = True
            entries = self.plan_entries()
            p = plan.build_plan(self.opts.run_id, "frames", self.opts.code, entries)
            p["light_sheets"] = _light_sheets(entries)
            plan.write_plan(self.rdir, p)
            self.progress.event(None, "frames", "done", videos=len(self.videos()),
                                batches=len(p["visual_batches"]), inline=len(p["inline_sheets"]))
        self.write()

    def plan_entries(self) -> list[dict[str, Any]]:
        out = []
        for v in self.videos():
            if v.status == "error" or not v.key or not v.view_dir:
                continue
            e = self.finals.get(id(v)) or {}
            fj = e.get("frames_json")
            if fj is None and v.frames_json:
                fj = cache.read_json(v.frames_json)
            out.append({"key": v.key, "view_dir": v.view_dir, "frames_json": fj,
                        "words": e.get("words"), "windows": e.get("windows") or []})
        return out


_SELF_START: list[float | None] = []


def _self_start() -> float | None:
    """This process's start time (cache.process_start, computed once): run.json `pid_start`."""
    if not _SELF_START:
        _SELF_START.append(cache.process_start(os.getpid()))
    return _SELF_START[0]


def _words(transcript: dict[str, Any] | None) -> int:
    if not transcript:
        return 0
    return sum(asrc.ac.count_words(str(s.get("text", ""))) for s in transcript.get("segments") or [])


def _ytdlp_version() -> str | None:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("yt-dlp")
    except PackageNotFoundError:
        return None


def _needs_asr_work(inp: str, opts: RunOptions, root: Path) -> bool:
    """Cheap pre-check (no network) used to decide whether to spawn the worker at run start."""
    if opts.video_only:
        return False
    if opts.fresh:
        return True
    try:
        key = cache.index_get(cache.normalize_input(inp), root)
    except (FileNotFoundError, OSError):
        return False  # fails at resolve
    if not key:
        return True
    kp = cache.key_paths(key, root)
    man = cache.load_manifest(kp)
    meta = cache.read_json(kp.meta, {}) or {}
    if meta.get("has_audio") is False:
        return False
    rng = cache.range_tag(opts.from_s, opts.to_s, meta.get("duration"))
    for r in {rng, "full"}:
        rtag = cache.transcript_tag(r, opts.lang)
        ph = cache.params_hash(_tparams(opts, r))
        if cache.stage_fresh(man, manifest_stage_transcript(rtag), ph) and kp.transcript_json(rtag).is_file():
            return False
    return True


def _tparams(opts: RunOptions, rng: str) -> dict[str, Any]:
    full = rng == "full"
    return asrc.transcript_params(opts.backend, opts.lang, None if full else opts.from_s,
                                  None if full else opts.to_s)


def _protect_keys(opts: RunOptions, root: Path) -> set[str]:
    keys = set()
    for inp in opts.inputs:
        try:
            k = cache.index_get(cache.normalize_input(inp), root)
        except (FileNotFoundError, OSError):
            continue
        if k:
            keys.add(k)
    return keys


async def run_pipeline(opts: RunOptions, progress: Progress, state: dict[str, Any] | None = None) -> RunResult:
    """The whole DAG in the module docstring. Never raises for per-video failures (they
    become VideoResult errors); raises only on programming errors / cancellation."""
    state = state if state is not None else {}
    root = cache.cache_root()
    rdir = cache.run_dir(opts.run_id, root)
    for stale in ("plan.json",):
        (rdir / stale).unlink(missing_ok=True)
    rs = _Run(opts, root, rdir, progress, bool(state.get("detached")))
    state["run"] = rs
    rs.slots = [[VideoResult(input=inp)] for inp in opts.inputs]
    rs.resolved = [False] * len(opts.inputs)
    for v in rs.videos():
        v.stages["meta"] = "running"

    cache.prune_runs(root=root)
    evicted = cache.evict_lru(_protect_keys(opts, root), root=root)
    rs.write()
    kv: dict[str, Any] = {"run_id": opts.run_id, "inputs": len(opts.inputs), "backend": opts.backend}
    if evicted:
        kv["evicted"] = len(evicted)
    progress.event(None, "run", "start", **kv)

    if not opts.video_only:
        rs.worker = AsrWorker(opts.backend, root / cache.ASR_LOCK, log_path=rdir / "asr.log",
                              on_model=lambda ev: _on_model(progress, ev))
        state["worker"] = rs.worker
        if any(_needs_asr_work(inp, opts, root) for inp in opts.inputs):
            await rs.worker.start()
            rs.write()

    net = asyncio.Semaphore(opts.jobs_net)
    cpu = CpuLimiter(opts.jobs_cpu,
                     asr_exclusive=opts.backend == "cpu" and not state.get("jobs_cpu_explicit", False))
    try:
        await asyncio.gather(*(_process_one(i, inp, rs, net, cpu) for i, inp in enumerate(opts.inputs)))
        rs.changed()  # guarantees the frames-stage plan exists even for all-failed runs
        final = _finalize_run(rs)
    finally:
        if rs.worker is not None:
            if state.get("stopping"):
                rs.worker.kill()
            else:
                await rs.worker.close()
    videos = rs.videos()
    code = exit_code_for(videos) if videos else EXIT_ALL_FAILED
    result = RunResult(opts.run_id, code, progress.elapsed(), opts.backend, str(rs.plan_path),
                       final.get("mode"), videos)
    rs.write(state="done", exit_code=code, result=result.to_dict())
    return result


def _on_model(progress: Progress, ev: dict[str, Any]) -> None:
    """Worker EV_MODEL -> `WFM <t> - model start|progress|done model=<id> [got_mb=] mb=`
    (progress arrives ~every 60 s while a model downloads, so `wait` shows movement)."""
    status = {"downloading": "start", "progress": "progress"}.get(str(ev.get("status")), "done")
    kv: dict[str, Any] = {"model": ev.get("model")}
    if status == "progress" and ev.get("got_mb") is not None:
        kv["got_mb"] = ev.get("got_mb")
    if ev.get("mb") is not None:
        kv["mb"] = ev.get("mb")
    progress.event(None, "model", status, **kv)


def _light_sheets(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Q8 light visuals (--tldr): per video, abs paths of frames.json `light_sheets`."""
    return [{"key": e["key"], "sheets": [str(Path(e["view_dir"]) / s["file"]) for s in
                                         (e["frames_json"] or {}).get("light_sheets") or []]}
            for e in entries if (e.get("frames_json") or {}).get("light_sheets")]


def _finalize_run(rs: _Run) -> dict[str, Any]:
    """Windows (windowed mode only), final plan.json, `WFM - context done`."""
    entries = rs.plan_entries()
    total_words = sum(int(e.get("words") or 0) for e in entries)
    mode = plan.choose_mode(plan.transcript_tokens_est(total_words))
    if mode == "windowed":
        for v in rs.videos():
            fin = rs.finals.get(id(v))
            if not fin or not fin.get("transcript") or v.status == "error":
                continue
            fin["windows"] = _write_windows(v, fin)
        entries = rs.plan_entries()
    p = plan.build_plan(rs.opts.run_id, "done", rs.opts.code, entries)
    p["light_sheets"] = _light_sheets(entries)
    plan.write_plan(rs.rdir, p)
    rs.progress.event(None, "context", "done", mode=p["mode"], tokens=p["transcript_tokens_est"],
                      batches=len(p["visual_batches"]), windows=len(p["transcript_windows"]))
    return p


def _write_windows(v: VideoResult, fin: dict[str, Any]) -> list[dict[str, Any]]:
    t = fin["transcript"]
    meta = fin["meta"]
    segs = t.get("segments") or []
    lo = fin["from_s"] or 0.0
    hi = fin["to_s"] or meta.get("duration") or (segs[-1]["t1"] if segs else lo)
    chapters = [float(c.get("start_time") or 0) for c in meta.get("chapters") or [] if isinstance(c, dict)]
    anchors = chapters if chapters else [m[1] for m in fin["markers"] or []]
    bounds = plan.window_bounds(float(lo), float(hi), anchors)
    view = cache.ViewPaths(Path(v.view_dir or ""), fin["vtag"])
    return plan.write_windows(view, t, fin["rtag"], bounds, fin["markers"])


async def _process_one(i: int, inp: str, rs: _Run, net: asyncio.Semaphore, cpu: CpuLimiter) -> None:
    """Resolve one CLI input (maybe a playlist) and process every resulting video."""
    placeholder = rs.slots[i][0]
    try:
        items = await ingest.resolve(inp, rs.opts, net)
    except WfmError as e:
        placeholder.status = "error"
        placeholder.error = e.to_dict()
        placeholder.stages["meta"] = "error"
        for s in ("audio", "video", "frames", "asr"):
            placeholder.stages[s] = "skipped"
        rs.resolved[i] = True
        rs.progress.event(None, "meta", "error", code=e.code, input=_short(inp))
        rs.changed()
        return
    except Exception as e:  # noqa: BLE001 (a bug in resolve must not take the other inputs down)
        placeholder.status = "error"
        placeholder.error = WfmError("download_failed", f"{type(e).__name__}: {e}").to_dict()
        placeholder.stages["meta"] = "error"
        for s in ("audio", "video", "frames", "asr"):
            placeholder.stages[s] = "skipped"
        rs.resolved[i] = True
        rs.progress.event(None, "meta", "error", code="download_failed", input=_short(inp))
        rs.changed()
        return
    slot: list[VideoResult] = []
    jobs = []
    for item in items:
        if isinstance(item, FailedEntry):
            v = VideoResult(input=item.input, status="error", error=item.error.to_dict(),
                            parent_input=item.parent_input)
            v.stages.update(meta="error", audio="skipped", video="skipped", frames="skipped", asr="skipped")
            rs.progress.event(None, "meta", "error", code=item.error.code, input=_short(item.input))
            slot.append(v)
            continue
        v = VideoResult(input=item.input, key=item.key, parent_input=item.parent_input)
        slot.append(v)
        jobs.append((item, v))
    rs.slots[i] = slot
    rs.resolved[i] = True
    for item, v in jobs:
        _fill_meta(v, item)
        kv: dict[str, Any] = {}
        if isinstance(item.meta.get("duration"), (int, float)):
            kv["dur"] = round(float(item.meta["duration"]))
        if item.is_local:
            kv["local"] = True
        elif not item.index_hit and not ingest.js_runtime_available():
            kv["warn"] = "no_js_runtime"
        rs.progress.event(item.key, "meta", "cached" if item.index_hit else "done", **kv)
    rs.changed()
    await asyncio.gather(*(process_input(item, rs.opts, video=v, worker=rs.worker, net=net, cpu=cpu,
                                         progress=rs.progress, on_change=rs.changed, run=rs)
                           for item, v in jobs))


def _short(s: str, n: int = 80) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _fill_meta(v: VideoResult, item: Resolved) -> None:
    m = item.meta
    v.title = m.get("title")
    v.uploader = m.get("uploader") or m.get("channel")
    v.duration = m.get("duration")
    v.webpage_url = m.get("webpage_url")
    v.is_local = item.is_local
    v.stages["meta"] = "done"


class _BranchError(Exception):
    """A fatal WfmError tagged with the video stage it happened in."""

    def __init__(self, stage: str, err: WfmError) -> None:
        super().__init__(str(err))
        self.stage = stage
        self.err = err


async def _key_lock(kp: cache.KeyPaths, progress: Progress) -> cache.FileLock:
    kp.dir.mkdir(parents=True, exist_ok=True)
    lk = cache.FileLock(kp.run_lock)
    announced = False
    while not lk.acquire(blocking=False):
        if not announced:
            progress.event(kp.key, "meta", "progress", wait="run_lock")
            announced = True
        await asyncio.sleep(KEY_LOCK_POLL_S)
    return lk


async def process_input(item: Resolved, opts: RunOptions, *, video: VideoResult, worker: AsrWorker | None,
                        net: asyncio.Semaphore, cpu: CpuLimiter, progress: Progress,
                        on_change: Callable[[], None], run: _Run | None = None) -> None:
    """Both branches for one resolved video, under its run.lock. Mutates `video` (stages,
    paths, error/warnings, visual_md/visual_cached) and calls on_change() after every
    transition (-> run.json + frames-plan check). Catches WfmError per branch: fatal ->
    video.status "error" (other branch cancelled); non-fatal -> warning, branch "skipped".
    Cache: a stage is reused when cache.stage_fresh(manifest, stage, params_hash) and not
    opts.fresh. `run` (cli-internal) receives the per-video plan entry for the final plan."""
    kp = cache.key_paths(item.key)
    lock = await _key_lock(kp, progress)
    try:
        job = _VideoJob(item, opts, video, worker, net, cpu, progress, on_change, kp, run)
        await job.execute()
    finally:
        lock.release()


class _VideoJob:
    """The per-video DAG (audio + video branches) plus per-video finalize."""

    def __init__(self, item: Resolved, opts: RunOptions, video: VideoResult, worker: AsrWorker | None,
                 net: asyncio.Semaphore, cpu: CpuLimiter, progress: Progress,
                 on_change: Callable[[], None], kp: cache.KeyPaths, run: _Run | None) -> None:
        self.item, self.opts, self.v, self.worker = item, opts, video, worker
        self.net, self.cpu, self.progress, self.on_change = net, cpu, progress, on_change
        self.kp, self.run = kp, run
        self.key = item.key
        self.meta = item.meta
        dur = self.meta.get("duration")
        self.duration: float | None = float(dur) if isinstance(dur, (int, float)) and dur > 0 else None
        self.rng = cache.range_tag(opts.from_s, opts.to_s, self.duration)
        self.rtag = cache.transcript_tag(self.rng, opts.lang)
        self.vtag = cache.view_tag(self.rng, opts.resolution)
        self.view = kp.view(self.vtag)
        self.man: dict[str, Any] = {}
        self.transcript: dict[str, Any] | None = None
        self.frames_json: dict[str, Any] | None = None
        self.shared: dict[str, asyncio.Task[tuple[MediaFile, bool]]] = {}
        self.cur = {"audio": "audio", "video": "video"}

    # -- manifest ---------------------------------------------------------
    def _load_manifest(self) -> None:
        source = {"input": self.item.input, "webpage_url": self.meta.get("webpage_url"),
                  "extractor": self.meta.get("extractor_key"), "id": self.meta.get("id"),
                  "local_path": self.meta.get("local_path")}
        man = cache.load_manifest(self.kp) or cache.new_manifest(self.key, source)
        if self.opts.fresh:
            man["stages"] = {k: s for k, s in man.get("stages", {}).items() if k == "meta"}
            man["media"] = {}
        man["source"] = {**man.get("source", {}), **{k: v for k, v in source.items() if v is not None}}
        from . import VERSION as _V

        man["skill_version"] = _V
        tools = man.setdefault("tools", {})
        tools["backend"] = self.opts.backend
        ffv = proc.tool_version("ffmpeg")
        if ffv:
            tools["ffmpeg"] = ffv
        if not self.item.is_local:
            yv = _ytdlp_version()
            if yv:
                tools["yt_dlp"] = yv
        cache.set_stage(man, "meta", "done")
        self.man = man
        self.save()

    def save(self) -> None:
        cache.save_manifest(self.kp, self.man)

    def stage(self, name: str, status: str) -> None:
        self.v.stages[name] = status
        self.on_change()

    # -- media --------------------------------------------------------------
    async def media(self, kind: str) -> tuple[MediaFile, bool]:
        """(MediaFile, cached). kind "muxed" is shared by both branches (one task)."""
        if self.item.is_local:
            return ingest.local_media(self.item), True
        if kind not in self.shared:
            self.shared[kind] = asyncio.ensure_future(self._download(kind))
        return await asyncio.shield(self.shared[kind])

    async def _download(self, kind: str) -> tuple[MediaFile, bool]:
        stage = manifest_stage_media(kind)
        rel = self.man.get("media", {}).get(kind)
        if not self.opts.fresh and rel and cache.stage_fresh(self.man, stage) and (self.kp.dir / rel).is_file():
            p = self.kp.dir / rel
            fmt = (self.man["stages"][stage] or {}).get("format_id")
            return MediaFile(str(p), kind, fmt, round(p.stat().st_size / 1e6, 2)), True
        cache.set_stage(self.man, stage, "running")
        self.save()
        try:
            mf = await ingest.download(self.kp, self.item, kind, self.opts, self.net)
        except WfmError as e:
            cache.set_stage(self.man, stage, "error", code=e.code)
            cache.add_error(self.man, stage, e.to_dict())
            self.save()
            raise
        try:
            rel = str(Path(mf.path).resolve().relative_to(self.kp.dir.resolve()))
        except ValueError:
            rel = mf.path
        self.man.setdefault("media", {})[kind] = rel
        cache.set_stage(self.man, stage, "done", format_id=mf.format_id, mb=mf.mb)
        self.save()
        return mf, False

    def _media_event(self, stage: str, mf: MediaFile, cached: bool) -> None:
        kv: dict[str, Any] = {"mb": mf.mb}
        if mf.format_id:
            kv["fmt"] = mf.format_id
        if mf.kind == "muxed":
            kv["muxed"] = True
        if mf.kind == "local":
            kv["src"] = "local"
        self.progress.event(self.key, stage, "cached" if cached and mf.kind != "local" else "done", **kv)

    # -- audio branch --------------------------------------------------------
    async def audio_branch(self, kind: str | None) -> None:
        if kind is None:
            self.v.stages["audio"] = self.v.stages["asr"] = "skipped"
            self.progress.event(self.key, "audio", "skipped")
            self.on_change()
            return
        self.cur["audio"] = "audio"
        if self.item.is_local and self.meta.get("has_audio") is False:
            raise WfmError("no_audio", "the file has no audio stream")
        self.stage("audio", "running")
        mf, cached = await self.media(kind)
        if self.meta.get("has_audio") is False:
            raise WfmError("no_audio", "the media has no audio stream")
        self.v.stages["audio"] = "done"
        self._media_event("audio", mf, cached)
        self.on_change()
        self.cur["audio"] = "asr"
        await self.asr(mf)

    async def asr(self, mf: MediaFile) -> None:
        opts = self.opts
        tparams = _tparams(opts, self.rng)
        ph = cache.params_hash(tparams)
        stage = manifest_stage_transcript(self.rtag)
        tj = self.kp.transcript_json(self.rtag)
        if not opts.fresh and cache.stage_fresh(self.man, stage, ph) and tj.is_file():
            t = asrc.load_transcript(self.kp, self.rtag)
            if t is not None:
                self._set_transcript(t, "cached", {"words": _words(t)})
                return
        full_rtag = cache.transcript_tag("full", opts.lang)
        full_ph = cache.params_hash(_tparams(opts, "full"))
        if (self.rng != "full" and not opts.fresh
                and cache.stage_fresh(self.man, manifest_stage_transcript(full_rtag), full_ph)
                and self.kp.transcript_json(full_rtag).is_file()):
            full = asrc.load_transcript(self.kp, full_rtag)
            if full is not None:
                lo = opts.from_s or 0.0
                hi = opts.to_s if opts.to_s is not None else (self.duration or float("inf"))
                t = asrc.slice_transcript(full, lo, hi, self.key)
                cache.atomic_write_json(tj, t)
                cache.set_stage(self.man, stage, "done", params_hash=ph, sliced_from=full_rtag)
                self.save()
                self._set_transcript(t, "done", {"sliced": True, "words": _words(t)})
                return
        if self.worker is None:
            raise WfmError("asr_failed", "no ASR worker (internal error)")
        self.stage("asr", "running")
        cache.set_stage(self.man, stage, "running", params_hash=ph)
        self.save()
        await self.worker.start()
        if self.run is not None:
            self.run.write()  # worker_pid
            self.run.job_seq += 1
            seq = self.run.job_seq
        else:
            seq = 1
        job = AsrJob(job_id=f"j{seq}", key=self.key, audio=mf.path, from_s=opts.from_s, to_s=opts.to_s,
                     lang=opts.lang)
        await self.cpu.asr_begin()
        try:
            t = await asrc.transcribe(
                self.worker, self.kp, job=job, rtag=self.rtag, duration=self.duration or 0.0,
                backend=opts.backend,
                on_progress=lambda pct: self.progress.event(self.key, "asr", "progress", pct=pct))
        except WfmError as e:
            cache.set_stage(self.man, stage, "error", params_hash=ph, code=e.code)
            cache.add_error(self.man, stage, e.to_dict())
            self.save()
            raise
        finally:
            await self.cpu.asr_end()
        self.man.setdefault("tools", {}).update(self.worker.versions or {})
        cache.set_stage(self.man, stage, "done", params_hash=ph)
        self.save()
        st = t.get("stats") or {}
        kv: dict[str, Any] = {"engines": asrc.engines_summary(t)}
        if st.get("speed_x"):
            kv["speed"] = f"{st['speed_x']}x"
        kv["words"] = st.get("words", _words(t))
        self._set_transcript(t, "done", kv)

    def _set_transcript(self, t: dict[str, Any], status: str, kv: dict[str, Any]) -> None:
        self.transcript = t
        self.v.transcript_json = str(self.kp.transcript_json(self.rtag))
        self.v.stages["asr"] = "done"
        self.progress.event(self.key, "asr", status, **kv)  # before on_change: run-level lines follow
        self.on_change()

    # -- video branch -------------------------------------------------------
    async def video_branch(self, kind: str | None) -> None:
        if kind is None:
            self.v.stages["video"] = self.v.stages["frames"] = "skipped"
            if not self.opts.audio_only:
                raise WfmError("no_video", "the source has no video")
            self.progress.event(self.key, "video", "skipped")
            self.on_change()
            return
        self.cur["video"] = "video"
        if self.item.is_local and self.meta.get("has_video") is False:
            raise WfmError("no_video", "the file has no video stream")
        self.stage("video", "running")
        mf, cached = await self.media(kind)
        if self.meta.get("has_video") is False:
            raise WfmError("no_video", "the media has no video stream")
        self.v.stages["video"] = "done"
        self._media_event("video", mf, cached)
        self.on_change()
        self.cur["video"] = "frames"
        await self.frames(mf)

    def _frames_hash(self, hires: bool) -> str:
        fp = frames.frames_params(self.duration or 0.0, hires=hires, from_s=self.opts.from_s, to_s=self.opts.to_s)
        return cache.params_hash({"frames": fp.to_dict(), "sheet_w": sheets.sheet_width_env(), "hires": hires,
                                  "v": FRAMES_PARAMS_V})

    async def frames(self, mf: MediaFile) -> None:
        opts = self.opts
        hires = opts.resolution == 1080
        ph = self._frames_hash(hires)
        stage = manifest_stage_frames(self.vtag)
        if not opts.fresh and cache.stage_fresh(self.man, stage, ph) and self.view.frames_json.is_file():
            fj = frames.load_frames_json(self.view)
            if fj is not None:
                self._set_frames(fj, "cached", self._frames_kv(fj))
                return
        # New tiles renumber the segments: a stored visual.md (#n = old tiles) no longer matches.
        self.view.visual_md.unlink(missing_ok=True)
        self.v.visual_md, self.v.visual_cached = None, False
        self.stage("frames", "running")
        cache.set_stage(self.man, stage, "running", params_hash=ph)
        self.save()
        duration = self.duration
        if duration is None:
            duration = await ingest.ffprobe_duration(mf.path) or 0.0
        nice = opts.backend == "cpu"
        chapters = self.meta.get("chapters")
        try:
            async with self.cpu:
                out = await asyncio.to_thread(frames.extract_segments, mf.path, self.view, duration=duration,
                                              hires=hires, from_s=opts.from_s, to_s=opts.to_s, nice=nice)
                sh, light = await asyncio.to_thread(_render_sheets, out, self.view.dir, hires, chapters)
                await asyncio.to_thread(frames.write_frames_json, self.view, out, sh, light=light)
        except WfmError as e:
            cache.set_stage(self.man, stage, "error", params_hash=ph, code=e.code)
            cache.add_error(self.man, stage, e.to_dict())
            self.save()
            raise
        fj = frames.load_frames_json(self.view)
        if fj is None:
            raise WfmError("frames_failed", "frames.json missing after extraction")
        cache.set_stage(self.man, stage, "done", params_hash=ph, segs=len(fj.get("segments") or []),
                        sheets=len(fj.get("sheets") or []))
        self.save()
        self._set_frames(fj, "done", {**self._frames_kv(fj), "extract_s": round(out.extract_s, 1)})

    @staticmethod
    def _frames_kv(fj: dict[str, Any]) -> dict[str, Any]:
        shs = fj.get("sheets") or []
        return {"segs": len(fj.get("segments") or []), "sheets": len(shs),
                "grid": shs[0].get("grid") if shs else "-"}

    def _set_frames(self, fj: dict[str, Any], status: str, kv: dict[str, Any]) -> None:
        self.frames_json = fj
        self.v.frames_json = str(self.view.frames_json)
        self.v.sheets = [str(self.view.dir / s["file"]) for s in fj.get("sheets") or []]
        self.v.stages["frames"] = "done"
        self.progress.event(self.key, "frames", status, **kv)
        self.on_change()

    # -- orchestration -------------------------------------------------------
    async def _guard(self, branch: str, coro: Any, stages: tuple[str, str]) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except WfmError as e:
            if e.fatal:
                raise _BranchError(self.cur[branch], e) from e
            self.v.warnings.append(e.to_dict())
            for s in stages:
                if self.v.stages.get(s) not in STAGE_FINAL:
                    self.v.stages[s] = "skipped"
            self.progress.event(self.key, stages[1] if self.cur[branch] == stages[1] else stages[0],
                                "skipped", code=e.code)
            self.on_change()
        except Exception as e:  # bug in a stage module: fail this video, not the run
            code = {"audio": "download_failed", "asr": "asr_failed", "video": "download_failed",
                    "frames": "frames_failed"}[self.cur[branch]]
            raise _BranchError(self.cur[branch], WfmError(code, f"{type(e).__name__}: {e}")) from e

    async def execute(self) -> None:
        v = self.v
        self._load_manifest()
        v.view_dir = str(self.view.dir)
        if self.view.visual_md.is_file() and not self.opts.fresh:
            v.visual_md = str(self.view.visual_md)
            v.visual_cached = True
        has_video = self.meta.get("has_video") is not False
        mp = ingest.media_plan(self.item.split_formats, self.opts, has_video=has_video)
        if self.item.is_local:
            mp = {"audio": None if self.opts.video_only else "local",
                  "video": None if self.opts.audio_only else "local"}
        tasks = [asyncio.ensure_future(self._guard("audio", self.audio_branch(mp["audio"]), ("audio", "asr"))),
                 asyncio.ensure_future(self._guard("video", self.video_branch(mp["video"]), ("video", "frames")))]
        fatal: _BranchError | None = None
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for t in done:
                exc = t.exception()
                if isinstance(exc, _BranchError):
                    fatal = exc
                elif exc is not None:
                    raise exc
            if fatal is not None:
                for t in pending:
                    t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()
            for t in self.shared.values():
                t.cancel()
            raise
        finally:
            for t in self.shared.values():
                if not t.done():
                    t.cancel()
            if not self.item.is_local:
                ingest.drop_info_json(self.kp)
        if fatal is not None:
            self._fail(fatal)
            return
        self._finalize()

    def _fail(self, be: _BranchError) -> None:
        v = self.v
        v.status = "error"
        v.error = be.err.to_dict()
        for s in ("audio", "asr", "video", "frames"):
            if s == be.stage:
                v.stages[s] = "error"
            elif v.stages.get(s) not in STAGE_FINAL:
                v.stages[s] = "skipped"
        cache.add_error(self.man, be.stage, be.err.to_dict())
        self.save()
        self.progress.event(self.key, be.stage, "error", code=be.err.code)
        self.on_change()

    def _finalize(self) -> None:
        """The transcript .md (per view when it carries this view's tile markers, so a 1080p
        run never rewrites the markers a 720p session is reading) + context.md; status done."""
        v = self.v
        markers = plan.markers_from_frames(self.frames_json) if self.frames_json else None
        if self.transcript is not None:
            v.transcript_md = str(plan.write_transcript_md(self.kp, self.rtag, self.transcript, markers,
                                                           self.view if markers else None))
        paths = {"transcript": v.transcript_md, "frames_json": v.frames_json,
                 "sheets_dir": str(self.view.sheets_dir) if v.sheets else None}
        text = plan.render_context_md(self.meta, self.vtag, self.transcript, self.frames_json, paths)
        v.context_md = str(plan.write_context_md(self.view, text))
        v.status = "done"
        if self.run is not None:
            self.run.finals[id(v)] = {
                "frames_json": self.frames_json, "words": _words(self.transcript), "windows": [],
                "transcript": self.transcript, "markers": markers, "meta": self.meta, "rtag": self.rtag,
                "vtag": self.vtag, "from_s": self.opts.from_s, "to_s": self.opts.to_s,
            }
        self.on_change()


def _render_sheets(out: Any, view_dir: Path, hires: bool, chapters: Any) -> tuple[list[Any], list[Any]]:
    sh = sheets.make_sheets(out.segments, view_dir, src_w=out.src_w, src_h=out.src_h, hires=hires)
    light: list[Any] = []
    if hasattr(sheets, "make_light_sheets"):
        light = sheets.make_light_sheets(out.segments, view_dir, src_w=out.src_w, src_h=out.src_h,
                                         hires=hires, chapters=chapters)
    return sh, light


# --------------------------------------------------------------------------
# wait / cancel
# --------------------------------------------------------------------------
def cmd_wait(args: argparse.Namespace) -> int:
    """Poll runs/<id>/run.json every WAIT_POLL_S until:
      --until frames: run.json frames_reached, or state != "running"
      --until done:   state != "running"
    Exit 0 when reached (regardless of per-video failures), 6 on --timeout with the pid
    alive, 7 when the pid is dead and not reached (state set to "crashed"; later waits on a
    crashed run exit 7 too), 130 when the run was cancelled / interrupted before the stage,
    2 for an unknown run id. "Alive" is pid-reuse safe (cache.run_pid_alive). Prints ONE line:
    WFM_WAIT {"v":1,"run_id","until","reached":bool,"exit":<this exit>,"alive":bool,
      "state","run_exit":int|null,"elapsed_s":<since run start>,
      "plan":<plan.json dict or null>,
      "videos":[VideoResult dict + "last":<last WFM line for its key or null>],
      "last_run_line":<last "-" line or null>, "log":<abs runs/<id>/log>,
      "result":<final WFM_RESULT dict when the run finished, else null>}
    Last lines come from progress.last_lines_by_key(runs/<id>/log).
    """
    rid = args.run
    if not cache.valid_run_id(rid):
        raise UsageError("wait: invalid run id")
    root = cache.cache_root()
    rj = cache.read_run_json(rid, root)
    if rj is None:
        _err(f"wait: unknown run id {rid}")
        return EXIT_USAGE
    deadline = time.monotonic() + max(0.0, args.timeout)
    while True:
        rj = cache.read_run_json(rid, root) or rj
        st = rj.get("state")
        frames_ok = args.until == "frames" and bool(rj.get("frames_reached"))
        reached = st != "running" or frames_ok
        alive = _run_alive(rj)
        if reached and st in ("cancelled", "crashed") and not frames_ok:
            # cancelled / Ctrl-C / crash before the stage: not a success
            code = EXIT_INTERRUPTED if st == "cancelled" else EXIT_WAIT_DEAD
            break
        if reached:
            code = EXIT_OK
            break
        if not alive:
            again = cache.read_run_json(rid, root) or rj  # the run may have finished just now
            if again.get("state") != "running":
                rj = again
                continue
            rj = dict(again, state="crashed")
            cache.write_run_json(rid, rj, root)
            code = EXIT_WAIT_DEAD
            break
        if time.monotonic() >= deadline:
            code = EXIT_WAIT_TIMEOUT
            break
        time.sleep(WAIT_POLL_S)
    rdir = root / cache.RUNS_DIR / rid
    log = rdir / "log"
    last = last_lines_by_key(log)
    started = rj.get("started_ts")
    videos = [dict(v, last=last.get(v.get("key") or "")) for v in rj.get("videos") or []]
    obj = {
        "v": 1, "run_id": rid, "until": args.until, "reached": code == EXIT_OK, "exit": code,
        "alive": _run_alive(rj), "state": rj.get("state"), "run_exit": rj.get("exit"),
        "elapsed_s": round(time.time() - started, 2) if isinstance(started, (int, float)) else None,
        "plan": cache.read_json(rdir / "plan.json"), "videos": videos, "last_run_line": last.get("-"),
        "log": str(log), "result": rj.get("result"),
    }
    print(format_json_line(TAG_WAIT, obj), flush=True)
    return code


def _run_alive(rj: dict[str, Any]) -> bool:
    pid = rj.get("pid")
    if pid is None:  # detach stub: child not started yet
        started = rj.get("started_ts")
        return isinstance(started, (int, float)) and time.time() - started < LAUNCH_GRACE_S
    return cache.run_pid_alive(rj)


def _signal(pid: int, sig: int, *, group: bool) -> None:
    try:
        if group and os.name == "posix":
            os.killpg(pid, sig)
        else:
            os.kill(pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _strays(rid: str, rj: dict[str, Any]) -> list[int]:
    """Live children the run registered (runs/<id>/children.json, pid-reuse checked), plus
    run.json worker_pid for runs that predate the registry file."""
    reg = proc.read_registry(cache.cache_root() / cache.RUNS_DIR / rid / CHILDREN_FILE)
    pids = [p for p, ts in reg if cache.same_process(p, ts)]
    wpid = rj.get("worker_pid")
    if not reg and rj.get("state") == "running" and cache.pid_alive(wpid):
        pids.append(wpid)
    return pids


def _stop_strays(rid: str, rj: dict[str, Any]) -> int:
    """SIGTERM every stray child's process group (they run in their own sessions), SIGKILL
    whatever is left after 2 s. Returns how many were signalled."""
    pids = _strays(rid, rj)
    for p in pids:
        _signal(p, signal.SIGTERM, group=True)
    deadline = time.monotonic() + 2.0
    while any(cache.pid_alive(p) for p in pids) and time.monotonic() < deadline:
        time.sleep(0.1)
    sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)  # Windows has no SIGKILL
    for p in _strays(rid, rj):
        _signal(p, sigkill, group=True)
    return len(pids)


def cmd_cancel(args: argparse.Namespace) -> int:
    """Only a run whose run.json state is "running" and whose pid is still the run's process
    (cache.run_pid_alive: pid reuse never gets signalled) is stopped: SIGTERM the run pid
    (its process group when detached), wait up to 5 s, then SIGKILL. Then any children the
    run registered that are still alive (ffmpeg / yt-dlp / ASR worker orphaned by a killed
    orchestrator) get SIGTERM, then SIGKILL (_stop_strays). A "running" run whose pid is gone
    is marked "cancelled". Prints one text line. Exit 0 (also when already finished), 2 for
    an unknown run id."""
    rid = args.run
    if not cache.valid_run_id(rid):
        raise UsageError("cancel: invalid run id")
    rj = cache.read_run_json(rid)
    if rj is None:
        _err(f"cancel: unknown run id {rid}")
        return EXIT_USAGE
    pid = rj.get("pid")
    group = bool(rj.get("detached"))
    live = rj.get("state") == "running" and cache.run_pid_alive(rj)
    killed = False
    if live:
        _signal(pid, signal.SIGTERM, group=group)
        deadline = time.monotonic() + CANCEL_GRACE_S
        while cache.run_pid_alive(rj) and time.monotonic() < deadline:
            time.sleep(0.1)
        if cache.run_pid_alive(rj):
            _signal(pid, getattr(signal, "SIGKILL", signal.SIGTERM), group=group)  # Windows: no SIGKILL
            killed = True
            time.sleep(0.2)
    strays = _stop_strays(rid, rj)
    after = cache.read_run_json(rid) or rj
    if after.get("state") == "running":
        cache.write_run_json(rid, dict(after, state="cancelled", exit=EXIT_INTERRUPTED))
    extra = f", stopped {strays} leftover process{'es' if strays != 1 else ''}" if strays else ""
    if live:
        print(f"cancelled run {rid} (pid {pid}{', killed' if killed else ''}{extra})", flush=True)
    elif rj.get("state") == "running":
        print(f"run {rid} had already stopped (pid gone), marked cancelled{extra}", flush=True)
    else:
        print(f"run {rid} is not running (state {rj.get('state')}){extra}", flush=True)
    return EXIT_OK


# --------------------------------------------------------------------------
# visual-put / frame
# --------------------------------------------------------------------------
def _key_or_usage(key: str, cmd: str) -> cache.KeyPaths:
    if not cache.valid_key(key):
        raise UsageError(f"{cmd}: invalid key {key!r}")
    kp = cache.key_paths(key)
    if not kp.dir.is_dir():
        raise UsageError(f"{cmd}: unknown key {key} (not in the cache)")
    return kp


VISUAL_PUT_MAX_BYTES = 4_000_000


def cmd_visual_put(args: argparse.Namespace) -> int:
    """Read stdin, or `--from PATH` (UTF-8) -> plan.write_visual_md(KEY, --view VTAG, --flags F);
    print the path. `--from` exists because a heredoc carrying transcribed code (braces +
    quotes) trips Claude Code's shell-obfuscation check and needs approval on every --code
    run; the agent writes the draft with its Write tool instead. A --from file inside the
    key's cache dir is deleted after a successful store. Exit 2 for unknown key/view,
    missing/oversized file or an empty body."""
    kp = _key_or_usage(args.key, "visual-put")
    if not _VTAG_RE.match(args.view):
        raise UsageError(f"visual-put: invalid view {args.view!r}")
    src: Path | None = None
    if args.from_path:
        src = Path(args.from_path).expanduser()
        try:
            if not src.is_file() or src.stat().st_size > VISUAL_PUT_MAX_BYTES:
                raise UsageError(f"visual-put: --from {src} is not a readable file under 4 MB")
            raw = src.read_bytes()
        except OSError as e:
            raise UsageError(f"visual-put: cannot read --from {src}: {e.strerror}") from None
    else:
        raw = sys.stdin.buffer.read()
    body = raw.decode("utf-8", errors="replace")
    if not body.strip():
        raise UsageError("visual-put: empty input (pipe the merged visual timeline in, or pass --from)")
    try:
        path = plan.write_visual_md(kp.view(args.view), args.key, args.flags, body, VERSION)
    except FileNotFoundError:
        raise UsageError(f"visual-put: unknown view {args.view} for {args.key}") from None
    if src is not None and src.resolve().is_relative_to(kp.dir.resolve()) and src.resolve() != path.resolve():
        src.unlink(missing_ok=True)
    print(path, flush=True)
    return EXIT_OK


def parse_crop(text: str) -> tuple[int, int, int, int]:
    """"X,Y,W,H" non-negative ints, W and H > 0, else UsageError."""
    parts = text.replace(" ", "").split(",")
    try:
        x, y, w, h = (int(p) for p in parts)
    except ValueError:
        raise UsageError("frame: --crop takes X,Y,W,H (integers)") from None
    if x < 0 or y < 0 or w <= 0 or h <= 0:
        raise UsageError("frame: --crop needs X,Y >= 0 and W,H > 0")
    return x, y, w, h


def cmd_frame(args: argparse.Namespace) -> int:
    """frames.zoom(KEY, --t (types.parse_ts accepted too), --crop, --width clamped to 2000);
    print the absolute jpg path. --crop is in the pixels of the plain `frame KEY --t T` image
    (default width), whatever --width asks for. Exit 2 for unknown key, t outside
    [0, duration] or a crop box outside the image."""
    kp = _key_or_usage(args.key, "frame")
    man = cache.load_manifest(kp)
    if man is None:
        raise UsageError(f"frame: {args.key} has no manifest")
    try:
        t = parse_ts(args.t)
    except ValueError as e:
        raise UsageError(f"frame: {e}") from None
    meta = cache.read_json(kp.meta, {}) or {}
    dur = meta.get("duration")
    if isinstance(dur, (int, float)) and dur > 0 and not 0 <= t <= dur:
        raise UsageError(f"frame: --t {t:g} is outside the video (0..{dur:g} s)")
    crop = parse_crop(args.crop) if args.crop else None
    width = max(64, min(MAX_IMAGE_SIDE, args.width))
    try:
        path = frames.zoom(kp, man, t, width=width, crop=crop)
    except ValueError as e:  # crop box outside the frame image
        raise UsageError(f"frame: {e}") from None
    except WfmError as e:
        _err(f"frame: {e.code}: {e.message}")
        return EXIT_ALL_FAILED
    print(Path(path).resolve(), flush=True)
    return EXIT_OK


# --------------------------------------------------------------------------
# doctor / setup / cache
# --------------------------------------------------------------------------
def cmd_doctor(args: argparse.Namespace) -> int:
    """doctor.run_checks(quick) -> JSON (one line) or table on stdout; exit report.exit_code."""
    import json

    from . import doctor

    report = doctor.run_checks(quick=args.quick)
    if args.json:
        print(json.dumps(doctor.to_json(report), ensure_ascii=False, separators=(",", ":")), flush=True)
    else:
        print(doctor.render_table(report), flush=True)
    return report.exit_code


def cmd_setup(args: argparse.Namespace) -> int:
    """Prefetch this backend's models: AsrWorker(...).prefetch() with model events printed as
    WFM lines (`WFM <t> - model start model=<id> mb=<mb>` / `model done`), then a size
    summary. SETUP_TIP printed last ONLY when sys.stdout.isatty(). Exit 0, or 5 on failure."""
    if not proc.which("uv"):
        from .doctor import install_hint

        _err(f"missing prerequisite: uv. Install: {install_hint('uv')}")
        return EXIT_PREREQ
    try:
        backend = asrc.select_backend()
    except ValueError as e:
        raise UsageError(f"setup: {e}") from None
    root = cache.cache_root()
    progress = Progress(time.monotonic())
    state: dict[str, Any] = {}

    async def go() -> dict[str, int]:
        state["task"] = asyncio.current_task()
        install_signal_handlers(asyncio.get_running_loop(), state)
        w = AsrWorker(backend, root / cache.ASR_LOCK, log_path=root / "setup.log",
                      on_model=lambda ev: _on_model(progress, ev))
        state["worker"] = w
        try:
            await w.start()
            await w.wait_ready()
            return await w.prefetch()
        finally:
            await w.close()

    progress.event(None, "run", "start", backend=backend, cmd="setup")
    try:
        models = asyncio.run(go())
    except (asyncio.CancelledError, KeyboardInterrupt):
        progress.event(None, "run", "error", code="interrupted")
        return EXIT_INTERRUPTED
    except WfmError as e:
        progress.event(None, "run", "error", code=e.code)
        _err(f"setup failed: {e.code}: {e.message} (log: {root / 'setup.log'})")
        return EXIT_ALL_FAILED
    total = sum(int(mb or 0) for mb in models.values())
    progress.event(None, "run", "done", backend=backend, models=len(models), mb=total)
    for mid, mb in models.items():
        print(f"  {mid}  {mb} MB", flush=True)
    if sys.stdout.isatty():
        print(SETUP_TIP, flush=True)
    return EXIT_OK


def cmd_cache(args: argparse.Namespace) -> int:
    """list: "<key>  <MB>  <last_used>  <title>" per entry + total; path: cache root (or key
    dir with KEY); clear KEY | --all: cache.clear, prints deleted count. clear without a
    target -> exit 2."""
    root = cache.cache_root()
    if args.cache_cmd == "list":
        entries = cache.list_entries(root)
        for e in entries:
            title = " ".join((e.title or "-").split())
            print(f"{e.key}  {e.bytes / 1e6:.1f}MB  {e.last_used or '-'}  {title}")
        total = sum(e.bytes for e in entries)
        print(f"{len(entries)} entries, {total / 1e6:.1f} MB (cap {cache.max_bytes() / 1e9:g} GB) in {root}",
              flush=True)
        return EXIT_OK
    if args.cache_cmd == "path":
        if args.key:
            print(_key_or_usage(args.key, "cache path").dir, flush=True)
        else:
            print(root, flush=True)
        return EXIT_OK
    if args.all and args.key:
        raise UsageError("cache clear: pass KEY or --all, not both")
    if not args.all and not args.key:
        raise UsageError("cache clear: pass KEY or --all")
    try:
        deleted = cache.clear(args.key, all_=args.all, root=root)
    except KeyError:
        raise UsageError(f"cache clear: unknown key {args.key}") from None
    print(f"deleted {len(deleted)} cache entr{'y' if len(deleted) == 1 else 'ies'}", flush=True)
    return EXIT_OK
