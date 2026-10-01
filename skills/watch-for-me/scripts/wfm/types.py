"""Shared types, constants and tiny pure helpers for every wfm module.

This module is IMPLEMENTED (not a stub): it is the contract builders share.
Stdlib only. JSON shapes referenced here are spec section 3 (WFM_RESULT,
manifest.json, frames.json, transcripts/<rtag>.json) and 4.6 (plan.json).

Conventions pinned here (spec ambiguities resolved in the skeleton):
- All times are float seconds, ABSOLUTE in the source video (a --from/--to
  range never shifts timestamps).
- Every `to_dict()` returns exactly the JSON object written to disk / stdout.
- Paths inside JSON are absolute strings unless a field says "relative to
  the key dir" (manifest `media`, frames.json `file`).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

# --------------------------------------------------------------------------
# Exit codes (spec 3 "Exit codes")
# --------------------------------------------------------------------------
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_PREREQ = 3
EXIT_PARTIAL = 4
EXIT_ALL_FAILED = 5
EXIT_WAIT_TIMEOUT = 6
EXIT_WAIT_DEAD = 7
EXIT_INTERRUPTED = 130

MAX_INPUTS = 10
DEFAULT_MAX_MINUTES = 240
DEFAULT_JOBS_NET = 4
DEFAULT_WAIT_TIMEOUT_S = 540
MAX_IMAGE_SIDE = 2000  # spec 4.4: never emit a sheet or zoom wider/taller than this

# --------------------------------------------------------------------------
# Per-video error codes (spec 3 "Per-video error codes")
# --------------------------------------------------------------------------
ErrorCode = Literal[
    "unsupported_url",
    "login_required",
    "private",
    "geo_blocked",
    "live_stream",
    "playlist_refused",
    "too_long",
    "download_failed",
    "no_audio",
    "no_video",
    "asr_failed",
    "frames_failed",
    "model_download_failed",
    "disk_full",
]
ERROR_CODES: tuple[str, ...] = (
    "unsupported_url",
    "login_required",
    "private",
    "geo_blocked",
    "live_stream",
    "playlist_refused",
    "too_long",
    "download_failed",
    "no_audio",
    "no_video",
    "asr_failed",
    "frames_failed",
    "model_download_failed",
    "disk_full",
)
# Non-fatal codes: the video still finishes with status "done"; the code goes
# into VideoResult.warnings, NOT VideoResult.error (skeleton decision).
NON_FATAL_CODES: frozenset[str] = frozenset({"no_audio", "no_video"})


class WfmError(Exception):
    """A classified, user-facing failure.

    Raised by ingest/frames/asr_client/cache; caught per video by cli and
    serialized into WFM_RESULT `error` (or `warnings` for NON_FATAL_CODES)
    and into manifest `errors`.

    Args:
        code: one of ERROR_CODES.
        message: one line, no newlines (truncate stderr tails to ~200 chars).
        hint: optional one-line fix, e.g. "retry with --cookies chrome".
    """

    def __init__(self, code: str, message: str, hint: str | None = None) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown error code {code!r}")
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = " ".join(message.split())
        self.hint = hint

    @property
    def fatal(self) -> bool:
        return self.code not in NON_FATAL_CODES

    def to_dict(self) -> dict[str, Any]:
        """-> {"code","message","hint"} (spec 3: `error` when set)."""
        return {"code": self.code, "message": self.message, "hint": self.hint}


# --------------------------------------------------------------------------
# Stage / event vocabularies
# --------------------------------------------------------------------------
StageStatus = Literal["pending", "running", "done", "error", "skipped"]
STAGE_STATUSES: tuple[str, ...] = ("pending", "running", "done", "error", "skipped")
STAGE_FINAL: frozenset[str] = frozenset({"done", "error", "skipped"})

# WFM progress line vocabulary (spec 3 "Progress format")
EventStage = Literal["run", "meta", "audio", "video", "frames", "asr", "context", "model"]
EVENT_STAGES: tuple[str, ...] = ("run", "meta", "audio", "video", "frames", "asr", "context", "model")
EventStatus = Literal["start", "done", "progress", "cached", "skipped", "error"]
EVENT_STATUSES: tuple[str, ...] = ("start", "done", "progress", "cached", "skipped", "error")

# Per-video stage names in WFM_RESULT `stages` (spec 3 WFM_RESULT schema)
VIDEO_STAGES: tuple[str, ...] = ("meta", "audio", "video", "frames", "asr")

Backend = Literal["mlx", "cpu"]
PlanMode = Literal["visual", "windowed"]
WaitUntil = Literal["frames", "done"]
Resolution = Literal[720, 1080]

# Manifest stage keys (spec 3 manifest.json `stages`). Skeleton decision for the
# download stages, which the spec example leaves out:
#   "meta"
#   "media/audio" | "media/video_720" | "media/video_1080" | "media/muxed"
#   "transcripts/<rtag>"            params_hash = sha1(asr_client.transcript_params(...))
#   "views/<vtag>/frames"           params_hash = sha1(FramesParams.to_dict() + sheet grid/width)


def manifest_stage_media(name: str) -> str:
    """"audio" | "video_720" | "video_1080" | "muxed" -> "media/<name>"."""
    return f"media/{name}"


def manifest_stage_transcript(rtag: str) -> str:
    return f"transcripts/{rtag}"


def manifest_stage_frames(vtag: str) -> str:
    return f"views/{vtag}/frames"


# --------------------------------------------------------------------------
# Timestamp helpers (used by sheets labels, transcript.md, context.md, plan)
# --------------------------------------------------------------------------
def fmt_ts(seconds: float) -> str:
    """Format seconds (floored) as "mm:ss", or "h:mm:ss" when >= 3600.

    Per value, not per video (matches the research prototype): 59:59 -> "59:59",
    3600 -> "1:00:00". Negative input is clamped to 0.
    """
    t = max(0, int(seconds))
    if t >= 3600:
        return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}"
    return f"{t // 60:02d}:{t % 60:02d}"


_TS_RE = re.compile(r"^\d+(\.\d+)?$|^\d+:\d{1,2}(\.\d+)?$|^\d+:\d{1,2}:\d{1,2}(\.\d+)?$")


def parse_ts(text: str) -> float:
    """Parse --from/--to/--t values: "SS", "MM:SS", "HH:MM:SS" (fractions ok).

    Raises:
        ValueError: on anything else (cli maps it to exit 2).
    """
    s = text.strip()
    if not _TS_RE.match(s):
        raise ValueError(f"bad time {text!r}: use SS, MM:SS or HH:MM:SS")
    total = 0.0
    for part in s.split(":"):
        total = total * 60 + float(part)
    return total


def ms9(seconds: float) -> str:
    """Seconds -> zero-padded 9-digit milliseconds, e.g. 12.1 -> "000012100".

    Used for frames/f_<ms9>.jpg and zoom/f_<ms9>_w<width>... file names.
    """
    return f"{round(seconds * 1000):09d}"


# --------------------------------------------------------------------------
# Run options (parsed `run` flags, spec 3 "run flags")
# --------------------------------------------------------------------------
@dataclass
class RunOptions:
    inputs: list[str]
    run_id: str
    detach: bool = False
    hires: bool = False  # --code implies hires=True
    code: bool = False
    lang: str | None = None  # ISO 639-1, forced speech language
    from_s: float | None = None
    to_s: float | None = None
    audio_only: bool = False
    video_only: bool = False
    cookies: str | None = None  # browser name for --cookies-from-browser
    playlist: int | None = None  # N in 1..10, None = refuse playlists
    max_minutes: int = DEFAULT_MAX_MINUTES
    fresh: bool = False
    jobs_net: int = DEFAULT_JOBS_NET
    jobs_cpu: int = 2  # cli sets max(2, cores//2); cpu backend: 1 while an ASR job runs
    quiet: bool = False
    backend: Backend = "mlx"  # resolved by asr_client.select_backend()

    @property
    def resolution(self) -> int:
        """720, or 1080 with --hires/--code."""
        return 1080 if (self.hires or self.code) else 720

    def flags_dict(self) -> dict[str, Any]:
        """Serializable flags for runs/<id>/run.json `flags` (inputs/run_id excluded)."""
        d = asdict(self)
        d.pop("inputs")
        d.pop("run_id")
        return d


# --------------------------------------------------------------------------
# Resolved input / metadata
# --------------------------------------------------------------------------
# Trimmed meta.json keys (spec 3 cache layout). Local files also carry
# local_path, has_audio, has_video.
META_KEYS: tuple[str, ...] = (
    "id", "title", "uploader", "channel", "upload_date", "duration", "chapters",
    "description", "webpage_url", "extractor_key", "live_status", "language",
    "view_count", "tags", "width", "height",
)


@dataclass
class Resolved:
    """Output of ingest.resolve() for one input (after playlist expansion).

    Attributes:
        input: the original input string (entry URL for playlist entries).
        key: cache key (cache.key_for_info / cache.key_for_local).
        meta: trimmed meta dict (META_KEYS; local adds local_path/has_audio/has_video).
        info_json: abs path of the uncompressed yt-dlp -J json used for
            `--load-info-json` (a temp file in the key dir, `info.json`,
            deleted after downloads; the kept copy is info.json.gz). None for local.
        is_local: True for local files.
        split_formats: True when the site offers separate audio-only and
            video-only formats; False -> ONE muxed download feeds both branches.
        index_hit: True when the key came from index.json (no meta fetch).
        parent_input: the playlist URL this entry came from, else None.
    """

    input: str
    key: str
    meta: dict[str, Any]
    info_json: str | None
    is_local: bool
    split_formats: bool
    index_hit: bool = False
    parent_input: str | None = None


@dataclass
class FailedEntry:
    """A playlist entry that could not be resolved (ingest.resolve never raises for entries)."""

    input: str
    error: WfmError
    parent_input: str | None = None


@dataclass
class MediaFile:
    """A downloaded (or local) media file.

    path: absolute path. kind: "audio" | "video_720" | "video_1080" | "muxed" | "local".
    format_id: yt-dlp format id (None for local). mb: size in MB (1e6 bytes).
    """

    path: str
    kind: str
    format_id: str | None
    mb: float


# --------------------------------------------------------------------------
# Frames / sheets (spec 4.3, 4.4, frames.json)
# --------------------------------------------------------------------------
@dataclass
class FramesParams:
    """frames.json `params` (spec 3). to_dict() is also the params_hash input."""

    mode: Literal["scene", "key"] = "scene"
    scene: float = 0.15
    floor_s: float = 7
    width: int = 1280  # 1920 with --hires
    dup_bits: int = 20
    repeat_bits: int = 10
    heartbeat_s: float = 60
    cap: int = 240  # resolved cap = clamp(20 * minutes, 24, 240)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RawFrame:
    """A candidate frame from ffmpeg: absolute timestamp + abs path of raw_%05d.jpg."""

    t: float
    path: str


@dataclass
class HashedFrame:
    """RawFrame + its 256-bit dHash (int). Input of frames.segment() (pure, testable)."""

    t: float
    hash: int
    path: str


@dataclass
class Segment:
    """One frames.json `segments[]` entry.

    n: 1-based tile number in time order (repeats included in numbering).
    t0/t1: segment span [t0, t1). t: timestamp of the representative (LAST) frame.
    file: path relative to the view dir, "frames/f_<ms9>.jpg".
    novelty: Hamming distance of the segment's first frame to the previous
        segment's first frame (256 for the first segment).
    repeat_of: n of an earlier kept segment within repeat_bits, else None
        (repeats are listed but never placed on sheets).
    sheet: 1-based sheet number it was placed on (set by sheets.make_sheets), else None.
    hash: dHash of the representative frame; NOT serialized to frames.json.
    """

    n: int
    t0: float
    t1: float
    t: float
    file: str
    novelty: int
    repeat_of: int | None = None
    sheet: int | None = None
    hash: int = field(default=0, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n, "t0": round(self.t0, 3), "t1": round(self.t1, 3), "t": round(self.t, 3),
            "file": self.file, "novelty": self.novelty, "repeat_of": self.repeat_of, "sheet": self.sheet,
        }


@dataclass
class Grid:
    cols: int
    rows: int
    width: int  # sheet width in px, <= MAX_IMAGE_SIDE

    @property
    def label(self) -> str:
        return f"{self.cols}x{self.rows}"


@dataclass
class Sheet:
    """One frames.json `sheets[]` entry. file is relative to the view dir."""

    n: int
    file: str  # "sheets/sheet_NNN.jpg"
    grid: str  # "3x3"
    w: int
    h: int
    est_tokens: int  # ceil(w/28) * ceil(h/28)
    tiles: list[int]  # segment numbers n on this sheet, in order

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FramesOutput:
    """Return of frames.extract_segments()."""

    params: FramesParams
    raw_count: int
    segments: list[Segment]
    src_w: int  # width/height of the kept jpgs (after scale), for sheets.choose_grid
    src_h: int
    extract_s: float
    dedupe_s: float


# --------------------------------------------------------------------------
# ASR / transcripts (spec 4.5, transcripts/<rtag>.json)
# --------------------------------------------------------------------------
@dataclass
class AsrJob:
    """One transcription job sent to the worker (protocol `op: "job"`)."""

    job_id: str
    key: str
    audio: str  # abs media path (audio.* / muxed.* / local file)
    from_s: float | None = None
    to_s: float | None = None
    lang: str | None = None


@dataclass
class TranscriptSegment:
    i: int
    t0: float
    t1: float
    text: str
    lang: str
    engine: str  # "parakeet" | "whisper"


@dataclass
class TranscriptChunk:
    t0: float
    t1: float
    lang: str
    lang_p: float
    engine: str
    model: str


# --------------------------------------------------------------------------
# Per-video + run results (WFM_RESULT, spec 3)
# --------------------------------------------------------------------------
@dataclass
class VideoResult:
    """One WFM_RESULT `videos[]` entry; also stored live in runs/<id>/run.json.

    Fields beyond the spec example (skeleton additions, all optional for
    readers): `warnings` (non-fatal WfmError dicts, e.g. no_audio),
    `parent_input` (playlist URL), `transcript_json`, `frames_json`, `sheets`.
    `key` is None when the input failed before a key existed (e.g. unsupported_url).
    `status`: "running" while in flight, then "done" | "error".
    """

    input: str
    key: str | None = None
    status: Literal["running", "done", "error"] = "running"
    error: dict[str, Any] | None = None
    warnings: list[dict[str, Any]] = field(default_factory=list)
    title: str | None = None
    uploader: str | None = None
    duration: float | None = None
    webpage_url: str | None = None
    is_local: bool = False
    parent_input: str | None = None
    view_dir: str | None = None
    context_md: str | None = None
    transcript_md: str | None = None
    transcript_json: str | None = None
    frames_json: str | None = None
    sheets: list[str] = field(default_factory=list)  # abs sheet paths, in order
    visual_md: str | None = None
    visual_cached: bool = False
    stages: dict[str, str] = field(default_factory=lambda: {s: "pending" for s in VIDEO_STAGES})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> VideoResult:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class RunResult:
    """The WFM_RESULT object (spec 3). `ok` == (exit == 0)."""

    run_id: str
    exit: int
    elapsed_s: float
    backend: str
    plan: str | None  # abs path of runs/<id>/plan.json
    plan_mode: str | None
    videos: list[VideoResult]

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": 1, "run_id": self.run_id, "ok": self.exit == EXIT_OK, "exit": self.exit,
            "elapsed_s": round(self.elapsed_s, 2), "backend": self.backend, "plan": self.plan,
            "plan_mode": self.plan_mode, "videos": [v.to_dict() for v in self.videos],
        }


def exit_code_for(videos: list[VideoResult]) -> int:
    """0 all done, 4 some done + some error, 5 all error (or no videos)."""
    done = sum(1 for v in videos if v.status == "done")
    if videos and done == len(videos):
        return EXIT_OK
    return EXIT_PARTIAL if done else EXIT_ALL_FAILED
