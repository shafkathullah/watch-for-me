"""Subprocess helpers + child-process registry (IMPLEMENTED, shared).

Every external process wfm starts (yt-dlp, ffmpeg, ffprobe, the ASR worker)
MUST be started through `run()` / `arun()` / `popen()` here so that
`kill_children()` can terminate them on SIGINT/SIGTERM/`cancel` without
killing the caller's shell (we never use killpg on our own group).

Stdlib only. Not in the spec tree (skeleton addition, see wfm/__init__.py).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from functools import cache
from pathlib import Path

_LOCK = threading.Lock()
_CHILDREN: dict[int, float] = {}  # pid -> epoch spawn time, live children started via this module
_REGISTRY_FILE: Path | None = None  # runs/<id>/children.json while a `run` is active


@dataclass
class ProcResult:
    returncode: int
    stdout: str
    stderr: str


def set_registry_file(path: Path | None) -> None:
    """Mirror the child registry to `path` (JSON {"children":[[pid, spawn_ts], ...]}) on every
    change, so `cancel` can reach children whose orchestrator was SIGKILLed (they run in
    their own sessions and outlive it). None stops mirroring."""
    global _REGISTRY_FILE
    with _LOCK:
        _REGISTRY_FILE = path
        _flush_registry()


def _flush_registry() -> None:
    """Caller holds _LOCK. Best effort, atomic (tmp + os.replace)."""
    if _REGISTRY_FILE is None:
        return
    tmp = _REGISTRY_FILE.with_name(f".{_REGISTRY_FILE.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps({"children": [[p, t] for p, t in sorted(_CHILDREN.items())]}))
        os.replace(tmp, _REGISTRY_FILE)
    except OSError:
        pass


def read_registry(path: Path) -> list[tuple[int, float]]:
    """[(pid, spawn_ts)] from a registry file; [] when missing or unreadable."""
    try:
        data = json.loads(path.read_text())
        return [(int(p), float(t)) for p, t in data.get("children") or []]
    except (OSError, ValueError, TypeError, AttributeError):
        return []


def _register(pid: int) -> None:
    with _LOCK:
        _CHILDREN[pid] = time.time()
        _flush_registry()


def _unregister(pid: int) -> None:
    with _LOCK:
        if _CHILDREN.pop(pid, None) is not None:
            _flush_registry()


def live_children() -> list[int]:
    with _LOCK:
        return sorted(_CHILDREN)


def kill_children(sig: int = signal.SIGTERM) -> list[int]:
    """Send `sig` to every registered live child. Returns the pids signalled.

    Children are started with start_new_session=True on POSIX, so the signal
    goes to the child's whole group (e.g. uv -> python worker, yt-dlp -> ffmpeg).
    """
    pids = live_children()
    for pid in pids:
        try:
            if os.name == "posix":
                os.killpg(pid, sig)
            else:
                os.kill(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    return pids


def _nice_prefix(nice: bool) -> list[str]:
    if nice and os.name == "posix" and shutil.which("nice"):
        return ["nice", "-n", "10"]
    return []


def popen(cmd: list[str], *, nice: bool = False, **kw: object) -> subprocess.Popen:
    """Start a registered child in its own process group (POSIX).

    Caller must call `reap(proc)` (or `wait_popen`) when it exits so the pid is
    unregistered. Extra kwargs go to subprocess.Popen.
    """
    if os.name == "posix":
        kw.setdefault("start_new_session", True)
    p = subprocess.Popen([*_nice_prefix(nice), *cmd], **kw)  # type: ignore[call-overload]
    _register(p.pid)
    return p


def reap(proc: subprocess.Popen) -> None:
    _unregister(proc.pid)


def run(cmd: list[str], *, check: bool = False, nice: bool = False, timeout: float | None = None,
        input_text: str | None = None, env: dict[str, str] | None = None) -> ProcResult:
    """Run to completion (sync, safe inside asyncio.to_thread), text mode, capture both streams.

    Raises:
        subprocess.CalledProcessError: when check=True and rc != 0.
        FileNotFoundError: when the executable is missing.
        subprocess.TimeoutExpired: after killing the child, when timeout elapses.
    """
    p = popen(cmd, nice=nice, stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        out, err = p.communicate(input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        raise
    finally:
        reap(p)
    if check and p.returncode:
        raise subprocess.CalledProcessError(p.returncode, cmd, out, err)
    return ProcResult(p.returncode, out, err)


async def arun(cmd: list[str], *, nice: bool = False, stdout_path: str | None = None,
               env: dict[str, str] | None = None) -> ProcResult:
    """Async run to completion, capture stderr (and stdout unless stdout_path is set,
    in which case stdout is written to that file and ProcResult.stdout is "").

    Cancellation of the awaiting task kills the child.
    """
    out_f = open(stdout_path, "wb") if stdout_path else None  # noqa: SIM115, ASYNC230 (tiny local file, opened once)
    start_new_session = os.name == "posix"
    try:
        p = await asyncio.create_subprocess_exec(
            *_nice_prefix(nice), *cmd, stdin=asyncio.subprocess.DEVNULL,
            stdout=out_f if out_f else asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=start_new_session, env=env)
        _register(p.pid)
        try:
            out, err = await p.communicate()
        except asyncio.CancelledError:
            try:
                if start_new_session:
                    os.killpg(p.pid, signal.SIGKILL)
                else:
                    p.kill()
            except (ProcessLookupError, PermissionError, OSError):
                pass
            raise
        finally:
            _unregister(p.pid)
    finally:
        if out_f:
            out_f.close()
    return ProcResult(p.returncode or 0, (out or b"").decode(errors="replace"), err.decode(errors="replace"))


def which(name: str) -> str | None:
    return shutil.which(name)


@cache
def tool_version(name: str) -> str | None:
    """First version-looking token of `<name> -version`/`--version`, or None if missing.

    ffmpeg/ffprobe: "-version" (e.g. "8.0.1"); uv/node/deno/bun: "--version".
    """
    exe = shutil.which(name)
    if not exe:
        return None
    flag = "-version" if name in ("ffmpeg", "ffprobe") else "--version"
    try:
        r = subprocess.run([exe, flag], capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"(\d+\.\d+(?:\.\d+)?)", r.stdout + r.stderr)
    return m.group(1) if m else "unknown"


def ffmpeg_version() -> tuple[int, int] | None:
    """(major, minor) of ffmpeg on PATH, None if missing/unparseable.

    frames.py uses `-fps_mode passthrough` when >= (5, 1), else `-vsync passthrough`.
    """
    v = tool_version("ffmpeg")
    if not v or v == "unknown":
        return None
    parts = v.split(".")
    return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0


def js_runtime_args() -> list[str]:
    """yt-dlp `--js-runtimes` args for non-default runtimes found on PATH (spec 4.2).

    deno needs nothing (yt-dlp[deno] extra ships it and it is enabled by default);
    node and bun each add one `--js-runtimes <name>`.
    """
    args: list[str] = []
    for rt in ("node", "bun"):
        if shutil.which(rt):
            args += ["--js-runtimes", rt]
    return args


def ytdlp_base() -> list[str]:
    """[sys.executable, "-m", "yt_dlp"]: yt-dlp always runs as a subprocess of the watch env."""
    return [sys.executable, "-m", "yt_dlp"]


def ytdlp_latest_base() -> list[str]:
    """Fallback after extractor/download errors (spec 3): latest yt-dlp via uvx."""
    return ["uvx", "--from", "yt-dlp[default,deno]@latest", "yt-dlp"]
