"""Cache root, keys, index.json, manifest atomic io, file locks, LRU eviction, runs/ dir (spec 3 "Cache layout").

Stdlib only (also imported by the ASR worker envs for FileLock).

Layout (all under cache_root()):
    index.json                  {"v":1,"inputs":{<normalized input>: <key>}}
    index.lock                  FileLock guarding index.json read-modify-write
    asr.lock                    held by the ASR worker per job (all processes/sessions)
    runs/<run_id>/run.json      see write_run_json
    runs/<run_id>/log           WFM lines (detached stdout+stderr, or teed by foreground runs)
    runs/<run_id>/asr.log       ASR worker stderr (kept apart so model-download noise never
                                pushes WFM lines out of the tail `wait` reads)
    runs/<run_id>/plan.json     spec 4.6
    <key>/run.lock              held by a run for the whole time it works on <key>
    <key>/manifest.json         spec 3 manifest (atomic writes)
    <key>/meta.json, info.json.gz, media/, transcripts/, views/<vtag>/..., zoom/

Tag rules (skeleton decisions resolving spec 3 "rtag"/"vtag"):
    range_tag   = "full" | "r<from_ms>-<to_ms>"   (from default 0, to default = duration;
                  from==0 and to>=duration -> "full"; ms = round(s*1000))
    rtag        = range_tag + ("-lang_<xx>" if --lang forced else "")     transcripts/<rtag>.*
    vtag        = range_tag + "-<720|1080>"   (never includes the lang suffix)  views/<vtag>/
Concurrency rule: within one process, manifest/run.json writes happen only on
the asyncio event-loop thread (CPU work returns results from threads; the
coroutine writes). Across processes, <key>/run.lock serializes runs per key.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import json
import os
import re
import secrets
import shutil
import string
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

if TYPE_CHECKING:
    from typing import Self

INDEX_FILE = "index.json"
INDEX_LOCK = "index.lock"
ASR_LOCK = "asr.lock"
RUNS_DIR = "runs"
RUN_LOCK = "run.lock"
MANIFEST = "manifest.json"
DEFAULT_MAX_GB = 10.0
RUN_PRUNE_DAYS = 7
LOCK_POLL_S = 0.2
MAX_MANIFEST_ERRORS = 20

# Query params dropped when normalizing URLs for index.json (tracking / start-time).
DROP_QUERY_PARAMS: frozenset[str] = frozenset(
    {"si", "t", "start", "feature", "pp", "ab_channel", "igsh", "igshid", "s", "ref", "ref_src", "fbclid"}
)  # plus every "utm_*"

_KEY_BAD = re.compile(r"[^A-Za-z0-9_-]")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_YT_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")
_YT_SHORT_RE = re.compile(r"^/(?:shorts|live|embed)/([A-Za-z0-9_-]{11})$")


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return _dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc).timestamp()
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Locks
# --------------------------------------------------------------------------
def _lock_backend() -> str:
    """"fcntl" (POSIX), "msvcrt" (Windows) or "excl" (O_EXCL lockfile fallback).

    Resolved on every call (cheap) so tests can monkeypatch sys.modules to hide fcntl.
    """
    try:
        import fcntl  # noqa: F401

        return "fcntl"
    except ImportError:
        pass
    try:
        import msvcrt  # noqa: F401

        return "msvcrt"
    except ImportError:
        return "excl"


class FileLock:
    """Exclusive inter-process lock on `path` (created if missing).

    POSIX: fcntl.flock(LOCK_EX[|LOCK_NB]). Windows: msvcrt.locking on byte 0
    (fcntl/msvcrt imported LAZILY inside acquire so importing this module never
    fails on either platform; spec 3 review fix). If neither exists, an O_EXCL
    sidecar file `<path>.excl` holding the owner pid (stale when that pid is dead).
    Not reentrant; two FileLock objects on one path conflict even inside one process.

    Usage:
        with FileLock(p):            # blocks
        lk = FileLock(p); ok = lk.acquire(blocking=False)
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._fd: int | None = None
        self._backend: str | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def _try_once(self) -> bool:
        backend = _lock_backend()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if backend == "excl":
            return self._try_excl()
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            if backend == "fcntl":
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        except OSError:
            os.close(fd)
            return False
        self._fd, self._backend = fd, backend
        return True

    def _excl_path(self) -> Path:
        return self.path.with_name(self.path.name + ".excl")

    def _try_excl(self) -> bool:
        p = self._excl_path()
        for _ in range(2):
            try:
                fd = os.open(p, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                try:
                    owner = int(p.read_text().strip() or "0")
                except (OSError, ValueError):
                    owner = 0
                if owner and pid_alive(owner):
                    return False
                with contextlib.suppress(OSError):
                    p.unlink()  # stale: owner died
                continue
            os.write(fd, str(os.getpid()).encode())
            self._fd, self._backend = fd, "excl"
            return True
        return False

    def acquire(self, blocking: bool = True, timeout: float | None = None) -> bool:
        """Acquire. blocking=False -> return False immediately if held elsewhere.
        timeout (blocking only) -> poll every 0.2 s, False on expiry."""
        if self._fd is not None:
            raise RuntimeError(f"FileLock {self.path} is not reentrant")
        if self._try_once():
            return True
        if not blocking:
            return False
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(LOCK_POLL_S)
            if self._try_once():
                return True

    def release(self) -> None:
        """Release and close the fd. No-op if not held."""
        fd, backend = self._fd, self._backend
        if fd is None:
            return
        self._fd = self._backend = None
        try:
            if backend == "fcntl":
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
            elif backend == "msvcrt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        except OSError:
            pass
        finally:
            os.close(fd)
            if backend == "excl":
                with contextlib.suppress(OSError):
                    self._excl_path().unlink()

    def is_held_elsewhere(self) -> bool:
        """Non-blocking probe: True if another holder has it (acquire+release otherwise).
        A lock held by THIS object reports False."""
        if self._fd is not None:
            return False
        if self._try_once():
            self.release()
            return False
        return True

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    def __del__(self) -> None:  # best effort: never leak a held flock fd
        with contextlib.suppress(Exception):
            self.release()


# --------------------------------------------------------------------------
# Root, keys, tags
# --------------------------------------------------------------------------
def cache_root() -> Path:
    """$WFM_CACHE_DIR, else macOS ~/Library/Caches/watch-for-me, Linux
    ${XDG_CACHE_HOME:-~/.cache}/watch-for-me, Windows %LOCALAPPDATA%\\watch-for-me\\Cache.
    Created (parents=True) if missing. Returns an absolute path."""
    env = os.environ.get("WFM_CACHE_DIR")
    if env:
        root = Path(env).expanduser()
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Caches" / "watch-for-me"
    elif sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        root = Path(base) / "watch-for-me" / "Cache"
    else:
        base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
        root = Path(base) / "watch-for-me"
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def max_bytes() -> int:
    """WFM_CACHE_MAX_GB (float, default 10) -> bytes (GB = 1e9)."""
    try:
        gb = float(os.environ.get("WFM_CACHE_MAX_GB") or DEFAULT_MAX_GB)
    except ValueError:
        gb = DEFAULT_MAX_GB
    return int(max(0.0, gb) * 1e9)


def sanitize_key_part(s: str) -> str:
    """Every char outside [A-Za-z0-9_-] -> "_" (case kept)."""
    return _KEY_BAD.sub("_", s)


def key_for_info(extractor_key: str, video_id: str) -> str:
    """"<extractor_key lower>-<id>", e.g. ("Youtube", "zjkBMFhNj_g") -> "youtube-zjkBMFhNj_g".

    Skeleton decision: spec text says [a-z0-9_-] but its own examples keep the id's case
    and YouTube ids are case-sensitive, so the id keeps its case and the allowed set is
    [A-Za-z0-9_-] (sanitize_key_part on both parts; extractor part lowercased).
    """
    return f"{sanitize_key_part(extractor_key.lower())}-{sanitize_key_part(str(video_id))}"


def key_for_local(path: str | Path) -> str:
    """"local-" + sha1(first 1 MiB bytes + str(size).encode()).hexdigest()[:16].
    Stable across runs and renames; changes if the content/size changes."""
    p = Path(path)
    size = p.stat().st_size
    with open(p, "rb") as f:
        head = f.read(1 << 20)
    return "local-" + hashlib.sha1(head + str(size).encode()).hexdigest()[:16]


def is_url(s: str) -> bool:
    """True for http:// or https:// (case-insensitive scheme)."""
    return s[:8].lower().startswith(("http://", "https://"))


def normalize_input(s: str) -> str:
    """index.json lookup key for an input string.

    URL: lowercase scheme+host, strip "www."/"m." host prefix, drop fragment, drop
    DROP_QUERY_PARAMS and utm_*, sort remaining query params, strip trailing "/".
    YouTube short forms (youtu.be/<id>, /shorts/<id>, /live/<id>, /embed/<id>) map to
    youtube.com/watch?v=<id> so they share one index entry (cached rerun skips meta).
    Local path: "file:" + abspath + "#" + str(st_mtime_ns) + ":" + str(st_size).
    Raises FileNotFoundError for a non-URL that is not an existing file.
    """
    s = s.strip()
    if is_url(s):
        parts = urlsplit(s)
        host = (parts.hostname or "").lower()
        for prefix in ("www.", "m."):
            if host.startswith(prefix):
                host = host[len(prefix):]
                break
        netloc = host + (f":{parts.port}" if parts.port else "")
        query = [
            (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k not in DROP_QUERY_PARAMS and not k.startswith("utm_")
        ]
        path = parts.path.rstrip("/")
        yt = _YT_SHORT_RE.match(path) if host == "youtube.com" else None
        if host == "youtu.be" and _YT_ID_RE.fullmatch(path.lstrip("/")):
            yt_id = path.lstrip("/")
        elif yt:
            yt_id = yt.group(1)
        else:
            yt_id = None
        if yt_id:  # youtu.be/<id>, /shorts/<id>, /live/<id>, /embed/<id> -> watch?v=<id>
            netloc, path = "youtube.com", "/watch"
            query = [(k, v) for k, v in query if k != "v"] + [("v", yt_id)]
        query.sort()
        return urlunsplit((parts.scheme.lower(), netloc, path, urlencode(query), ""))
    p = Path(s).expanduser()
    if not p.is_file():
        raise FileNotFoundError(s)
    st = p.stat()
    return f"file:{p.resolve()}#{st.st_mtime_ns}:{st.st_size}"


def _ms(s: float) -> int:
    return round(s * 1000)


def range_tag(from_s: float | None, to_s: float | None, duration: float | None) -> str:
    """See module docstring. `to` is clamped to duration; callers pass meta duration.
    Unknown duration with an open end -> "r<from_ms>-end"."""
    start = max(0.0, from_s or 0.0)
    end = to_s
    if duration is not None and duration > 0:
        end = duration if end is None else min(end, duration)
        if start <= 0 and end >= duration:
            return "full"
    elif end is None:
        return "full" if start <= 0 else f"r{_ms(start)}-end"
    return f"r{_ms(start)}-{_ms(end)}"


def transcript_tag(rtag_range: str, lang: str | None) -> str:
    """range_tag + "-lang_<xx>" when lang forced."""
    return f"{rtag_range}-lang_{lang}" if lang else rtag_range


def view_tag(rtag_range: str, resolution: int) -> str:
    """range_tag + "-720" | "-1080"."""
    return f"{rtag_range}-{resolution}"


def params_hash(params: dict[str, Any]) -> str:
    """sha1 hex of json.dumps(params, sort_keys=True, separators=(",", ":"))."""
    blob = json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(blob.encode()).hexdigest()


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ViewPaths:
    dir: Path  # <key>/views/<vtag>
    vtag: str

    @property
    def frames_dir(self) -> Path:
        return self.dir / "frames"

    @property
    def frames_json(self) -> Path:
        return self.dir / "frames.json"

    @property
    def sheets_dir(self) -> Path:
        return self.dir / "sheets"

    @property
    def context_md(self) -> Path:
        return self.dir / "context.md"

    @property
    def windows_dir(self) -> Path:
        return self.dir / "windows"

    @property
    def visual_md(self) -> Path:
        return self.dir / "visual.md"


@dataclass(frozen=True)
class KeyPaths:
    root: Path  # cache root
    key: str

    @property
    def dir(self) -> Path:
        return self.root / self.key

    @property
    def run_lock(self) -> Path:
        return self.dir / RUN_LOCK

    @property
    def manifest(self) -> Path:
        return self.dir / MANIFEST

    @property
    def meta(self) -> Path:
        return self.dir / "meta.json"

    @property
    def info_json(self) -> Path:
        """Uncompressed yt-dlp -J for --load-info-json; deleted after downloads finish."""
        return self.dir / "info.json"

    @property
    def info_gz(self) -> Path:
        return self.dir / "info.json.gz"

    @property
    def media_dir(self) -> Path:
        return self.dir / "media"

    @property
    def transcripts_dir(self) -> Path:
        return self.dir / "transcripts"

    @property
    def zoom_dir(self) -> Path:
        return self.dir / "zoom"

    def media_stem(self, kind: str) -> Path:
        """kind "audio" | "video_720" | "video_1080" | "muxed" -> media/<kind> (no ext;
        yt-dlp -o appends ".%(ext)s")."""
        return self.media_dir / kind

    def transcript_json(self, rtag: str) -> Path:
        return self.transcripts_dir / f"{rtag}.json"

    def transcript_md(self, rtag: str) -> Path:
        return self.transcripts_dir / f"{rtag}.md"

    def transcript_partial(self, rtag: str) -> Path:
        """Append-only JSONL of chunk events while a job runs (skeleton decision:
        no resume in v0.1; deleted when a job (re)starts and after the final json is written)."""
        return self.transcripts_dir / f"{rtag}.partial.jsonl"

    def view(self, vtag: str) -> ViewPaths:
        return ViewPaths(self.dir / "views" / vtag, vtag)


def valid_key(key: str) -> bool:
    """A cache key is one path component of [A-Za-z0-9_-] (guards `frame`/`cache` args)."""
    return bool(key) and len(key) <= 200 and not _KEY_BAD.search(key) and key not in (RUNS_DIR,)


def key_paths(key: str, root: Path | None = None) -> KeyPaths:
    """KeyPaths for key under root (default cache_root())."""
    return KeyPaths(root or cache_root(), key)


# --------------------------------------------------------------------------
# JSON io
# --------------------------------------------------------------------------
def atomic_write_text(path: str | Path, text: str) -> None:
    """Write to <path>.tmp.<pid> then os.replace (parents created)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.tmp.{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, p)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def atomic_write_json(path: str | Path, obj: Any, *, indent: int | None = 1) -> None:
    """json.dumps(ensure_ascii=False) + atomic_write_text."""
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=indent) + "\n")


def read_json(path: str | Path, default: Any = None) -> Any:
    """Parsed JSON, or `default` when missing/corrupt (never raises for those)."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


# --------------------------------------------------------------------------
# index.json
# --------------------------------------------------------------------------
def _index_inputs(root: Path) -> dict[str, str]:
    data = read_json(root / INDEX_FILE, {})
    inputs = data.get("inputs") if isinstance(data, dict) else None
    return dict(inputs) if isinstance(inputs, dict) else {}


def index_get(norm_input: str, root: Path | None = None) -> str | None:
    """Key for a normalized input, only if <key>/manifest.json exists with meta done."""
    root = root or cache_root()
    key = _index_inputs(root).get(norm_input)
    if not key or not valid_key(key):
        return None
    return key if stage_fresh(load_manifest(key_paths(key, root)), "meta") else None


def _index_update(root: Path, fn: Any) -> None:
    with FileLock(root / INDEX_LOCK):
        inputs = _index_inputs(root)
        fn(inputs)
        atomic_write_json(root / INDEX_FILE, {"v": 1, "inputs": inputs}, indent=None)


def index_put(norm_input: str, key: str, root: Path | None = None) -> None:
    """Insert/overwrite under INDEX_LOCK (read-modify-write, atomic replace)."""
    _index_update(root or cache_root(), lambda d: d.__setitem__(norm_input, key))


def index_drop_keys(keys: set[str], root: Path | None = None) -> None:
    """Remove every index entry pointing at one of `keys`."""
    if not keys:
        return

    def drop(d: dict[str, str]) -> None:
        for k in [k for k, v in d.items() if v in keys]:
            del d[k]

    _index_update(root or cache_root(), drop)


# --------------------------------------------------------------------------
# manifest.json (spec 3)
# --------------------------------------------------------------------------
def new_manifest(key: str, source: dict[str, Any]) -> dict[str, Any]:
    """Fresh manifest: {"v":1,"key","skill_version":wfm.VERSION,"created","last_used" (ISO-8601 UTC),
    "source":{input,webpage_url,extractor,id,local_path},"tools":{},"media":{},"stages":{},"errors":[]}."""
    from . import VERSION

    now = _now_iso()
    src = {k: source.get(k) for k in ("input", "webpage_url", "extractor", "id", "local_path")}
    return {"v": 1, "key": key, "skill_version": VERSION, "created": now, "last_used": now,
            "source": src, "tools": {}, "media": {}, "stages": {}, "errors": []}


def load_manifest(kp: KeyPaths) -> dict[str, Any] | None:
    """Parsed manifest or None (missing/corrupt -> None; caller recreates)."""
    m = read_json(kp.manifest)
    if not isinstance(m, dict) or not isinstance(m.get("stages"), dict):
        return None
    m.setdefault("media", {})
    m.setdefault("tools", {})
    m.setdefault("errors", [])
    return m


def save_manifest(kp: KeyPaths, manifest: dict[str, Any]) -> None:
    """Atomic write; also sets last_used = now."""
    manifest["last_used"] = _now_iso()
    atomic_write_json(kp.manifest, manifest)


def set_stage(manifest: dict[str, Any], stage: str, status: str, *, params_hash: str | None = None,
              **extra: Any) -> None:
    """In-memory update of manifest["stages"][stage] = {"status", [params_hash], t_start/t_end, **extra}.
    "running" sets t_start (time.time()), final statuses set t_end. Caller saves."""
    stages = manifest.setdefault("stages", {})
    prev = stages.get(stage) or {}
    entry: dict[str, Any] = {"status": status}
    now = round(time.time(), 3)
    if status == "running":
        entry["t_start"] = now
    else:
        if "t_start" in prev:
            entry["t_start"] = prev["t_start"]
        if status in ("done", "error", "skipped"):
            entry["t_end"] = now
    if params_hash is not None:
        entry["params_hash"] = params_hash
    entry.update(extra)
    stages[stage] = entry


def stage_fresh(manifest: dict[str, Any] | None, stage: str, params_hash: str | None = None) -> bool:
    """True when stage status is "done" and (params_hash is None or matches)."""
    if not manifest:
        return False
    entry = (manifest.get("stages") or {}).get(stage)
    if not isinstance(entry, dict) or entry.get("status") != "done":
        return False
    return params_hash is None or entry.get("params_hash") == params_hash


def add_error(manifest: dict[str, Any], stage: str, error: dict[str, Any]) -> None:
    """Append {"stage", "at", **error} to manifest["errors"] (keep last 20)."""
    errs = manifest.setdefault("errors", [])
    errs.append({"stage": stage, "at": _now_iso(), **error})
    del errs[:-MAX_MANIFEST_ERRORS]


# --------------------------------------------------------------------------
# LRU eviction, listing, clearing
# --------------------------------------------------------------------------
@dataclass
class CacheEntry:
    key: str
    bytes: int
    last_used: str | None  # ISO from manifest
    title: str | None  # from meta.json


def dir_bytes(path: Path) -> int:
    """Recursive size of regular files (symlinks not followed)."""
    total = 0
    stack = [path]
    while stack:
        d = stack.pop()
        try:
            it = list(os.scandir(d))
        except OSError:
            continue
        for e in it:
            try:
                if e.is_symlink():
                    continue
                if e.is_dir(follow_symlinks=False):
                    stack.append(Path(e.path))
                elif e.is_file(follow_symlinks=False):
                    total += e.stat(follow_symlinks=False).st_size
            except OSError:
                continue
    return total


def _entry_last_used_ts(root: Path, e: CacheEntry) -> float:
    ts = _parse_iso(e.last_used)
    if ts is not None:
        return ts
    try:
        return (root / e.key).stat().st_mtime
    except OSError:
        return 0.0


def list_entries(root: Path | None = None) -> list[CacheEntry]:
    """Every <key>/ dir (skips runs/ and files), sorted by last_used descending."""
    root = root or cache_root()
    out: list[CacheEntry] = []
    try:
        dirs = [p for p in root.iterdir() if p.is_dir() and p.name != RUNS_DIR and valid_key(p.name)]
    except OSError:
        return []
    for d in dirs:
        man = read_json(d / MANIFEST, {}) or {}
        meta = read_json(d / "meta.json", {}) or {}
        out.append(CacheEntry(d.name, dir_bytes(d), man.get("last_used") if isinstance(man, dict) else None,
                              meta.get("title") if isinstance(meta, dict) else None))
    out.sort(key=lambda e: _entry_last_used_ts(root, e), reverse=True)
    return out


def _rmtree(p: Path) -> None:
    shutil.rmtree(p, ignore_errors=True)


def _key_busy(root: Path, key: str) -> bool:
    lock_file = root / key / RUN_LOCK
    return lock_file.exists() and FileLock(lock_file).is_held_elsewhere()


def evict_lru(protect: set[str], limit_bytes: int | None = None, root: Path | None = None) -> list[str]:
    """Delete least-recently-used key dirs until total <= limit (default max_bytes()).

    Never deletes keys in `protect` (index hits for the current run's inputs) nor any key whose
    run.lock is held elsewhere (FileLock.is_held_elsewhere). Also drops their index.json entries.
    Returns deleted keys. Called at run start.
    """
    root = root or cache_root()
    limit = max_bytes() if limit_bytes is None else limit_bytes
    entries = list_entries(root)
    total = sum(e.bytes for e in entries)
    deleted: list[str] = []
    for e in reversed(entries):  # oldest first
        if total <= limit:
            break
        if e.key in protect or _key_busy(root, e.key):
            continue
        _rmtree(root / e.key)
        total -= e.bytes
        deleted.append(e.key)
    index_drop_keys(set(deleted), root)
    return deleted


def clear(key: str | None = None, *, all_: bool = False, root: Path | None = None) -> list[str]:
    """`cache clear KEY` / `cache clear --all`: delete key dir(s) (+ index entries; --all
    also deletes runs/ of finished runs and index.json). Skips keys whose run.lock is held
    elsewhere. Returns deleted keys. Raises KeyError for an unknown KEY."""
    root = root or cache_root()
    if all_:
        keys = [e.key for e in list_entries(root)]
    else:
        if not key or not valid_key(key) or not (root / key).is_dir():
            raise KeyError(key)
        keys = [key]
    deleted = []
    for k in keys:
        if _key_busy(root, k):
            continue
        _rmtree(root / k)
        deleted.append(k)
    index_drop_keys(set(deleted), root)
    if all_:
        runs = root / RUNS_DIR
        if runs.is_dir():
            for d in runs.iterdir():
                rj = read_json(d / "run.json", {}) or {}
                if rj.get("state") == "running" and run_pid_alive(rj):
                    continue
                _rmtree(d)
    return deleted


# --------------------------------------------------------------------------
# runs/<run_id>/
# --------------------------------------------------------------------------
def new_run_id() -> str:
    """"wfm" + 6 chars of [a-z0-9] (secrets.choice)."""
    alphabet = string.ascii_lowercase + string.digits
    return "wfm" + "".join(secrets.choice(alphabet) for _ in range(6))


def valid_run_id(run_id: str) -> bool:
    """[A-Za-z0-9_-]{1,64}: guards path traversal in --run/--run-id."""
    return bool(_RUN_ID_RE.match(run_id or ""))


def run_dir(run_id: str, root: Path | None = None) -> Path:
    """<root>/runs/<run_id> (created)."""
    if not valid_run_id(run_id):
        raise ValueError(f"invalid run id {run_id!r}")
    d = (root or cache_root()) / RUNS_DIR / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_run_json(run_id: str, data: dict[str, Any], root: Path | None = None) -> None:
    """Atomic write of runs/<id>/run.json.

    Shape (spec 3 + skeleton additions marked +):
    {"v":1,"run_id","pid","worker_pid":int|null,"started":ISO,"inputs":[...],"keys":[...],
     "flags":RunOptions.flags_dict(),
     +"started_ts":float,               # epoch seconds (for wait's elapsed_s)
     +"pid_start":float|null,           # pid's start time (epoch s): pid-reuse guard for wait/cancel
     +"detached":bool,                  # pid is a session leader (cancel may signal its group)
     +"state":"running"|"done"|"cancelled"|"crashed",
     +"frames_reached":bool,            # every video reached frames (done/skipped/error)
     +"exit":int|null,                  # run exit code once finished
     +"videos":[VideoResult.to_dict()], # live per-video state, same shape as WFM_RESULT
     +"result":RunResult.to_dict()|null # the final WFM_RESULT object
     +"updated":ISO}
    Rewritten by the run on every per-video stage transition.
    """
    data = dict(data)
    data["updated"] = _now_iso()
    atomic_write_json(run_dir(run_id, root) / "run.json", data, indent=None)


def read_run_json(run_id: str, root: Path | None = None) -> dict[str, Any] | None:
    if not valid_run_id(run_id):
        return None
    d = read_json((root or cache_root()) / RUNS_DIR / run_id / "run.json")
    return d if isinstance(d, dict) else None


def prune_runs(days: int = RUN_PRUNE_DAYS, root: Path | None = None) -> list[str]:
    """Delete runs/<id>/ dirs whose run.json `started` (or dir mtime) is older than `days`
    and whose pid is not alive. Returns deleted run ids."""
    runs = (root or cache_root()) / RUNS_DIR
    if not runs.is_dir():
        return []
    cutoff = time.time() - days * 86400
    deleted = []
    for d in runs.iterdir():
        if not d.is_dir():
            continue
        rj = read_json(d / "run.json", {}) or {}
        started = rj.get("started_ts") or _parse_iso(rj.get("started"))
        if started is None:
            try:
                started = d.stat().st_mtime
            except OSError:
                continue
        if started < cutoff and not run_pid_alive(rj):
            _rmtree(d)
            deleted.append(d.name)
    return deleted


def pid_alive(pid: int | None) -> bool:
    """os.kill(pid, 0) on POSIX (PermissionError -> alive); Windows: OpenProcess via ctypes.
    None -> False."""
    if not pid or not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


PROC_START_TOL_S = 3.0  # `ps etime` has 1 s resolution; the recorded time is taken right after spawn
_ETIME_RE = re.compile(r"^(?:(?:(\d+)-)?(\d+):)?(\d+):(\d+)$")


def process_start(pid: int | None) -> float | None:
    """Epoch start time of `pid` (1 s resolution) from `ps -o etime=` (POSIX, locale-free),
    or None when unknown (Windows, no ps, dead pid)."""
    if not pid or not isinstance(pid, int) or pid <= 0 or os.name != "posix":
        return None
    import subprocess

    try:
        r = subprocess.run(["ps", "-o", "etime=", "-p", str(pid)], capture_output=True, text=True,
                           timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = _ETIME_RE.match(r.stdout.strip())
    if r.returncode != 0 or not m:
        return None
    d, h, mi, se = (int(g or 0) for g in m.groups())
    return time.time() - (((d * 24 + h) * 60 + mi) * 60 + se)


def same_process(pid: int | None, started: float | None) -> bool:
    """pid is alive AND is the process that started at `started` (epoch s, recorded at spawn).
    Guards every signal we send to a pid read back from disk against pid reuse. `started`
    None (older run.json) or an unknown start time (Windows) falls back to pid_alive."""
    if not pid_alive(pid):
        return False
    if not isinstance(started, (int, float)):
        return True
    now_start = process_start(pid)
    return now_start is None or abs(now_start - started) <= PROC_START_TOL_S


def run_pid_alive(rj: dict[str, Any]) -> bool:
    """The run.json `pid` is alive and still the run's process (`pid_start`, see same_process)."""
    return same_process(rj.get("pid"), rj.get("pid_start"))
