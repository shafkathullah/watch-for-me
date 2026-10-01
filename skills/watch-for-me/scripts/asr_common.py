"""ASR shared code: JSONL protocol (worker <-> wfm.asr_client), PCM decode,
silence cuts, language-switch splitting, routing and the per-job loop.

IMPORT RULE: this module is imported by BOTH sides:
  - wfm/asr_client.py, running in the watch.py env (stdlib + Pillow, NO numpy):
    it may only use the protocol constants, `encode`/`decode`, model-id and
    HF-cache helpers, which are stdlib-only.
  - asr_mlx.py / asr_cpu.py (numpy available): everything.
Therefore: NO top-level `import numpy`. DSP functions import numpy inside.

Protocol (spec 4.5; skeleton additions marked +):

  client -> worker (one JSON object per line on stdin):
    {"op":"job","job_id":"j1","key":"…","audio":"/abs/media/audio.webm","from":null,"to":null,"lang":null}
    {"op":"prefetch","roles":["parakeet","lid","whisper"]}                 (+ used by `watch.py setup`)
    {"op":"shutdown"}
    stdin EOF == shutdown (no orphans if the orchestrator dies).

  worker -> client (one JSON object per line on stdout; logs go to stderr):
    {"ev":"ready","backend":"mlx","load_s":1.3,"versions":{"parakeet_mlx":"0.5.2","mlx_whisper":"0.4.3"}}  (+versions)
    {"ev":"model","model":"<hf id or alias>","status":"downloading"|"done","mb":1610}
    {"ev":"model","model":"<id>","status":"progress","mb":1610,"got_mb":412,"s":120}  (+ every ~60 s while downloading)
    {"ev":"waiting","job_id":"j1","reason":"asr.lock"}      (+ job queued behind another session's job)
    {"ev":"chunk","job_id":"j1","t0":0.0,"t1":61.3,"lang":"en","lang_p":0.99,"engine":"parakeet",
     "model":"<id>","wall_s":1.3,"segments":[{"t0":0.42,"t1":4.1,"text":"…"}]}               (+model, wall_s)
    {"ev":"done","job_id":"j1","audio_s":3588,"wall_s":99.5,"decode_s":2.6,"lid_s":3.1}   (+decode_s,lid_s)
    {"ev":"prefetched","models":{"<id>":<mb>}}                                                     (+)
      a failed prefetch ends with {"ev":"error","job_id":"prefetch",...} (not fatal to the worker)
    {"ev":"error","job_id":"j1"|null,"code":"asr_failed"|"no_audio"|"model_download_failed"|"disk_full","message":"…"}

  Pinned semantics:
  - `from`/`to` are absolute seconds or null; every t0/t1 the worker emits is
    ABSOLUTE (range start already added).
  - One `chunk` event per chunk, in time order, including silent/music chunks
    (then `segments: []`, and `"skipped":"silence"` for chunks that never reached
    an engine). Segment times are absolute and inside [chunk.t0, chunk.t1].
  - A job ends with exactly one `done` or one `error` (job_id set). An error
    with job_id null is fatal to the worker; it exits non-zero afterwards.
  - `ready` is sent once after startup (models possibly preloaded, see
    run_worker). Clients must ignore unknown events and unknown fields.
  - The worker holds `wfm.cache.FileLock(<CACHE>/asr.lock)` for the duration
    of each job (and of a prefetch), and of the startup preload when it gets the
    lock without waiting; a worker that finds the lock busy at startup loads its
    models lazily inside its first job (spec 4.1: lock BEFORE model load).

Worker command line (built by wfm.asr_client.worker_command):
    uv run [--locked] --script <scripts>/asr_mlx.py --lock <CACHE>/asr.lock
Models come from env (WFM_PARAKEET_MODEL, WFM_WHISPER_MODEL, WFM_LID_MODEL,
HF_HOME, HF_TOKEN), inherited from the orchestrator.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:  # numpy only exists in the worker envs
    from typing import Self

    import numpy as np

# --------------------------------------------------------------------------
# Protocol constants (IMPLEMENTED)
# --------------------------------------------------------------------------
PROTOCOL_V = 1

OP_JOB = "job"
OP_PREFETCH = "prefetch"
OP_SHUTDOWN = "shutdown"

EV_READY = "ready"
EV_MODEL = "model"
EV_CHUNK = "chunk"
EV_DONE = "done"
EV_PREFETCHED = "prefetched"
EV_ERROR = "error"
EV_WAITING = "waiting"

MODEL_DOWNLOADING = "downloading"
MODEL_PROGRESS = "progress"
MODEL_DONE = "done"

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


# ~60 s between download progress events; WFM_MODEL_PROGRESS_S overrides (tests only, undocumented)
MODEL_PROGRESS_EVERY_S = _env_float("WFM_MODEL_PROGRESS_S", 60.0)

ENGINE_PARAKEET = "parakeet"
ENGINE_WHISPER = "whisper"
ROLES = ("parakeet", "lid", "whisper")  # model roles

PREFETCH_ID = "prefetch"  # job_id of a prefetch's EV_WAITING / EV_ERROR (a non-null id: not fatal)
WORKER_ERROR_CODES = ("asr_failed", "no_audio", "model_download_failed", "disk_full")

# --------------------------------------------------------------------------
# Algorithm constants (spec 4.5 "Per job algorithm")
# --------------------------------------------------------------------------
SR = 16000
RMS_FRAME = 480  # samples
RMS_HOP = 240  # samples -> 15 ms per rms value
CHUNK_T = 60.0  # target chunk length, s
CUT_BEFORE = 20.0  # search window [prev+T-20, prev+T+10]
CUT_AFTER = 10.0  # last chunk absorbs a remainder <= T+10
LID_WINDOW = 30.0  # s, global grid from the decoded range start
LID_SPLIT_SLACK = 5.0  # split within +-5 s of a disagreeing window boundary
LID_FINE_WINDOW = 10.0  # s, switch localization windows (padded to 30 s for whisper LID)
LID_FINE_HOP = 5.0
LID_PAUSE_SMOOTH = 0.4  # s, rms smoothing when placing a language-switch cut (fallback)
LID_PIECE_MIN_P = 0.5  # sentence-level LID pieces are short; accept a lower top prob
LID_PIECE_MIN_S = 1.0  # pieces shorter than this do not vote
PAUSE_DBFS = -45.0  # pause detection for switch placement
PAUSE_REL = 0.15  # ... or below 15% of the span's median rms (background music)
PAUSE_MIN_S = 0.15
PAUSE_MARGIN = 3.0  # s widened around the fine-LID interval when looking for pauses
LID_MIXED_P = 0.95  # a voiced 30 s window below this gets fine LID (may hold a switch)
LID_MIN_P = 0.7  # top prob below this -> route to Parakeet (stays silent on non-speech)
SILENCE_DBFS = -50.0
SILENCE_FRAC = 0.95
PARAKEET_LANGS = frozenset({"en"})
HOLE_MIN_S = 2.5  # uncovered voiced span inside a Parakeet chunk that gets a second pass
HOLE_VOICED_FRAC = 0.5  # ... when at least this share of its rms frames is above PAUSE_DBFS
HOLE_CONTEXT_S = 0.5  # audio kept on each side of a hole for the second pass
PARAKEET_TAIL_PAD = 2.0  # s of zeros appended to every Parakeet chunk: TDT drops the last ~2 s of
# speech when the input ends right after it (injection.mp4: "dollar temp dir, slash pwned" lost)
# Whisper segment filter (step 4b/5): drop when
#   avg_logprob < -1.0  OR  compression_ratio > 2.4  OR  (no_speech_prob > 0.6 and avg_logprob < -1.0)
WHISPER_MIN_AVG_LOGPROB = -1.0
WHISPER_MAX_COMPRESSION = 2.4
WHISPER_NO_SPEECH = 0.6
NON_SPEECH_TAGS = frozenset({"music", "musique", "música", "musica", "musik", "musica di sottofondo", "bgm",
                             "applause", "laughter", "silence", "instrumental", "音楽", "音乐", "музыка"})
CREDIT_RE = re.compile(r"amara\.org|sous-titr|subtitle|untertitel|legenda|sottotitoli|subtítulos|字幕|ご視聴"
                       r"|продолжение следует|редактор субтитров|チャンネル登録", re.IGNORECASE)
# ja/zh/ko scripts: Han, kana (incl. half-width), Hangul. Written without spaces (ja/zh), so a
# whitespace split counts a whole 10 s sentence as 1 word [M: Japanese talk, two real ~10 s
# segments dropped by the < 0.3 words/s rule, tokens_est 267 for 9 min of speech].
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f\uac00-\ud7af]")
CJK_CHAR_WORDS = 0.75  # one CJK char ~ 1 token ~ 0.75 word at plan.TOKENS_PER_WORD 1.35

# --------------------------------------------------------------------------
# Models (IMPLEMENTED; stdlib; used by worker, asr_client params_hash, doctor)
# --------------------------------------------------------------------------
DEFAULT_MODELS: dict[str, dict[str, str]] = {
    "mlx": {
        "parakeet": "mlx-community/parakeet-tdt-0.6b-v3",
        "whisper": "mlx-community/whisper-large-v3-turbo",
        "lid": "mlx-community/whisper-tiny-mlx",
    },
    "cpu": {
        # onnx-asr alias; resolves to istupakov/parakeet-tdt-0.6b-v3-onnx
        "parakeet": "nemo-parakeet-tdt-0.6b-v3",
        # spec 4.5: `large-v3-turbo` alias 307-redirects mobiuslabsgmbh -> dropbox-dash.
        # Pins the target repo id directly + a revision (MODEL_REVISIONS).
        "whisper": "dropbox-dash/faster-whisper-large-v3-turbo",
        "lid": "Systran/faster-whisper-tiny",
    },
}
ENV_FOR_ROLE = {"parakeet": "WFM_PARAKEET_MODEL", "whisper": "WFM_WHISPER_MODEL", "lid": "WFM_LID_MODEL"}
# Alias -> HF repo actually downloaded (for cache checks / sizes).
HF_REPO_FOR_ALIAS: dict[str, str] = {
    "nemo-parakeet-tdt-0.6b-v3": "istupakov/parakeet-tdt-0.6b-v3-onnx",
    "silero": "istupakov/silero-vad-onnx",
    "large-v3-turbo": "dropbox-dash/faster-whisper-large-v3-turbo",
    "tiny": "Systran/faster-whisper-tiny",
}
# Revision pins for the DEFAULT repos (HF API sha, 2026-09-29). Applied only when the
# configured id equals the repo (env overrides float). A cached snapshot of the pinned
# revision loads with no network call; otherwise snapshot_download(revision=pin).
MODEL_REVISIONS: dict[str, str] = {
    "mlx-community/parakeet-tdt-0.6b-v3": "ed2b7e8c15f9aaa0b5772e2efb986255eaef7e15",
    "mlx-community/whisper-large-v3-turbo": "a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb",
    "mlx-community/whisper-tiny-mlx": "6caf9c55601caafbe6508a8b0d216bdf4783c4e8",
    "dropbox-dash/faster-whisper-large-v3-turbo": "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf",
    "Systran/faster-whisper-tiny": "d90ca5fe260221311c53c58e660288d3deb8d356",
}
# Approximate download sizes in MB (spec 4.5 "Model downloads" [M]).
MODEL_SIZES_MB: dict[str, int] = {
    "mlx-community/parakeet-tdt-0.6b-v3": 2510,
    "mlx-community/whisper-large-v3-turbo": 1610,
    "mlx-community/whisper-tiny-mlx": 74,
    "istupakov/parakeet-tdt-0.6b-v3-onnx": 670,
    "istupakov/silero-vad-onnx": 6,
    "dropbox-dash/faster-whisper-large-v3-turbo": 1620,
    "Systran/faster-whisper-tiny": 76,
}


def model_ids(backend: str, env: dict[str, str] | None = None) -> dict[str, str]:
    """{"parakeet","whisper","lid"} -> model id for `backend`, env overrides applied."""
    e = os.environ if env is None else env
    return {role: e.get(ENV_FOR_ROLE[role]) or DEFAULT_MODELS[backend][role] for role in ROLES}


def hf_hub_dir() -> Path:
    """HF hub cache dir: $HF_HUB_CACHE, else $HF_HOME/hub, else ~/.cache/huggingface/hub."""
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"])
    home = os.environ.get("HF_HOME") or os.path.join(os.path.expanduser("~"), ".cache", "huggingface")
    return Path(home) / "hub"


def hf_repo_id(model: str) -> str | None:
    """Model id/alias -> HF repo id; None for a local directory path."""
    if os.path.isdir(model):
        return None
    return HF_REPO_FOR_ALIAS.get(model, model)


def hf_repo_cached(model: str) -> bool:
    """True when the repo has at least one snapshot in the HF cache (or `model` is a local dir).

    Stdlib only: checks <hub>/models--<org>--<name>/snapshots/*/ is non-empty.
    Does not verify completeness (an interrupted download may still pass).
    """
    repo = hf_repo_id(model)
    if repo is None:
        return True
    snaps = hf_hub_dir() / ("models--" + repo.replace("/", "--")) / "snapshots"
    try:
        return any(any(p.iterdir()) for p in snaps.iterdir() if p.is_dir())
    except OSError:
        return False


def model_size_mb(model: str) -> int | None:
    repo = hf_repo_id(model)
    return MODEL_SIZES_MB.get(repo) if repo else None


# --------------------------------------------------------------------------
# JSONL codec (IMPLEMENTED)
# --------------------------------------------------------------------------
def encode(msg: dict[str, Any]) -> str:
    """dict -> one JSON line (no trailing newline), UTF-8 safe, compact."""
    return json.dumps(msg, ensure_ascii=False, separators=(",", ":"))


def decode(line: str | bytes) -> dict[str, Any] | None:
    """One line -> dict, or None for blank / non-JSON / non-object lines
    (workers' libraries sometimes print to stdout; clients skip such lines)."""
    if isinstance(line, bytes):
        line = line.decode("utf-8", errors="replace")
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def emit(msg: dict[str, Any], stream: Any = None) -> None:
    """Worker side: write one event line to the REAL stdout and flush (thread-safe:
    the download-progress thread emits too).

    Workers must call `protect_stdout()` first so library prints cannot corrupt the stream.
    """
    out = stream or _REAL_STDOUT or sys.stdout
    with _EMIT_LOCK:
        out.write(encode(msg) + "\n")
        out.flush()


_REAL_STDOUT: Any = None
_EMIT_LOCK = threading.Lock()


def protect_stdout() -> None:
    """Worker side: keep the real stdout for `emit` and point both sys.stdout AND
    file descriptor 1 at stderr, so model libraries (tqdm, prints, C-level writes
    from onnxruntime/ctranslate2) never write into the protocol stream."""
    global _REAL_STDOUT
    if _REAL_STDOUT is not None:
        return
    try:
        sys.stdout.flush()
        fd = os.dup(1)
        os.dup2(2, 1)
        _REAL_STDOUT = open(fd, "w", encoding="utf-8", newline="\n")  # noqa: SIM115 (process lifetime)
    except (OSError, ValueError, AttributeError):  # no real fds (tests, embedded): keep the object
        _REAL_STDOUT = sys.stdout
    sys.stdout = sys.stderr




# --------------------------------------------------------------------------
# Model resolution + download events (worker side; huggingface_hub imported lazily)
# --------------------------------------------------------------------------
def model_revision(model: str) -> str | None:
    """Pinned revision for a default repo id (None for overrides / local dirs)."""
    repo = hf_repo_id(model)
    return MODEL_REVISIONS.get(repo) if repo else None


def hf_repo_dir(model: str) -> Path | None:
    repo = hf_repo_id(model)
    return hf_hub_dir() / ("models--" + repo.replace("/", "--")) if repo else None


def hf_local_snapshot(model: str) -> Path | None:
    """Local dir to load `model` from WITHOUT a network call, or None if not cached.

    Local dir -> itself. Pinned repo -> snapshots/<pin> only. Otherwise the snapshot
    refs/main points at, else the newest snapshot. Completeness is not verified; a
    loader failure on it falls back to a download (see load_model_with_events).
    """
    if os.path.isdir(model):
        return Path(model)
    base = hf_repo_dir(model)
    if base is None:
        return None
    snaps = base / "snapshots"
    pin = model_revision(model)
    cands: list[Path] = []
    if pin:
        cands.append(snaps / pin)
    else:
        try:
            cands.append(snaps / (base / "refs" / "main").read_text().strip())
        except OSError:
            pass
        try:
            cands += sorted((p for p in snaps.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            pass
    for c in cands:
        try:
            if c.is_dir() and any(c.iterdir()):
                return c
        except OSError:
            continue
    return None


def hf_snapshot_download(model: str, allow_patterns: list[str] | None = None) -> Path:
    """Download (or complete) `model` at its pinned revision; returns the snapshot dir."""
    from huggingface_hub import snapshot_download

    repo = hf_repo_id(model)
    if repo is None:
        return Path(model)
    return Path(snapshot_download(repo_id=repo, revision=model_revision(model), allow_patterns=allow_patterns))


def repo_bytes(model: str) -> int:
    """Bytes of the repo's blobs, incl. *.incomplete downloads. Follows symlinks:
    huggingface_hub >= 2.0 moves finished xet blobs to a shared store under
    <hub>/blobs/ and leaves a symlink in the repo's blobs/ [M: hub 2.0.0]."""
    base = hf_repo_dir(model)
    if base is None:
        return 0
    total = 0
    try:
        entries = list((base / "blobs").iterdir())
    except OSError:
        return 0
    for f in entries:
        try:
            total += f.stat().st_size
        except OSError:
            continue
    return total


class _DownloadTicker:
    """Emits EV_MODEL progress {mb, got_mb, s} every `every` s while a download runs
    (bytes measured by repo_bytes, incl. *.incomplete blobs). It is a heartbeat first:
    with hf_xet transfers (hub >= 1.0) the blob file grows in bursts, so got_mb can sit
    at 0 for minutes and then jump [M: turbo 1.61 GB at ~3 MB/s: 0 MB for ~4 min]."""

    def __init__(self, model: str, emit_fn: Callable[[dict[str, Any]], None], every: float) -> None:
        self.model, self.emit_fn, self.every = model, emit_fn, every
        self.start_bytes = repo_bytes(model)
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, name="wfm-dl-ticker", daemon=True)

    def got_mb(self) -> float:
        return round(max(0, repo_bytes(self.model) - self.start_bytes) / 1e6, 1)

    def _run(self) -> None:
        t0 = time.monotonic()
        while not self._stop.wait(self.every):
            try:
                self.emit_fn({"ev": EV_MODEL, "model": self.model, "status": MODEL_PROGRESS,
                              "mb": model_size_mb(self.model), "got_mb": self.got_mb(),
                              "s": round(time.monotonic() - t0)})
            except Exception:  # noqa: BLE001 (broken pipe etc.: main thread handles it)
                return

    def __enter__(self) -> Self:
        self._t.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._t.join(timeout=2)


def _is_disk_full(exc: BaseException) -> bool:
    e: BaseException | None = exc
    while e is not None:
        if isinstance(e, OSError) and e.errno == errno.ENOSPC:
            return True
        if "No space left on device" in str(e):
            return True
        e = e.__cause__ or e.__context__
    return False


def load_model_with_events(model: str, load: Callable[[Path | None], Any],
                           emit_fn: Callable[[dict[str, Any]], None],
                           download: Callable[[], Path | None] | None = None,
                           every: float = MODEL_PROGRESS_EVERY_S, self_download: bool = False) -> Any:
    """Load one model, downloading it first when it is not in the HF cache.

    `load(path)` gets the local snapshot dir (no network). `download()` fetches the
    model (default: hf_snapshot_download at the pinned revision) and returns the dir
    to load from. `self_download=True`: the loader resolves and fetches its own
    files (onnx-asr) -> `load(None)` runs inside the download phase instead.

    Cached: load(snapshot); on failure (incomplete snapshot) fall through to download.
    Not cached: EV_MODEL downloading {mb} -> progress {mb, got_mb} every ~60 s ->
    done {mb actual, s}. Raises AsrError("disk_full" | "model_download_failed" | "asr_failed").
    """
    snap = hf_local_snapshot(model)
    if snap is not None:
        try:
            return load(None if self_download else snap)
        except Exception as exc:
            if _is_disk_full(exc):
                raise AsrError("disk_full", f"{model}: {exc}") from exc
            print(f"[asr] cached {model} failed to load ({exc!r}); re-downloading", file=sys.stderr)
    dl = download or (lambda: hf_snapshot_download(model))
    emit_fn({"ev": EV_MODEL, "model": model, "status": MODEL_DOWNLOADING, "mb": model_size_mb(model)})
    t0 = time.monotonic()
    loaded: Any = None
    try:
        with _DownloadTicker(model, emit_fn, every) as ticker:
            if self_download:
                loaded = load(None)
            else:
                path = dl()
            got = ticker.got_mb()
    except Exception as exc:
        code = "disk_full" if _is_disk_full(exc) else "model_download_failed"
        raise AsrError(code, f"{model}: {type(exc).__name__}: {exc}") from exc
    emit_fn({"ev": EV_MODEL, "model": model, "status": MODEL_DONE, "mb": got or model_size_mb(model),
             "s": round(time.monotonic() - t0, 1)})
    if self_download:
        return loaded
    try:
        return load(path)
    except Exception as exc:
        raise AsrError("asr_failed", f"loading {model}: {type(exc).__name__}: {exc}") from exc


# --------------------------------------------------------------------------
# DSP + routing (numpy imported inside)
# --------------------------------------------------------------------------
class AsrError(Exception):
    """Worker-side failure with a protocol error code (WORKER_ERROR_CODES)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = " ".join(message.split())[:400]


@dataclass
class LangWindow:
    """LID result for one 30 s grid window [t0, t1) (seconds relative to pcm start).
    lang "" = not detected (silent window, LID skipped)."""

    t0: float
    t1: float
    lang: str
    p: float


@dataclass
class ChunkPlan:
    """A chunk to transcribe: [t0, t1) relative to pcm start, with its language."""

    t0: float
    t1: float
    lang: str | None = None  # None until LID ran (or forced)
    lang_p: float = 1.0


_NO_AUDIO_MARKERS = ("does not contain any stream", "matches no streams", "Output file is empty",
                     "Output file #0 does not contain")


def decode_pcm(audio: str, from_s: float | None = None, to_s: float | None = None) -> np.ndarray:
    """Decode `audio` to float32 mono 16 kHz in [-1, 1] through an ffmpeg pipe.

    Command: ffmpeg -nostdin -v error [-ss FROM] [-t TO-FROM] -i AUDIO -vn -f s16le -ac 1 -ar 16000 -
    (-ss/-t as INPUT options: `-t` instead of the spec's `-to` because an input-side
    `-to` needs ffmpeg >= 5.1; same range). No temp wav on disk.

    Raises:
        AsrError("no_audio"): no audio stream or zero samples decoded.
        AsrError("asr_failed"): ffmpeg failed for another reason (stderr tail in message).
    """
    import numpy as np

    if not os.path.exists(audio):
        raise AsrError("asr_failed", f"audio file missing: {audio}")
    cmd = ["ffmpeg", "-nostdin", "-v", "error"]
    start = float(from_s or 0.0)
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    if to_s is not None:
        if to_s <= start:
            raise AsrError("no_audio", f"empty range {start}-{to_s}")
        cmd += ["-t", f"{to_s - start:.3f}"]
    cmd += ["-i", audio, "-vn", "-map", "0:a:0", "-f", "s16le", "-ac", "1", "-ar", str(SR), "-"]
    try:
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, check=False)
    except FileNotFoundError as exc:
        raise AsrError("asr_failed", "ffmpeg not found on PATH") from exc
    err = r.stderr.decode("utf-8", errors="replace").strip()
    if r.returncode != 0:
        if any(m in err for m in _NO_AUDIO_MARKERS):
            raise AsrError("no_audio", "no audio stream")
        if "No space left" in err:
            raise AsrError("disk_full", err[-300:])
        raise AsrError("asr_failed", f"ffmpeg exit {r.returncode}: {err[-300:]}")
    n = len(r.stdout) // 2
    if n < SR // 10:  # < 0.1 s
        raise AsrError("no_audio", "no audio samples decoded" + (f" ({err[-200:]})" if err else ""))
    pcm = np.frombuffer(r.stdout[: n * 2], dtype=np.int16).astype(np.float32)
    pcm /= 32768.0
    return pcm


def frame_rms(pcm: np.ndarray) -> np.ndarray:
    """RMS over RMS_FRAME-sample frames every RMS_HOP samples, centered like
    np.sqrt(np.convolve(pcm**2, ones(480)/480, 'same')[::240] + 1e-12) (the
    prototype), but O(n) and low-memory: value k covers samples [k*240-240, k*240+240).
    One value per 15 ms; len == ceil(len(pcm)/240)."""
    import numpy as np

    n = len(pcm)
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    nb = -(-n // RMS_HOP)
    x = np.zeros(nb * RMS_HOP, dtype=np.float32)
    x[:n] = pcm
    blocks = np.einsum("ij,ij->i", x.reshape(nb, RMS_HOP), x.reshape(nb, RMS_HOP)).astype(np.float64)
    prev = np.concatenate(([0.0], blocks[:-1]))
    return np.sqrt((prev + blocks) / RMS_FRAME + 1e-12).astype(np.float32)


def silence_cuts(pcm: np.ndarray, sr: int = SR, target: float = CHUNK_T,
                 before: float = CUT_BEFORE, after: float = CUT_AFTER,
                 rms: np.ndarray | None = None) -> list[float]:
    """Chunk boundaries in seconds relative to pcm start: [0.0, c1, ..., duration].

    Each cut = quietest rms point in [prev + target - before, prev + target + after];
    loop while duration - prev > target + after, so the last chunk absorbs a
    remainder <= target + after. Audio <= target + after -> [0.0, duration].
    Port of scratchpad/asr/route.py `cuts_for`. `rms` may be passed to avoid recomputing.
    """
    import numpy as np

    dur = len(pcm) / sr
    r = frame_rms(pcm) if rms is None else rms
    hop_s = RMS_HOP / sr
    cuts = [0.0]
    while dur - cuts[-1] > target + after:
        a = int((cuts[-1] + target - before) / hop_s)
        b = min(len(r), int((cuts[-1] + target + after) / hop_s))
        if b <= a:
            break
        cuts.append(round((a + int(np.argmin(r[a:b]))) * hop_s, 3))
    return [*cuts, dur]


def lid_grid(duration: float, window: float = LID_WINDOW) -> list[tuple[float, float]]:
    """Global LID grid [(0,30),(30,60),...,(k*30,duration)] relative to pcm start.
    A final window shorter than 5 s is merged into the previous one."""
    if duration <= 0:
        return []
    out: list[tuple[float, float]] = []
    t = 0.0
    while t < duration - 1e-9:
        out.append((t, min(t + window, duration)))
        t += window
    if len(out) > 1 and out[-1][1] - out[-1][0] < 5.0:
        last = out.pop()
        out[-1] = (out[-1][0], last[1])
    return out


def _quietest(rms: np.ndarray, hop_s: float, lo: float, hi: float, smooth_s: float = 0.0) -> float | None:
    """Time of the lowest rms in [lo, hi). smooth_s > 0 first averages rms over that
    span, so the middle of the LONGEST pause wins over a brief dip between words."""
    import numpy as np

    a, b = max(0, int(lo / hop_s)), min(len(rms), int(hi / hop_s))
    if b <= a:
        return None
    r = rms[a:b]
    k = int(smooth_s / hop_s)
    if k > 1 and len(r) > k:
        r = np.convolve(r.astype(np.float64), np.ones(k) / k, "same")
    return round((a + int(np.argmin(r))) * hop_s, 3)


def find_switches(windows: list[LangWindow], both: bool = False) -> list[tuple[LangWindow, LangWindow]]:
    """Candidate language switches: adjacent VOICED windows (silent ones skipped) whose
    languages differ while at least one side is confident (p >= LID_MIN_P); `both=True`
    requires both sides (the coarse-only rule, where no fine LID can confirm).
    A 30 s window that straddles a switch often scores < 0.7 [M: 0.69 on long.wav],
    so requiring both sides missed real switches; fine LID confirms them instead."""
    voiced = [w for w in windows if w.lang]
    ok = (lambda a, b: a >= LID_MIN_P and b >= LID_MIN_P) if both else (lambda a, b: max(a, b) >= LID_MIN_P)
    return [(w1, w2) for w1, w2 in pairwise(voiced) if w1.lang != w2.lang and ok(w1.p, w2.p)]


def fine_grid(r0: float, r1: float, win: float = LID_FINE_WINDOW, hop: float = LID_FINE_HOP) -> list[tuple[float, float]]:
    """Overlapping short LID windows covering [r0, r1] (for switch localization)."""
    out: list[tuple[float, float]] = []
    t = r0
    while t < r1 - 1e-9:
        out.append((t, min(t + win, r1)))
        if t + win >= r1:
            break
        t += hop
    return out


def refine_regions(windows: list[LangWindow]) -> list[tuple[float, float]]:
    """Spans that get fine LID: both windows of every find_switches pair, plus every
    voiced window scoring below LID_MIXED_P (a window that holds two languages scores
    lower [M: 0.69-0.93 mixed vs >= 0.96 pure on long.wav; >= 0.99 on English talks],
    and a short switch inside ONE window is otherwise invisible). Merged, sorted."""
    regs = [(w1.t0, w2.t1) for w1, w2 in find_switches(windows)]
    regs += [(w.t0, w.t1) for w in windows if w.lang and w.p < LID_MIXED_P]
    out: list[tuple[float, float]] = []
    for r0, r1 in sorted(regs):
        if out and r0 <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], r1))
        else:
            out.append((r0, r1))
    return out


def fine_switches(fine: list[LangWindow]) -> list[tuple[float, float, str, str]]:
    """Switch intervals from fine windows: runs of consecutive confident (p >= LID_MIN_P)
    windows, in centre order; between two runs of different languages the switch lies
    between the last centre of one and the first centre of the next (a window that
    straddles the switch scores low and is skipped). -> [(lo, hi, lang_a, lang_b)]."""
    ws = sorted((w for w in fine if w.lang and w.p >= LID_MIN_P), key=lambda w: w.t0 + w.t1)
    out = []
    for w1, w2 in pairwise(ws):
        if w1.lang != w2.lang:
            out.append(((w1.t0 + w1.t1) / 2, (w2.t0 + w2.t1) / 2, w1.lang, w2.lang))
    return out


def pause_points(rms: np.ndarray, hop_s: float, lo: float, hi: float) -> list[tuple[float, float]]:
    """(middle, length) of pauses in [lo, hi): runs >= PAUSE_MIN_S of rms (smoothed 0.1 s)
    below max(PAUSE_DBFS, PAUSE_REL x median rms of the span)."""
    import numpy as np

    a, b = max(0, int(lo / hop_s)), min(len(rms), int(hi / hop_s))
    if b - a < 3:
        return []
    r = rms[a:b].astype(np.float64)
    k = max(1, int(0.1 / hop_s))
    if len(r) > k:
        r = np.convolve(r, np.ones(k) / k, "same")
    thr = max(10 ** (PAUSE_DBFS / 20), PAUSE_REL * float(np.median(r)))
    quiet = np.concatenate(([False], r < thr, [False]))
    edges = np.flatnonzero(np.diff(quiet.astype(np.int8)))
    out = []
    for s0, s1 in zip(edges[::2], edges[1::2]):
        if (s1 - s0) * hop_s >= PAUSE_MIN_S:
            out.append((round((a + (s0 + s1) / 2) * hop_s, 3), round((s1 - s0) * hop_s, 3)))
    return out


def pick_switch(pieces: list[LangWindow], lang_a: str, lang_b: str,
                pause_len: dict[float, float] | None = None) -> float | None:
    """Switch cut from sentence-level LID. `pieces` tile a span between pauses (piece
    i+1 starts at the pause that ends piece i). Returns the piece boundary maximising
    (#A pieces before + #B pieces after), counting only pieces of lang_a/lang_b with
    p >= LID_PIECE_MIN_P and length >= LID_PIECE_MIN_S (LID on a 1 s phrase is a coin
    toss). Ties -> the longest pause (pause_len: boundary -> pause seconds), then the
    boundary closest after the last A piece. None when no confident A piece precedes
    a confident B piece."""
    plen = pause_len or {}
    ok = [w for w in pieces
          if w.lang in (lang_a, lang_b) and w.p >= LID_PIECE_MIN_P and w.t1 - w.t0 >= LID_PIECE_MIN_S]
    first_a = next((i for i, w in enumerate(ok) if w.lang == lang_a), None)
    if first_a is None or not any(w.lang == lang_b for w in ok[first_a + 1:]):
        return None
    best, best_key = None, None
    for w in pieces[1:]:
        cut = w.t0
        score = sum(1 for x in ok if (x.t1 <= cut + 1e-6 and x.lang == lang_a) or (x.t0 >= cut - 1e-6 and x.lang == lang_b))
        last_a_end = max((x.t1 for x in ok if x.lang == lang_a and x.t1 <= cut + 1e-6), default=-1e9)
        key = (score, plen.get(cut, 0.0), -(cut - last_a_end))
        if best_key is None or key > best_key:
            best, best_key = cut, key
    return best


def _vote(pool: list[LangWindow], c0: float, c1: float) -> tuple[str, float]:
    """Overlap-weighted language vote of voiced windows over [c0, c1); lang_p = the
    overlap-weighted mean p of the winner. Tie -> higher mean p."""
    tot: dict[str, float] = {}
    wp: dict[str, float] = {}
    for w in pool:
        if not w.lang:
            continue
        ov = min(w.t1, c1) - max(w.t0, c0)
        if ov <= 0:
            continue
        tot[w.lang] = tot.get(w.lang, 0.0) + ov
        wp[w.lang] = wp.get(w.lang, 0.0) + ov * w.p
    if not tot:
        return "", 0.0
    lang = max(tot, key=lambda k: (round(tot[k], 6), wp[k] / tot[k]))
    return lang, wp[lang] / tot[lang]


def split_by_language(bounds: list[float], windows: list[LangWindow], rms: np.ndarray,
                      hop_s: float = RMS_HOP / SR, slack: float = LID_SPLIT_SLACK,
                      switches: list[float] | None = None,
                      fine: list[LangWindow] | None = None) -> list[ChunkPlan]:
    """Turn silence-cut bounds + LID windows into language-labelled chunks (pure).

    Switches: `switches` = exact cut times (placed by plan_chunks at the pause where
    sentence-level LID flips); each is inserted unless an existing bound lies within
    0.5 s. When None (pure/coarse path, the spec rule): the shared boundary of each
    confident find_switches(both=True) pair, cut at the quietest rms point within
    +-slack s, unless an existing bound is already within slack. Either way this
    works whether the switch falls inside a chunk or near a chunk edge (the spec's
    midpoint-in-chunk rule missed every switch that sat between two chunks' windows
    [M: long.wav, 0 of 8 switches split]).

    Labels: overlap-weighted vote over the pool = coarse windows + `fine` windows,
    where coarse windows overlapping a fine window are dropped (fine ones are the
    better local evidence). No voiced overlap -> nearest voiced window
    ("und", 0.0 when there is none at all).
    """
    cuts = sorted(bounds)
    exact = switches is not None
    todo = switches if switches is not None else [w2.t0 for _w1, w2 in find_switches(windows, both=True)]
    for sw in sorted(todo):
        near = 0.5 if exact else slack
        if any(abs(c - sw) <= near for c in cuts) or not cuts[0] < sw < cuts[-1]:
            continue
        k = next(i for i, c in enumerate(cuts) if c > sw)
        if exact:
            cuts.insert(k, round(sw, 3))
            continue
        cut = _quietest(rms, hop_s, max(sw - slack, cuts[k - 1] + 1.0), min(sw + slack, cuts[k] - 1.0))
        if cut is not None:
            cuts.insert(k, cut)
    fine = fine or []
    pool = [w for w in windows if not any(f.t0 < w.t1 and w.t0 < f.t1 for f in fine)] + fine
    voiced = [w for w in pool if w.lang]
    out: list[ChunkPlan] = []
    for c0, c1 in pairwise(cuts):
        if c1 - c0 <= 0:
            continue
        lang, p = _vote(pool, c0, c1)
        if not lang:
            if voiced:
                mid = (c0 + c1) / 2
                near = min(voiced, key=lambda w: abs((w.t0 + w.t1) / 2 - mid))
                lang, p = near.lang, near.p
            else:
                lang, p = "und", 0.0
        out.append(ChunkPlan(c0, c1, lang, p))
    return out


def is_silent(pcm_chunk: np.ndarray) -> bool:
    """True when > SILENCE_FRAC of rms frames are below SILENCE_DBFS (guards Whisper
    hallucinations on silence). Does NOT detect music; see route_engine."""
    import numpy as np

    r = frame_rms(pcm_chunk)
    if len(r) == 0:
        return True
    thr = 10 ** (SILENCE_DBFS / 20)
    return float(np.mean(r < thr)) > SILENCE_FRAC


def route_engine(lang: str, lang_p: float, forced: bool) -> str:
    """ENGINE_PARAKEET if lang in PARAKEET_LANGS, or (not forced and lang_p < LID_MIN_P);
    else ENGINE_WHISPER. With --lang forced: "en" -> parakeet, anything else -> whisper."""
    if lang in PARAKEET_LANGS:
        return ENGINE_PARAKEET
    if not forced and lang_p < LID_MIN_P:
        return ENGINE_PARAKEET
    return ENGINE_WHISPER


def count_words(text: str) -> int:
    """Word count for speech rates, `words=` headers and token estimates: whitespace words
    of the non-CJK text plus CJK_CHAR_WORDS per Han/kana/Hangul char (stdlib)."""
    cjk = len(_CJK_RE.findall(text))
    if not cjk:
        return len(text.split())
    rest = sum(1 for w in _CJK_RE.sub(" ", text).split() if re.search(r"\w", w))  # not "。"
    return rest + max(1, round(cjk * CJK_CHAR_WORDS))


def is_hallucination_text(text: str, dur: float | None) -> bool:
    """Text-level Whisper hallucination guard (beyond the spec's logprob/compression
    rule, which keeps all of these [M: turbo on a real-instrument loop mix and on
    synthetic tones, forced fr/en/ja: "Sous-titrage ST' 501" p=-0.05, "Thank you."
    over 30 s, "BGM", "ご視聴ありがとうございました", "...", all no_speech_prob 0.0]):
    - no letters/digits (".", "...", "♪");
    - a known non-speech tag ("Musique", "[Music]", "BGM", ...);
    - <= 8 words matching a subtitle-credit pattern (Amara.org, "Sous-titrage", 字幕, ...);
    - >= 10 s long at < 0.3 words/s (real speech runs ~2-3 words/s; count_words, so CJK
      text without spaces is not one word)."""
    t = text.strip()
    if not re.search(r"\w", t):
        return True
    norm = re.sub(r"[\s\W_]+", " ", t.lower()).strip()
    if norm in NON_SPEECH_TAGS:
        return True
    words = len(t.split())
    if words <= 8 and CREDIT_RE.search(t):
        return True
    return dur is not None and dur >= 10.0 and count_words(t) / dur < 0.3


def filter_whisper_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop hallucination-prone Whisper segments (keys avg_logprob, compression_ratio,
    no_speech_prob may be missing -> treated as passing). See WHISPER_* constants,
    plus is_hallucination_text. Returns the kept segments unchanged (same dicts)."""
    kept = []
    for s in segments:
        dur = float(s["t1"]) - float(s["t0"]) if "t0" in s and "t1" in s else None
        if is_hallucination_text(str(s.get("text", "")), dur):
            continue
        lp = s.get("avg_logprob")
        cr = s.get("compression_ratio")
        ns = s.get("no_speech_prob")
        if lp is not None and lp < WHISPER_MIN_AVG_LOGPROB:
            continue
        if cr is not None and cr > WHISPER_MAX_COMPRESSION:
            continue
        if ns is not None and lp is not None and ns > WHISPER_NO_SPEECH and lp < WHISPER_MIN_AVG_LOGPROB:
            continue  # subsumed by the first rule; kept for the spec's step-5 wording
        kept.append(s)
    return kept


# --------------------------------------------------------------------------
# Engine interface + worker loop
# --------------------------------------------------------------------------
class Engine(Protocol):
    """Backend-specific model code (asr_mlx.MlxEngine, asr_cpu.CpuEngine).

    All audio args are float32 mono 16 kHz numpy arrays; returned segment
    times are RELATIVE to the start of the array passed in.
    """

    backend: str  # "mlx" | "cpu"
    ids: dict[str, str]  # role -> model id (asr_common.model_ids(backend))

    def versions(self) -> dict[str, str]:
        """Package versions for manifest `tools` (e.g. {"parakeet_mlx":"0.5.2","mlx_whisper":"0.4.3"})."""
        ...

    def ensure(self, role: str, emit_fn: Callable[[dict[str, Any]], None]) -> None:
        """Download (if needed) and load the model for `role` in ROLES. Idempotent.
        When the repo is not in the HF cache, emits EV_MODEL downloading/progress/done
        events (load_model_with_events). Raises AsrError("model_download_failed"|"disk_full")."""
        ...

    def detect_language(self, x: np.ndarray) -> tuple[str, float]:
        """LID on <= 30 s (padded/trimmed to 30 s) -> (iso639-1 code, top probability)."""
        ...

    def parakeet(self, x: np.ndarray) -> list[dict[str, Any]]:
        """English transcription -> [{"t0","t1","text"}] (sentences, stripped, non-empty)."""
        ...

    def whisper(self, x: np.ndarray, lang: str) -> list[dict[str, Any]]:
        """Forced-language transcription, condition_on_previous_text=False ->
        [{"t0","t1","text","avg_logprob","compression_ratio","no_speech_prob"}] (unfiltered)."""
        ...


def pad_tail(x: np.ndarray, pad_s: float = PARAKEET_TAIL_PAD) -> np.ndarray:
    """Append `pad_s` of silence (segments are clamped back into the chunk by _abs_segments)."""
    import numpy as np

    return np.concatenate([x, np.zeros(int(pad_s * SR), dtype=x.dtype)]) if pad_s > 0 else x


def find_holes(x: np.ndarray, segs: list[dict[str, Any]], min_s: float = HOLE_MIN_S) -> list[tuple[float, float]]:
    """Voiced spans (s, relative to x) of >= min_s that no segment covers. Parakeet TDT greedy
    decoding sometimes skips a whole sentence (1 h talk: 4 holes of 5-18 s that Whisper
    transcribes); the gaps between its segments show where."""
    import numpy as np

    dur = len(x) / SR
    rms = frame_rms(x)
    thr = 10 ** (PAUSE_DBFS / 20)
    hop = RMS_HOP / SR
    edges = [0.0]
    for sg in sorted(segs, key=lambda d: float(d["t0"])):
        edges += [float(sg["t0"]), float(sg["t1"])]
    edges.append(dur)
    holes = []
    for a, b in zip(edges[::2], edges[1::2]):
        a, b = max(0.0, a), min(dur, b)
        if b - a < min_s:
            continue
        r = rms[int(a / hop):int(b / hop)]
        if len(r) and float(np.mean(r > thr)) >= HOLE_VOICED_FRAC:
            holes.append((a, b))
    return holes


def parakeet_filled(engine: Engine, x: np.ndarray) -> list[dict[str, Any]]:
    """engine.parakeet on the tail-padded chunk, then one more pass over each hole
    (with HOLE_CONTEXT_S of context) whose segments land inside the hole. Relative times."""
    segs = engine.parakeet(pad_tail(x))
    first = list(segs)
    for a, b in find_holes(x, segs) if segs else []:  # no text at all: music/noise, not a hole
        c0 = max(0.0, a - HOLE_CONTEXT_S)
        c1 = min(len(x) / SR, b + HOLE_CONTEXT_S)
        for e in engine.parakeet(pad_tail(x[int(c0 * SR):int(c1 * SR)])):
            t0, t1 = c0 + float(e["t0"]), c0 + float(e["t1"])
            overlap = sum(max(0.0, min(t1, float(f["t1"])) - max(t0, float(f["t0"]))) for f in first)
            if a - 0.25 <= (t0 + t1) / 2 <= b + 0.25 and overlap <= 0.5 * max(t1 - t0, 1e-3):
                segs.append({**e, "t0": t0, "t1": t1})
    return sorted(segs, key=lambda d: float(d["t0"]))


def _abs_segments(segs: list[dict[str, Any]], a0: float, a1: float) -> list[dict[str, Any]]:
    """Relative segments -> absolute {t0,t1,text}, clamped into [a0, a1], empty text dropped."""
    out = []
    for s in segs:
        text = str(s.get("text", "")).strip()
        if not text:
            continue
        t0 = min(max(a0 + float(s["t0"]), a0), a1)
        t1 = min(max(a0 + float(s["t1"]), t0), a1)
        out.append({"t0": round(t0, 2), "t1": round(t1, 2), "text": text})
    return out


def detect_windows(pcm: np.ndarray, engine: Engine, emit_fn: Callable[[dict[str, Any]], None],
                   grid: list[tuple[float, float]] | None = None) -> list[LangWindow]:
    """LID over `grid` (default: the global 30 s grid); silent windows are skipped (lang "")."""
    windows = []
    loaded = False
    for a, b in lid_grid(len(pcm) / SR) if grid is None else grid:
        x = pcm[int(a * SR):int(b * SR)]
        if is_silent(x):
            windows.append(LangWindow(a, b, "", 0.0))
            continue
        if not loaded:
            engine.ensure("lid", emit_fn)
            loaded = True
        lang, p = engine.detect_language(x[: int(LID_WINDOW * SR)])
        windows.append(LangWindow(a, b, lang, float(p)))
    return windows


def locate_switch(pcm: np.ndarray, rms: np.ndarray, lo: float, hi: float, lang_a: str, lang_b: str,
                  engine: Engine, emit_fn: Callable[[dict[str, Any]], None]) -> float | None:
    """Exact cut for a switch known to lie in (lo, hi): pauses in [lo-3, hi+3] split the
    span into sentence-ish pieces, each piece gets LID, cut = pick_switch. None when
    there are no pauses or LID does not confirm the order."""
    dur = len(pcm) / SR
    a, b = max(0.0, lo - PAUSE_MARGIN), min(dur, hi + PAUSE_MARGIN)
    pauses = pause_points(rms, RMS_HOP / SR, a, b)
    if not pauses:
        return None
    edges = [a, *(t for t, _ in pauses), b]
    grid = [(p0, p1) for p0, p1 in pairwise(edges) if p1 - p0 > 0.3]
    pieces = detect_windows(pcm, engine, emit_fn, grid=grid)
    return pick_switch(pieces, lang_a, lang_b, pause_len=dict(pauses))


def plan_chunks(pcm: np.ndarray, rms: np.ndarray, bounds: list[float], engine: Engine,
                emit_fn: Callable[[dict[str, Any]], None]) -> list[ChunkPlan]:
    """Language plan for one job:
    1. coarse LID on the 30 s grid (spec);
    2. fine LID (10 s windows, 5 s hop) over refine_regions (disagreeing pairs + low-p windows);
    3. per fine_switches interval: sentence-level LID between the pauses -> exact cut at
       the pause where the language flips (locate_switch); fallback: longest pause
       within +-LID_SPLIT_SLACK of the interval middle;
    4. split_by_language with those cuts; fine windows refine the chunk labels.
    English-only talks (every window p >= 0.95) cost exactly the coarse pass."""
    windows = detect_windows(pcm, engine, emit_fn)
    switches: list[float] = []
    fine: list[LangWindow] = []
    for r0, r1 in refine_regions(windows):
        fw = detect_windows(pcm, engine, emit_fn, grid=fine_grid(r0, r1))
        fine += fw
        for lo, hi, la, lb in fine_switches(fw):
            cut = locate_switch(pcm, rms, lo, hi, la, lb, engine, emit_fn)
            if cut is None:
                mid = (lo + hi) / 2
                cut = _quietest(rms, RMS_HOP / SR, mid - LID_SPLIT_SLACK, mid + LID_SPLIT_SLACK,
                                smooth_s=LID_PAUSE_SMOOTH)
            if cut is not None:
                switches.append(cut)
    return split_by_language(bounds, windows, rms, switches=switches, fine=fine)


def process_job(job: dict[str, Any], engine: Engine, emit_fn: Callable[[dict[str, Any]], None],
                should_abort: Callable[[], bool] = lambda: False) -> None:
    """Run one OP_JOB end to end (spec 4.5 steps 1-6), emitting EV_CHUNK... then EV_DONE.

    Steps: decode_pcm -> silence_cuts -> (lang forced: every chunk lang=job.lang, p=1.0 |
    else plan_chunks: coarse LID + fine LID at switches + split_by_language) ->
    per chunk: is_silent -> segments [] | route_engine -> engine.parakeet / engine.whisper
    (+filter_whisper_segments) -> offset times by chunk t0 + range start -> emit chunk.
    Loads models lazily via engine.ensure (whisper only on the first non-English chunk).
    `should_abort()` is polled between chunks (stdin closed -> stop early).

    Raises:
        AsrError: caller (run_worker) converts it into an EV_ERROR with job_id.
    """
    import numpy as np

    t_start = time.monotonic()
    job_id = job.get("job_id")
    base = float(job.get("from") or 0.0)
    forced = (job.get("lang") or "").strip().lower() or None
    pcm = decode_pcm(str(job["audio"]), job.get("from"), job.get("to"))
    dur = len(pcm) / SR
    t_decoded = time.monotonic()
    rms = frame_rms(pcm)
    bounds = silence_cuts(pcm, rms=rms)
    if forced:
        plans = [ChunkPlan(a, b, forced, 1.0) for a, b in pairwise(bounds)]
    else:
        plans = plan_chunks(pcm, rms, bounds, engine, emit_fn)
    t_lid = time.monotonic()
    thr = 10 ** (SILENCE_DBFS / 20)
    hop = RMS_HOP
    for c in plans:
        if should_abort():
            raise AsrError("asr_failed", "aborted: client closed stdin")
        a0, a1 = base + c.t0, base + c.t1
        lang = c.lang or "und"
        eng = route_engine(lang, c.lang_p, forced is not None)
        ev: dict[str, Any] = {"ev": EV_CHUNK, "job_id": job_id, "t0": round(a0, 2), "t1": round(a1, 2),
                              "lang": lang, "lang_p": round(c.lang_p, 3), "engine": eng,
                              "model": engine.ids[eng], "segments": []}
        r = rms[int(c.t0 * SR / hop):max(int(c.t0 * SR / hop) + 1, int(c.t1 * SR / hop))]
        if len(r) == 0 or float(np.mean(r < thr)) > SILENCE_FRAC:
            ev["skipped"] = "silence"
            emit_fn(ev)
            continue
        x = pcm[int(c.t0 * SR):int(c.t1 * SR)]
        tc = time.monotonic()
        if eng == ENGINE_WHISPER:
            engine.ensure("whisper", emit_fn)
            allw = engine.whisper(x, lang)
            raw = filter_whisper_segments(allw)
            if len(raw) < len(allw):
                print(f"[asr] {job_id} {a0:.1f}-{a1:.1f} {lang}: dropped {len(allw) - len(raw)}/{len(allw)} "
                      f"whisper segs: {[s.get('text', '')[:40] for s in allw if s not in raw]}", file=sys.stderr)
        else:
            engine.ensure("parakeet", emit_fn)
            raw = parakeet_filled(engine, x)
        ev["segments"] = _abs_segments(raw, a0, a1)
        ev["wall_s"] = round(time.monotonic() - tc, 2)
        emit_fn(ev)
    emit_fn({"ev": EV_DONE, "job_id": job_id, "audio_s": round(dur, 2),
             "wall_s": round(time.monotonic() - t_start, 2), "decode_s": round(t_decoded - t_start, 2),
             "lid_s": round(t_lid - t_decoded, 2), "chunks": len(plans)})


def _open_lock(path: str) -> Any:
    """The shared inter-process lock (wfm.cache.FileLock on <CACHE>/asr.lock)."""
    from wfm.cache import (
        FileLock,
    )

    return FileLock(path)


def _stdin_reader(inbox: queue.Queue[str | None], eof: threading.Event) -> None:
    try:
        for line in sys.stdin:
            inbox.put(line)
    except (OSError, ValueError):
        pass
    finally:
        eof.set()
        inbox.put(None)


def _job_error(job_id: Any, exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, AsrError):
        code, msg = exc.code, exc.message
    elif _is_disk_full(exc):
        code, msg = "disk_full", str(exc)
    elif isinstance(exc, MemoryError):
        code, msg = "asr_failed", "out of memory"
    else:
        code, msg = "asr_failed", f"{type(exc).__name__}: {exc}"
    return {"ev": EV_ERROR, "job_id": job_id, "code": code, "message": " ".join(msg.split())[:400]}


def run_worker(engine: Engine, argv: list[str] | None = None) -> int:
    """Worker main loop shared by asr_mlx.py and asr_cpu.py. Returns the exit code.

    argv: ["--lock", "<CACHE>/asr.lock"] (omitted -> no inter-process lock, tests only).
    1. protect_stdout(); a thread reads stdin into a queue (EOF -> shutdown, and
       aborts a running job between chunks).
    2. Memory rule (spec 4.1): NON-blocking acquire of the lock; if free, preload
       "lid" + "parakeet" WHILE HOLDING it, then release; if another session holds it,
       defer model loading to the first job (loaded inside the job's lock).
    3. emit EV_READY {backend, load_s, versions, preloaded}.
    4. OP_JOB -> hold the lock (blocking; EV_WAITING first if busy) around
       process_job; AsrError -> EV_ERROR{job_id, code, message}; any other
       exception -> EV_ERROR{job_id, "asr_failed", repr}. OP_PREFETCH -> hold the
       lock, engine.ensure each role, emit EV_PREFETCHED. OP_SHUTDOWN or EOF -> 0.
    A preload failure (model_download_failed / disk_full) is fatal: EV_ERROR with
    job_id null, exit 1. Unknown ops are ignored (logged to stderr).
    """
    protect_stdout()
    ap = argparse.ArgumentParser(prog=f"asr_{engine.backend}")
    ap.add_argument("--lock", default=None)
    args = ap.parse_args(argv)
    t0 = time.monotonic()
    lock = _open_lock(args.lock) if args.lock else None
    inbox: queue.Queue[str | None] = queue.Queue()
    eof = threading.Event()
    threading.Thread(target=_stdin_reader, args=(inbox, eof), name="wfm-stdin", daemon=True).start()
    try:
        return _worker_loop(engine, lock, inbox, eof, t0)
    except BrokenPipeError:  # client went away; nothing left to tell it
        return 0
    finally:
        if lock is not None:
            try:
                lock.release()
            except Exception as exc:  # noqa: BLE001
                print(f"[asr] lock release failed: {exc!r}", file=sys.stderr)


def _preload(engine: Engine, lock: Any) -> bool:
    got = True if lock is None else lock.acquire(blocking=False)
    if not got:
        print("[asr] asr.lock busy at startup: loading models lazily", file=sys.stderr)
        return False
    try:
        for role in ("lid", "parakeet"):
            engine.ensure(role, emit)
    finally:
        if lock is not None:
            lock.release()
    return True


def _worker_loop(engine: Engine, lock: Any, inbox: queue.Queue[str | None], eof: threading.Event,
                 t0: float) -> int:
    try:
        preloaded = _preload(engine, lock)
    except Exception as exc:  # noqa: BLE001
        emit(_job_error(None, exc))
        return 1
    emit({"ev": EV_READY, "backend": engine.backend, "load_s": round(time.monotonic() - t0, 2),
          "versions": engine.versions(), "preloaded": preloaded, "models": engine.ids})
    while True:
        line = inbox.get()
        if line is None:
            return 0
        msg = decode(line)
        if msg is None:
            continue
        op = msg.get("op")
        if op == OP_SHUTDOWN:
            return 0
        if op == OP_JOB:
            _run_locked(lock, msg.get("job_id"),
                        lambda m=msg: process_job(m, engine, emit, should_abort=eof.is_set), eof)
        elif op == OP_PREFETCH:
            _run_locked(lock, msg.get("job_id") or PREFETCH_ID, lambda m=msg: _prefetch(engine, m), eof)
        else:
            print(f"[asr] ignoring unknown op {op!r}", file=sys.stderr)


def _prefetch(engine: Engine, msg: dict[str, Any]) -> None:
    roles = [r for r in (msg.get("roles") or ROLES) if r in ROLES]
    for r in roles:
        engine.ensure(r, emit)
    emit({"ev": EV_PREFETCHED, "models": {engine.ids[r]: model_size_mb(engine.ids[r]) for r in roles}})


def _run_locked(lock: Any, job_id: Any, fn: Callable[[], None], eof: threading.Event | None = None) -> None:
    """Run fn holding the inter-process lock. While another session holds it: emit
    EV_WAITING once, then poll; stdin EOF while waiting -> give up (asr_failed)."""
    held = False
    try:
        if lock is not None:
            if not lock.acquire(blocking=False):
                emit({"ev": EV_WAITING, "job_id": job_id, "reason": "asr.lock"})
                while not lock.acquire(blocking=True, timeout=1.0):
                    if eof is not None and eof.is_set():
                        raise AsrError("asr_failed", "aborted while waiting for asr.lock: client closed stdin")
            held = True
        fn()
    except BrokenPipeError:
        raise
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, AsrError):
            print(f"[asr] {job_id}: {exc.code}: {exc.message}", file=sys.stderr)
        else:
            traceback_to_stderr(exc)
        emit(_job_error(job_id, exc))
    finally:
        if held:
            lock.release()


def traceback_to_stderr(exc: BaseException) -> None:
    import traceback

    traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)
