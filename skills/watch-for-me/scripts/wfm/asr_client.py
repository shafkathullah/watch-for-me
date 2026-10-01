"""ASR backend selection; spawns and drives ONE long-lived ASR worker per run over
JSONL (protocol in asr_common's docstring); writes transcripts/<rtag>.json.

Runs in the watch.py env: imports only the stdlib-safe parts of asr_common
(constants, encode/decode, model_ids, hf_repo_cached, model_size_mb).

Division of labour (skeleton decision): asr_client writes transcripts/<rtag>.json
(+ the partial JSONL while running); plan.write_transcript_md writes <rtag>.md
(it needs the frame-segment markers).

Worker events the client consumes (see asr_common): ready, model (status
downloading | progress | done; `progress` carries got_mb and arrives every ~60 s
while a model downloads), chunk, done, prefetched, error, waiting (job queued
behind another session's job on asr.lock; logged only). Unknown events are ignored.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import os
import platform
import signal
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import asr_common as ac

from . import cache, proc
from .cache import KeyPaths
from .types import AsrJob, WfmError

WORKER_SCRIPTS = {"mlx": "asr_mlx.py", "cpu": "asr_cpu.py"}
READY_TIMEOUT_S: float | None = None  # first run installs the env + downloads models: no timeout
SHUTDOWN_GRACE_S = 3.0
STREAM_LIMIT = 32 * 1024 * 1024  # max bytes per JSONL line from the worker
MAX_SPAWNS = 2  # first start + one respawn after a crash
WORKER_ENV = {
    "HF_HUB_DISABLE_PROGRESS_BARS": "1",  # tqdm bars would flood the log; worker emits its own progress
    "TQDM_DISABLE": "1",
    "PYTHONUNBUFFERED": "1",
    "PYTHONIOENCODING": "utf-8",
    "TOKENIZERS_PARALLELISM": "false",
}

# Callbacks the worker driver invokes (cli wires them to Progress + run.json):
OnModel = Callable[[dict[str, Any]], None]  # raw EV_MODEL event
OnChunk = Callable[[AsrJob, dict[str, Any]], None]  # raw EV_CHUNK event (after partial append)


def select_backend(env: dict[str, str] | None = None) -> str:
    """WFM_ASR_BACKEND "mlx"|"cpu" wins; "auto"/unset -> "mlx" iff sys.platform == "darwin"
    and platform.machine() == "arm64", else "cpu". Invalid value -> ValueError (exit 2)."""
    e = os.environ if env is None else env
    v = (e.get("WFM_ASR_BACKEND") or "auto").strip().lower()
    if v in WORKER_SCRIPTS:
        return v
    if v != "auto":
        raise ValueError(f"WFM_ASR_BACKEND must be auto, mlx or cpu (got {v!r})")
    return "mlx" if sys.platform == "darwin" and platform.machine() == "arm64" else "cpu"


def scripts_dir() -> Path:
    """Absolute dir holding watch.py / asr_*.py (parent of the wfm package)."""
    return Path(__file__).resolve().parent.parent


def worker_command(backend: str, lock_path: Path) -> list[str]:
    """["uv", "run", "--quiet", ("--locked" iff <script>.lock exists next to the script,)
    "--script", <abs script>, "--lock", str(lock_path)]. --quiet keeps env-install noise
    off stderr (stderr is still captured to the log)."""
    script = scripts_dir() / WORKER_SCRIPTS[backend]
    cmd = [proc.which("uv") or "uv", "run", "--quiet"]
    if script.with_name(script.name + ".lock").is_file():
        cmd.append("--locked")
    return [*cmd, "--script", str(script), "--lock", str(lock_path)]


def transcript_params(backend: str, lang: str | None, from_s: float | None, to_s: float | None) -> dict[str, Any]:
    """params_hash input for manifest stage "transcripts/<rtag>":
    {"backend", "models": asr_common.model_ids(backend), "lang", "from", "to",
     "chunk_t": asr_common.CHUNK_T, "lid_min_p": asr_common.LID_MIN_P, "v": 1}."""
    return {"backend": backend, "models": ac.model_ids(backend), "lang": lang, "from": from_s, "to": to_s,
            "chunk_t": ac.CHUNK_T, "lid_min_p": ac.LID_MIN_P, "v": 1}


def _err_code(code: Any) -> str:
    return code if code in ac.WORKER_ERROR_CODES else "asr_failed"


class AsrWorker:
    """One worker subprocess, jobs serialized FIFO in submission order (spec 4.1).

    Lifecycle: `start()` (non-blocking spawn; cli calls it at run start when any input is
    not an index hit and not --video-only, else lazily on first submit) -> `submit()`
    per job -> `close()`. `kill()` from signal handlers/atexit.
    The worker process is spawned in its own session (process group: uv + python) and
    registered with wfm.proc so kill_children() reaches it; its stderr is appended to
    log_path (or dropped), keeping the last lines for error messages.
    """

    def __init__(self, backend: str, lock_path: Path, *, log_path: Path | None = None,
                 on_model: OnModel | None = None) -> None:
        self.backend = backend
        self.lock_path = Path(lock_path)
        self.log_path = Path(log_path) if log_path else None
        self.on_model = on_model
        self._proc: asyncio.subprocess.Process | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._ready: asyncio.Future[dict[str, Any]] | None = None
        self._pending: dict[str, tuple[asyncio.Future[dict[str, Any]], Callable[[dict[str, Any]], None] | None]] = {}
        self._prefetch: asyncio.Future[dict[str, Any]] | None = None
        self._lock: asyncio.Lock | None = None
        self._versions: dict[str, str] = {}
        self._tail: collections.deque[str] = collections.deque(maxlen=30)
        self._fatal: WfmError | None = None
        self._spawns = 0
        self._closed = False

    @property
    def pid(self) -> int | None:
        """Worker pid once spawned (the `uv` process; its group includes python)."""
        return self._proc.pid if self._proc else None

    @property
    def versions(self) -> dict[str, str]:
        """`versions` from the ready event ({} before ready) for manifest `tools`."""
        return dict(self._versions)

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self) -> None:
        """Spawn if not running and start the stdout reader task. Returns immediately
        (does not wait for EV_READY). After a crash, one respawn is allowed; a fatal
        worker error (e.g. model_download_failed) is re-raised instead."""
        if self._fatal is not None:  # before `running`: a fatal worker may not have exited yet
            raise self._fatal
        if self.running:
            return
        if self._closed:
            raise WfmError("asr_failed", "ASR worker already closed")
        if self._spawns >= MAX_SPAWNS:
            raise WfmError("asr_failed", "ASR worker crashed twice; giving up" + self._tail_msg())
        self._spawns += 1
        loop = asyncio.get_running_loop()
        self._ready = loop.create_future()
        env = {**os.environ, **WORKER_ENV}
        cmd = worker_command(self.backend, self.lock_path)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=env, limit=STREAM_LIMIT,
                start_new_session=os.name == "posix")
        except FileNotFoundError as e:
            raise WfmError("asr_failed", f"cannot start ASR worker: {e}", hint="install uv") from e
        proc._register(self._proc.pid)
        err_task = asyncio.create_task(self._read_stderr(self._proc))
        self._tasks = [asyncio.create_task(self._read_stdout(self._proc, err_task)), err_task]

    async def wait_ready(self) -> float:
        """Await EV_READY; returns load_s. Raises WfmError("asr_failed") if the worker
        exits first (message = last stderr line), or "model_download_failed" /
        "disk_full" if a fatal EV_ERROR with that code arrives."""
        await self.start()
        assert self._ready is not None
        ev = await self._ready
        return float(ev.get("load_s") or 0.0)

    def _job_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def submit(self, job: AsrJob, on_chunk: OnChunk | None = None) -> dict[str, Any]:
        """Send OP_JOB and await its EV_DONE (FIFO: an internal asyncio.Lock serializes jobs,
        so callers may submit concurrently). Returns the EV_DONE event.

        Raises WfmError(code from EV_ERROR: asr_failed | no_audio | model_download_failed |
        disk_full), or WfmError("asr_failed") if the worker dies mid-job (one respawn is
        attempted for the NEXT job, not this one).
        """
        msg = {"op": ac.OP_JOB, "job_id": job.job_id, "key": job.key, "audio": str(job.audio),
               "from": job.from_s, "to": job.to_s, "lang": job.lang}
        cb = (lambda ev: on_chunk(job, ev)) if on_chunk else None
        async with self._job_lock():
            return await self._request(job.job_id, msg, cb)

    async def prefetch(self, roles: tuple[str, ...] = ("parakeet", "lid", "whisper")) -> dict[str, int]:
        """OP_PREFETCH -> EV_PREFETCHED models {id: mb}. Used by `setup`."""
        async with self._job_lock():
            ev = await self._request(None, {"op": ac.OP_PREFETCH, "roles": list(roles)}, None)
        return dict(ev.get("models") or {})

    async def _request(self, job_id: str | None, msg: dict[str, Any],
                       cb: Callable[[dict[str, Any]], None] | None) -> dict[str, Any]:
        await self.start()
        p = self._proc
        assert p is not None and p.stdin is not None
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        if job_id is None:
            self._prefetch = fut
        else:
            self._pending[job_id] = (fut, cb)
        try:
            p.stdin.write((ac.encode(msg) + "\n").encode())
            await p.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as e:
            self._drop(job_id)
            raise WfmError("asr_failed", "ASR worker is not accepting jobs" + self._tail_msg()) from e
        try:
            return await fut
        finally:
            self._drop(job_id)

    def _drop(self, job_id: str | None) -> None:
        if job_id is None:
            self._prefetch = None
        else:
            self._pending.pop(job_id, None)

    # -- reader tasks ------------------------------------------------------
    async def _read_stdout(self, p: asyncio.subprocess.Process, err_task: asyncio.Task[None]) -> None:
        assert p.stdout is not None
        try:
            while True:
                try:
                    line = await p.stdout.readline()
                except ValueError:  # line over STREAM_LIMIT: skip it
                    continue
                if not line:
                    break
                ev = ac.decode(line)
                if ev is not None:
                    self._dispatch(ev)
        finally:
            rc = await p.wait()
            proc._unregister(p.pid)
            with contextlib.suppress(asyncio.TimeoutError):  # let the stderr tail settle for the message
                await asyncio.wait_for(asyncio.shield(err_task), 1.0)
            self._on_exit(rc)

    async def _read_stderr(self, p: asyncio.subprocess.Process) -> None:
        assert p.stderr is not None
        log = None
        if self.log_path is not None:
            with contextlib.suppress(OSError):
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                log = open(self.log_path, "ab")  # noqa: SIM115, ASYNC230 (lives as long as the worker)
        try:
            while True:
                try:
                    line = await p.stderr.readline()
                except ValueError:
                    continue
                if not line:
                    break
                if log is not None:
                    with contextlib.suppress(OSError):
                        log.write(line)
                        log.flush()
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    self._tail.append(text)
        finally:
            if log is not None:
                log.close()

    def _log(self, text: str) -> None:
        if self.log_path is None:
            return
        with contextlib.suppress(OSError), open(self.log_path, "a", encoding="utf-8") as f:
            f.write(f"[asr_client] {text}\n")

    def _dispatch(self, ev: dict[str, Any]) -> None:
        kind = ev.get("ev")
        if kind == ac.EV_READY:
            self._versions = {str(k): str(v) for k, v in (ev.get("versions") or {}).items()}
            if self._ready is not None and not self._ready.done():
                self._ready.set_result(ev)
        elif kind == ac.EV_MODEL:
            if self.on_model is not None:
                self.on_model(ev)
        elif kind == ac.EV_CHUNK:
            ent = self._pending.get(str(ev.get("job_id")))
            if ent is not None and ent[1] is not None:
                ent[1](ev)
        elif kind == ac.EV_DONE:
            ent = self._pending.get(str(ev.get("job_id")))
            if ent is not None and not ent[0].done():
                ent[0].set_result(ev)
        elif kind == ac.EV_PREFETCHED:
            if self._prefetch is not None and not self._prefetch.done():
                self._prefetch.set_result(ev)
        elif kind == ac.EV_ERROR:
            err = WfmError(_err_code(ev.get("code")), str(ev.get("message") or "ASR worker error"))
            jid = ev.get("job_id")
            if jid is None:
                self._fatal = err
                self._fail_all(err)
            else:
                ent = self._pending.get(str(jid))
                if ent is not None and not ent[0].done():
                    ent[0].set_exception(err)
                elif self._prefetch is not None and not self._prefetch.done():
                    self._prefetch.set_exception(err)
        elif kind == ac.EV_WAITING:
            self._log(f"job {ev.get('job_id')} waiting for {ev.get('reason')}")

    def _tail_msg(self) -> str:
        last = [t for t in self._tail if not t.startswith("Warning:")]
        return f": {last[-1][:200]}" if last else ""

    def _fail_all(self, err: WfmError) -> None:
        futs = [f for f, _ in self._pending.values()]
        futs += [f for f in (self._ready, self._prefetch) if f is not None]
        for f in futs:
            if not f.done():
                f.set_exception(err)
                f.exception()  # mark retrieved: nobody may be awaiting ready

    def _on_exit(self, rc: int) -> None:
        err = self._fatal or WfmError("asr_failed", f"ASR worker exited (rc={rc})" + self._tail_msg())
        self._fail_all(err)

    # -- shutdown ----------------------------------------------------------
    async def close(self) -> None:
        """Send OP_SHUTDOWN, close stdin, wait SHUTDOWN_GRACE_S, then kill. Idempotent."""
        self._closed = True
        p = self._proc
        if p is None:
            return
        if p.returncode is None and p.stdin is not None:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError, RuntimeError):
                p.stdin.write((ac.encode({"op": ac.OP_SHUTDOWN}) + "\n").encode())
                await p.stdin.drain()
            with contextlib.suppress(Exception):
                p.stdin.close()
            try:
                await asyncio.wait_for(p.wait(), SHUTDOWN_GRACE_S)
            except asyncio.TimeoutError:
                self.kill()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(p.wait(), SHUTDOWN_GRACE_S)
                if p.returncode is None:
                    self.kill(getattr(signal, "SIGKILL", signal.SIGTERM))  # Windows has no SIGKILL
                    await p.wait()
        for t in self._tasks:
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(t, SHUTDOWN_GRACE_S)
        proc._unregister(p.pid)

    def kill(self, sig: int = signal.SIGTERM) -> None:
        """Synchronous SIGTERM of the worker's process group (atexit / signal handler safe)."""
        p = self._proc
        if p is None or p.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            if os.name == "posix":
                os.killpg(p.pid, sig)
            else:
                p.terminate()


async def transcribe(worker: AsrWorker, kp: KeyPaths, *, job: AsrJob, rtag: str, duration: float,
                     backend: str, on_progress: Callable[[int], None] | None = None,
                     on_chunk: OnChunk | None = None) -> dict[str, Any]:
    """Run one job and write transcripts/<rtag>.json (spec 3 shape). Returns that dict.

    - Deletes a stale <rtag>.partial.jsonl, appends every EV_CHUNK line to it, deletes it
      after the final json is written atomically (kept on failure: completed chunks survive).
    - on_progress(pct) fires at most once each for 25/50/75, from chunk.t1 relative to the
      [from, to or duration] range (audio time, not chunk count).
    - Segments get global index i in time order; each carries lang/engine of its chunk;
      chunks carry model from the event (+ "skipped":"silence" when the worker skipped one).
    - primary_lang = lang with the most transcribed seconds (chunks with segments).
    - stats = {"audio_s": done.audio_s, "wall_s": done.wall_s, "speed_x": audio_s/wall_s (1 dp),
      "words": total whitespace-split words}.
    Raises WfmError as submit().
    """
    partial = kp.transcript_partial(rtag)
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.unlink(missing_ok=True)
    lo = float(job.from_s or 0.0)
    hi = float(job.to_s) if job.to_s is not None else float(duration or 0.0)
    span = hi - lo
    fired: set[int] = set()
    events: list[dict[str, Any]] = []

    def _chunk(j: AsrJob, ev: dict[str, Any]) -> None:
        with open(partial, "a", encoding="utf-8") as f:
            f.write(ac.encode(ev) + "\n")
        events.append(ev)
        if on_progress is not None and span > 0:
            pct = (float(ev.get("t1", lo)) - lo) / span * 100
            for m in (25, 50, 75):
                if pct >= m and m not in fired and pct < 100:
                    fired.add(m)
                    on_progress(m)
        if on_chunk is not None:
            on_chunk(j, ev)

    done = await worker.submit(job, _chunk)
    full = (job.from_s in (None, 0, 0.0)) and job.to_s is None
    t = build_transcript(events, done, key=job.key, duration=duration, backend=backend,
                         range_=None if full else [lo, hi], lang=job.lang)
    cache.atomic_write_json(kp.transcript_json(rtag), t)
    partial.unlink(missing_ok=True)
    return t


def _primary_lang(chunks: list[dict[str, Any]], segments: list[dict[str, Any]]) -> str | None:
    secs: dict[str, float] = {}
    for s in segments:
        secs[s["lang"]] = secs.get(s["lang"], 0.0) + max(0.0, float(s["t1"]) - float(s["t0"]))
    # no segments: no primary language (LID on music/noise says "nn", "en", ... at low p)
    return max(secs, key=lambda k: secs[k]) if secs else None


def _words(segments: list[dict[str, Any]]) -> int:
    return sum(ac.count_words(str(s.get("text", ""))) for s in segments)


def build_transcript(events: list[dict[str, Any]], done: dict[str, Any], *, key: str, duration: float,
                     backend: str, range_: list[float] | None, lang: str | None = None) -> dict[str, Any]:
    """transcripts/<rtag>.json dict from the job's EV_CHUNK events + EV_DONE (pure)."""
    chunks: list[dict[str, Any]] = []
    segments: list[dict[str, Any]] = []
    for ev in sorted(events, key=lambda e: float(e.get("t0", 0.0))):
        c = {"t0": ev.get("t0"), "t1": ev.get("t1"), "lang": ev.get("lang"), "lang_p": ev.get("lang_p"),
             "engine": ev.get("engine"), "model": ev.get("model")}
        if ev.get("skipped"):
            c["skipped"] = ev["skipped"]
        chunks.append(c)
        for s in ev.get("segments") or []:
            segments.append({"i": 0, "t0": s["t0"], "t1": s["t1"], "text": s["text"], "lang": ev.get("lang"),
                             "engine": ev.get("engine")})
    segments.sort(key=lambda s: (float(s["t0"]), float(s["t1"])))
    for i, s in enumerate(segments):
        s["i"] = i
    audio_s = float(done.get("audio_s") or 0.0)
    wall_s = float(done.get("wall_s") or 0.0)
    return {"v": 1, "key": key, "duration": float(duration or 0.0), "range": range_, "backend": backend,
            "primary_lang": _primary_lang(chunks, segments) or lang, "chunks": chunks, "segments": segments,
            "stats": {"audio_s": round(audio_s, 2), "wall_s": round(wall_s, 2),
                      "speed_x": round(audio_s / wall_s, 1) if wall_s > 0 else None, "words": _words(segments)}}


def slice_transcript(full: dict[str, Any], from_s: float, to_s: float, key: str) -> dict[str, Any]:
    """Sub-range transcript from an existing "full" one (spec 3: slice instead of re-running ASR):
    keep segments with t0 < to_s and t1 > from_s, chunks overlapping, `range` = [from_s, to_s],
    stats.words recomputed, stats.wall_s = 0, i renumbered from 0."""
    dur = float(full.get("duration") or 0.0)
    hi = min(to_s, dur) if dur > 0 else to_s
    segs = [dict(s) for s in full.get("segments") or [] if float(s["t0"]) < to_s and float(s["t1"]) > from_s]
    for i, s in enumerate(segs):
        s["i"] = i
    chunks = [dict(c) for c in full.get("chunks") or [] if float(c["t0"]) < to_s and float(c["t1"]) > from_s]
    return {"v": 1, "key": key, "duration": full.get("duration"), "range": [from_s, hi],
            "backend": full.get("backend"), "primary_lang": _primary_lang(chunks, segs) or full.get("primary_lang"),
            "chunks": chunks, "segments": segs,
            "stats": {"audio_s": round(max(0.0, hi - from_s), 2), "wall_s": 0.0, "speed_x": None,
                      "words": _words(segs), "sliced_from": "full"}}


def engines_summary(transcript: dict[str, Any]) -> str:
    """For the `asr done engines=...` WFM value and context.md: "parakeet:en:58,whisper:fr:2"
    (engine:lang:chunk_count, ordered by first appearance; silence-skipped chunks not
    counted; "none" when every chunk was skipped)."""
    counts: dict[str, int] = {}
    for c in transcript.get("chunks") or []:
        if c.get("skipped"):
            continue
        k = f"{c.get('engine')}:{c.get('lang')}"
        counts[k] = counts.get(k, 0) + 1
    return ",".join(f"{k}:{n}" for k, n in counts.items()) or "none"


def load_transcript(kp: KeyPaths, rtag: str) -> dict[str, Any] | None:
    """Parsed transcripts/<rtag>.json or None (missing, corrupt or wrong version)."""
    t = cache.read_json(kp.transcript_json(rtag))
    if not isinstance(t, dict) or t.get("v") != 1 or not isinstance(t.get("segments"), list):
        return None
    return t

