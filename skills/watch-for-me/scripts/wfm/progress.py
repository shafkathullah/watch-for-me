"""WFM progress lines and the WFM_RESULT / WFM_WAIT / WFM_STARTED JSON lines (spec 3).

Line grammar (stdout, one event per line, stage completions only):
    WFM <elapsed_s> <key|-> <stage> <status> [k=v ...]
- elapsed_s: seconds since run start, 2 decimals ("%.2f").
- key: cache key or "-" for run-level events.
- stage in types.EVENT_STAGES, status in types.EVENT_STATUSES.
- k=v values never contain whitespace: `sanitize_value` replaces any run of
  whitespace with "_"; titles never go into WFM lines.
- `asr progress pct=25|50|75` at most once per threshold per job.
JSON lines: "<TAG> " + compact json (ensure_ascii=False), TAG in
    WFM_RESULT (types.RunResult.to_dict())
    WFM_WAIT   (see cli.cmd_wait for the shape)
    WFM_STARTED {"run_id": str, "pid": int}   (run --detach)

Owned sinks: stdout (always, unless quiet for WFM lines) and optionally the
run log file runs/<id>/log (append) for foreground runs, so `wait` can tail it.
In a --detach child stdout already IS the log, so log_path is None there.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from .types import EVENT_STAGES, EVENT_STATUSES

LINE_PREFIX = "WFM "
TAG_RESULT = "WFM_RESULT"
TAG_WAIT = "WFM_WAIT"
TAG_STARTED = "WFM_STARTED"
_WS_RE = re.compile(r"\s+")


@dataclass
class ProgressEvent:
    """Parsed WFM line."""

    elapsed: float
    key: str | None  # None for "-"
    stage: str
    status: str
    kv: dict[str, str] = field(default_factory=dict)


def _fmt_float(v: float) -> str:
    s = f"{v:.2f}".rstrip("0")
    return s + "0" if s.endswith(".") else s


def sanitize_value(value: object) -> str:
    """str(value) with whitespace runs -> "_"; floats formatted with up to 2 decimals
    (trailing zeros dropped, at least one decimal kept: 21.0 -> "21.0", 3.14159 -> "3.14"),
    bools as "true"/"false", None as "-"."""
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return _fmt_float(value)
    text = _WS_RE.sub("_", str(value).strip())
    return text or "-"


def format_line(elapsed: float, key: str | None, stage: str, status: str, **kv: object) -> str:
    """Build one WFM line (no trailing newline). kv order is preserved.

    Raises:
        ValueError: stage/status outside the vocabularies in wfm.types.
    """
    if stage not in EVENT_STAGES:
        raise ValueError(f"unknown stage {stage!r}")
    if status not in EVENT_STATUSES:
        raise ValueError(f"unknown status {status!r}")
    parts = [f"WFM {max(0.0, elapsed):.2f}", sanitize_value(key) if key else "-", stage, status]
    parts += [f"{sanitize_value(k)}={sanitize_value(v)}" for k, v in kv.items()]
    return " ".join(parts)


def parse_line(line: str) -> ProgressEvent | None:
    """Inverse of format_line; None for lines that are not WFM progress lines
    (including WFM_RESULT/WFM_WAIT/WFM_STARTED lines)."""
    if not line.startswith(LINE_PREFIX):
        return None
    parts = line.strip().split()
    if len(parts) < 5:
        return None
    try:
        elapsed = float(parts[1])
    except ValueError:
        return None
    kv: dict[str, str] = {}
    for tok in parts[5:]:
        k, sep, v = tok.partition("=")
        if not sep:
            return None
        kv[k] = v
    return ProgressEvent(elapsed, None if parts[2] == "-" else parts[2], parts[3], parts[4], kv)


def format_json_line(tag: str, obj: dict[str, Any]) -> str:
    """"<tag> <compact json>" (no trailing newline)."""
    return f"{tag} " + json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def parse_json_line(line: str, tag: str) -> dict[str, Any] | None:
    """Parse a "<tag> {json}" line, None if the line has another tag or bad JSON."""
    prefix = tag + " "
    if not line.startswith(prefix):
        return None
    try:
        obj = json.loads(line[len(prefix):])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def last_lines_by_key(log_path: str | Path, max_bytes: int = 262_144) -> dict[str, str]:
    """Tail of runs/<id>/log -> {key or "-": last WFM line for it}. Missing file -> {}.
    Reads at most the last max_bytes. Used by `wait` for per-video progress."""
    try:
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except OSError:
        return {}
    out: dict[str, str] = {}
    for raw in data.decode("utf-8", errors="replace").splitlines():
        line = raw.rstrip("\r")
        ev = parse_line(line)
        if ev is not None:
            out[ev.key or "-"] = line
    return out


class Progress:
    """Emitter used by cli for one run (thread-safe: one lock around writes).

    Args:
        t0: time.monotonic() at run start (elapsed base).
        quiet: suppress WFM lines on stdout (JSON lines are always printed).
        stream: output stream, default sys.stdout (resolved at write time).
        log_path: also append every line (WFM and JSON) to this file, or None.
    """

    def __init__(self, t0: float, *, quiet: bool = False, stream: IO[str] | None = None,
                 log_path: str | Path | None = None) -> None:
        self.t0 = t0
        self.quiet = quiet
        self._stream = stream
        self.log_path = str(log_path) if log_path else None
        self._lock = threading.Lock()
        self._last: dict[str, str] = {}

    def elapsed(self) -> float:
        """Seconds since t0."""
        return time.monotonic() - self.t0

    def _write(self, line: str, *, to_stdout: bool) -> None:
        with self._lock:
            if to_stdout:
                stream = self._stream or sys.stdout
                try:
                    stream.write(line + "\n")
                    stream.flush()
                except (OSError, ValueError):
                    pass  # closed/broken stdout must never kill a run
            if self.log_path:
                try:
                    with open(self.log_path, "a", encoding="utf-8") as f:
                        f.write(line + "\n")
                except OSError:
                    pass

    def event(self, key: str | None, stage: str, status: str, **kv: object) -> str:
        """Emit one WFM line (flushes). Returns the line. Also remembers it as the
        last line for `key` (see last())."""
        line = format_line(self.elapsed(), key, stage, status, **kv)
        self._last[key or "-"] = line
        self._write(line, to_stdout=not self.quiet)
        return line

    def last(self) -> dict[str, str]:
        """{key or "-": last WFM line emitted in this process}."""
        return dict(self._last)

    def json(self, tag: str, obj: dict[str, Any]) -> str:
        """Emit a JSON line (never suppressed by quiet). Returns it."""
        line = format_json_line(tag, obj)
        self._write(line, to_stdout=True)
        return line

    def result(self, obj: dict[str, Any]) -> str:
        """= json(TAG_RESULT, obj); must be the LAST stdout line of `run`."""
        return self.json(TAG_RESULT, obj)
