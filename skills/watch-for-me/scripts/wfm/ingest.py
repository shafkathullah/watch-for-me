"""yt-dlp metadata + parallel audio/video downloads, error mapping, retries,
playlists, local probe (spec 4.2, spec 3 error mapping).

yt-dlp is ALWAYS a subprocess (proc.ytdlp_base() = sys.executable -m yt_dlp),
never the Python API. Every call gets proc.js_runtime_args() and, with
--cookies B, `--cookies-from-browser B` (meta AND both download calls).

Called by cli.process_input (async). All subprocesses via wfm.proc.arun so
cancel/SIGTERM kills them. Network concurrency is enforced by the caller's
NET semaphore passed in as `net`.

Builder notes (measured 2026-09-29, yt-dlp 2026.08.19):
- X/Twitter offers split formats only over HLS (hls-audio-*, hls-<n> video-only);
  its https "http-<n>" formats are muxed with codecs reported as None. So
  split detection only counts direct http(s) formats when any exist
  (has_split_formats), and X gets ONE muxed download over https.
- A multi-video tweet status URL comes back as `_type: playlist` even with
  --no-playlist, with FULLY extracted entries whose `url` is the status URL
  itself (no per-entry URL). Such entries are used as their own info.json.
  `/video/N` URLs resolve to that single video with --no-playlist.
- With --playlist N the meta call uses --yes-playlist (so `/video/1` or
  `watch?v=..&list=..` expand), and every meta call adds `-I 1:<N or 1>` so a
  huge playlist is not fully enumerated just to be refused (246-entry YouTube
  playlist: 2.46 s -> 1.36 s; playlist_count is still reported).
"""

from __future__ import annotations

import asyncio
import dataclasses
import errno
import gzip
import json
import os
import re
import sys
import sysconfig
import time
from pathlib import Path
from typing import Any

from . import cache, proc
from .cache import KeyPaths
from .types import (
    ERROR_CODES,
    META_KEYS,
    FailedEntry,
    MediaFile,
    Resolved,
    RunOptions,
    WfmError,
)

# Exact yt-dlp selectors (spec 4.2).
META_ARGS = ["-J", "--no-playlist", "--flat-playlist", "--no-warnings"]
DL_COMMON = ["-N", "8", "--no-part", "-q", "--no-progress", "--no-warnings",
             "--print", "after_move:%(filepath)s|%(format_id)s"]
AUDIO_SORT = "lang,proto:https"
AUDIO_FORMAT = "ba[abr<=96]/ba/b"
VIDEO_SORT = "lang,res:{res},vcodec:h264,proto:https"  # .format(res=720|1080)
VIDEO_FORMAT = "bv*/b"
MUXED_FORMAT = "b/bv*+ba"
MAX_403_RETRIES = 2
RETRY_BACKOFF_S = (2.0, 5.0)  # sleep before 403 retry 1, 2
COMPLETE_RATIO = 0.98  # ffprobe duration >= 0.98 x meta duration -> download complete
LIVE_REFUSED = frozenset({"is_live", "is_upcoming"})
PLAYLIST_TYPES = frozenset({"playlist", "multi_video"})
MEDIA_KINDS = ("audio", "video_720", "video_1080", "muxed")
INFO_MAX_AGE_S = 4 * 3600  # cached info.json older than this has likely-expired media URLs
DIRECT_PROTOCOLS = frozenset({"http", "https"})
IMAGE_FORMATS = ("image2", "png_pipe", "jpeg_pipe", "webp_pipe", "bmp_pipe", "tiff_pipe", "gif_pipe", "svg_pipe")
MSG_MAX = 200

# Keys whose metadata only the latest yt-dlp could fetch: their downloads use it too.
_LATEST_KEYS: set[str] = set()


# --------------------------------------------------------------------------
# Error mapping
# --------------------------------------------------------------------------
def _code(preferred: str, fallback: str) -> str:
    """`preferred` when types.ERROR_CODES has it, else `fallback`.

    `unavailable` / `members_only` are proposed codes (ingest builder); until
    types.ERROR_CODES gains them they degrade to download_failed / login_required.
    """
    return preferred if preferred in ERROR_CODES else fallback


# (compiled pattern, code, fallback code, hint). First match wins: order matters
# ("Private video. Sign in if..." is private, not login; IG "not available ... use
# --cookies" is login, not unavailable).
_RULES: list[tuple[re.Pattern[str], str, str, str | None]] = [
    (re.compile(r"no space left on device|errno 28|disk quota exceeded"),
     "disk_full", "disk_full", "free disk space or point WFM_CACHE_DIR at a bigger disk"),
    (re.compile(r"unsupported url|is not a valid url|no suitable (info)?extractor"),
     "unsupported_url", "unsupported_url", None),
    (re.compile(r"this live event will begin|premieres in|live event has not started|will begin in a few moments"),
     "live_stream", "live_stream", "retry after the premiere/stream has ended"),
    (re.compile(r"members[- ]only|available to this channel's members|join this channel"),
     "members_only", "login_required", "members-only: retry with --cookies <browser> signed in as a member"),
    (re.compile(r"private video|this video is private|this account is private|private account|is private\b"),
     "private", "private", "only an account with access can watch it: --cookies <browser> signed in to it"),
    (re.compile(r"sign in to confirm|login required|log in to|use --cookies|--cookies-from-browser"
                r"|requires authentication|confirm your age|age[- ]restricted|registered users"
                r"|authentication is required|you need to log in|not logged in"),
     "login_required", "login_required", "retry with --cookies chrome"),
    (re.compile(r"not (made this video )?available in your country|available in your country"
                r"|geo[- ]?restrict|not available from your location|blocked it in your country"),
     "geo_blocked", "geo_blocked", None),
    (re.compile(r"http error 403"), "download_failed", "download_failed", None),
    (re.compile(r"video unavailable|this video has been removed|no longer available|has been terminated"
                r"|live stream recording is not available|this video is unavailable"
                r"|does not exist|http error 404|not found|no video could be found|has been deleted"
                r"|content is not available|video is unavailable|this post is unavailable"),
     "unavailable", "download_failed", None),
]


def _error_line(stderr: str) -> str:
    lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
    errs = [ln for ln in lines if ln.startswith("ERROR:")]
    line = (errs or lines or ["yt-dlp failed"])[-1]
    line = re.sub(r"^ERROR:\s*", "", line)
    line = re.split(r";\s*please report this issue", line, maxsplit=1, flags=re.IGNORECASE)[0]
    return line[:MSG_MAX]


def map_ytdlp_error(stderr: str) -> WfmError:
    """Classify a failed yt-dlp call from its stderr (case-insensitive substrings):
    "sign in to confirm" | "login required" | "use --cookies" -> login_required (hint "retry with --cookies chrome");
    "private video" -> private; "not available in your country" -> geo_blocked;
    "unsupported url" -> unsupported_url; "no space left" -> disk_full;
    "http error 403" -> download_failed with message containing "403" (callers retry);
    members-only -> members_only, removed/404/unavailable -> unavailable (when types has
    those codes, else login_required / download_failed); upcoming premiere -> live_stream;
    anything else -> download_failed. Message = last non-empty "ERROR:" line, <= 200 chars.
    Only ERROR lines (else the last line) are classified, so a WARNING mentioning cookies
    does not turn a network error into login_required.
    """
    msg = _error_line(stderr)
    low = msg.lower()
    for pat, code, fallback, hint in _RULES:
        if pat.search(low):
            err = WfmError(_code(code, fallback), msg, hint)
            err.ytdlp_class = code  # type: ignore[attr-defined]
            return err
    return WfmError("download_failed", msg, None)


def _worth_latest_retry(err: WfmError) -> bool:
    """Only unclassified download_failed / 403 errors may be fixed by a newer yt-dlp;
    removed/unavailable videos, logins, geo blocks etc. are not."""
    return err.code == "download_failed" and getattr(err, "ytdlp_class", "download_failed") == "download_failed"


def is_retryable_403(err: WfmError) -> bool:
    """True for the 403 download_failed produced by map_ytdlp_error."""
    return err.code == "download_failed" and "http error 403" in err.message.lower()


def _disk_error(e: OSError) -> WfmError:
    if e.errno in (errno.ENOSPC, errno.EDQUOT):
        return WfmError("disk_full", str(e), "free disk space or point WFM_CACHE_DIR at a bigger disk")
    return WfmError("download_failed", f"cache write failed: {e}")


# --------------------------------------------------------------------------
# Pure helpers on info dicts
# --------------------------------------------------------------------------
def trim_meta(info: dict[str, Any]) -> dict[str, Any]:
    """yt-dlp info dict -> {k: info.get(k) for k in types.META_KEYS}."""
    return {k: info.get(k) for k in META_KEYS}


def _has_codec(v: Any) -> bool | None:
    """True codec set, False explicitly "none", None unknown."""
    if v is None:
        return None
    return v != "none"


def has_split_formats(info: dict[str, Any]) -> bool:
    """True when `formats` has at least one audio-only (vcodec none, acodec set) AND one
    video-only (acodec none, vcodec set) format. False -> single muxed download.

    Only direct http(s) formats are considered when the site has any (X: split only over
    HLS, which is slow; its https formats are muxed -> muxed download).
    """
    fmts = [f for f in info.get("formats") or [] if isinstance(f, dict)]
    direct = [f for f in fmts if str(f.get("protocol") or "") in DIRECT_PROTOCOLS]
    pool = direct or fmts
    # like yt-dlp's ba/bv: "none" on one side makes it single-track (the other may be unknown)
    audio_only = any(_has_codec(f.get("vcodec")) is False and _has_codec(f.get("acodec")) is not False for f in pool)
    video_only = any(_has_codec(f.get("acodec")) is False and _has_codec(f.get("vcodec")) is not False for f in pool)
    return audio_only and video_only


def has_any_video(info: dict[str, Any]) -> bool:
    """False when every format is explicitly audio-only (vcodec "none"): podcasts, SoundCloud."""
    fmts = [f for f in info.get("formats") or [] if isinstance(f, dict)]
    if not fmts:
        return _has_codec(info.get("vcodec")) is not False
    return any(_has_codec(f.get("vcodec")) is not False for f in fmts)


def media_plan(split_formats: bool, opts: RunOptions, *, has_video: bool = True) -> dict[str, str | None]:
    """Which download feeds which branch: {"audio": kind|None, "video": kind|None}.

    split -> audio="audio", video="video_<res>"; muxed-only -> both "muxed" (ONE download);
    --audio-only / --video-only drop a branch; an audio-only site (has_video False) downloads
    only "audio". Helper for cli (not in the skeleton interface; additive).
    """
    want_a, want_v = not opts.video_only, not opts.audio_only and has_video
    if not has_video:
        return {"audio": "audio" if want_a else None, "video": None}
    if split_formats:
        return {"audio": "audio" if want_a else None, "video": f"video_{opts.resolution}" if want_v else None}
    return {"audio": "muxed" if want_a else None, "video": "muxed" if want_v else None}


def refusal(info: dict[str, Any], opts: RunOptions) -> WfmError | None:
    """Refusals checked right after meta (spec 4.1):
    live_status in LIVE_REFUSED -> live_stream;
    _type == "playlist" and opts.playlist is None -> playlist_refused, hint
        "rerun with --playlist N (N <= 10)" + entry count in message;
    watched span (--from/--to clipped to the duration, else the duration) > opts.max_minutes*60
        -> too_long (hint "use --from/--to or --max-minutes").
    Returns None when OK. Playlists with opts.playlist set are NOT refused here."""
    if info.get("live_status") in LIVE_REFUSED or info.get("is_live") is True:
        if info.get("live_status") == "is_upcoming":
            return WfmError("live_stream", "this stream or premiere has not started yet",
                            "retry once it has aired and ended")
        return WfmError("live_stream", "live streams are not supported while live",
                        "retry after the stream has ended (its recording works)")
    if info.get("_type") in PLAYLIST_TYPES:
        if opts.playlist is not None:
            return None
        n = info.get("playlist_count") or len(info.get("entries") or [])
        kind = "multi-video post" if info.get("extractor_key") in ("Twitter", "Instagram", "TikTok") else "playlist"
        return WfmError("playlist_refused", f"{kind} with {n} entries", "rerun with --playlist N (N <= 10)")
    dur = info.get("duration")
    if isinstance(dur, (int, float)):
        ranged = opts.from_s is not None or opts.to_s is not None
        span = watched_span(float(dur), opts.from_s, opts.to_s)
        if span > opts.max_minutes * 60:
            mins = f"{span / 60:.1f}" if span < 600 else f"{span / 60:.0f}"
            what = "range" if ranged else "video"
            hint = "use a shorter --from/--to range or a larger --max-minutes" if ranged \
                else "use --from/--to or --max-minutes"
            return WfmError("too_long", f"{mins} min {what} exceeds --max-minutes {opts.max_minutes}", hint)
    return None


def watched_span(duration: float, from_s: float | None, to_s: float | None) -> float:
    """Seconds actually watched: the --from/--to range clipped to the video (whole video
    without a range). `too_long` compares this, not the full duration."""
    lo = max(0.0, from_s or 0.0)
    hi = duration if to_s is None else min(duration, to_s)
    return max(0.0, hi - lo)


def _entry_url(entry: dict[str, Any]) -> str | None:
    for k in ("url", "webpage_url", "original_url"):
        v = entry.get(k)
        if isinstance(v, str) and v.lower().startswith(("http://", "https://")):
            return v
    return None


def _playlist_items(info: dict[str, Any], n: int) -> list[dict[str, Any]]:
    """First n usable entries: dicts carrying either a URL (flat) or full formats (embedded)."""
    out: list[dict[str, Any]] = []
    for e in info.get("entries") or []:
        if len(out) >= n:
            break
        if isinstance(e, dict) and (e.get("formats") or _entry_url(e)):
            out.append(e)
    return out


def _embedded(entry: dict[str, Any]) -> bool:
    """Fully extracted entry (X multi-video): use it as its own info.json."""
    return bool(entry.get("formats")) and entry.get("_type") in (None, "video")


# --------------------------------------------------------------------------
# yt-dlp invocation
# --------------------------------------------------------------------------
def _cookie_args(opts: RunOptions) -> list[str]:
    return ["--cookies-from-browser", opts.cookies] if opts.cookies else []


def _base(latest: bool) -> list[str] | None:
    """yt-dlp argv prefix; None when latest is asked for but uvx is missing."""
    if not latest:
        return proc.ytdlp_base()
    return proc.ytdlp_latest_base() if proc.which("uvx") else None


def meta_args(opts: RunOptions) -> list[str]:
    """META_ARGS adapted to --playlist (see module notes)."""
    args = list(META_ARGS)
    if opts.playlist is not None:
        args[args.index("--no-playlist")] = "--yes-playlist"
    return [*args, "-I", f"1:{opts.playlist or 1}"]


def download_args(kind: str, res: int | None = None) -> list[str]:
    """-S/-f for one media kind (spec 4.2). res defaults to the kind's own (720/1080)."""
    if kind == "audio":
        return ["-S", AUDIO_SORT, "-f", AUDIO_FORMAT]
    if kind.startswith("video_"):
        return ["-S", VIDEO_SORT.format(res=res or int(kind.split("_")[1])), "-f", VIDEO_FORMAT]
    if kind == "muxed":
        return ["-S", VIDEO_SORT.format(res=res or 720), "-f", MUXED_FORMAT]
    raise ValueError(f"unknown media kind {kind!r}")


def js_runtime_available() -> bool:
    """True if yt-dlp can find a JS runtime: deno in this env's scripts dir (yt-dlp[deno]
    extra) or on PATH, or node/bun on PATH. False -> cli emits `meta done warn=no_js_runtime`."""
    scripts = sysconfig.get_path("scripts") or os.path.dirname(sys.executable)
    for d in (scripts, os.path.dirname(sys.executable)):
        for name in ("deno", "deno.exe"):
            if os.path.isfile(os.path.join(d, name)):
                return True
    return any(proc.which(n) for n in ("deno", "node", "bun"))


async def fetch_info(url: str, opts: RunOptions, net: asyncio.Semaphore, *, latest: bool = False) -> dict[str, Any]:
    """`yt-dlp META_ARGS [--cookies-from-browser B] URL` -> parsed info dict.

    latest=True uses proc.ytdlp_latest_base() (uvx fallback).
    Raises WfmError via map_ytdlp_error on rc != 0 or bad JSON.
    """
    base = _base(latest)
    if base is None:
        raise WfmError("download_failed", "latest yt-dlp fallback needs uvx on PATH")
    cmd = [*base, *meta_args(opts), *proc.js_runtime_args(), *_cookie_args(opts), "--", url]
    async with net:
        r = await proc.arun(cmd)
    if r.returncode != 0:
        raise map_ytdlp_error(r.stderr)
    try:
        info = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        raise WfmError("download_failed", f"yt-dlp returned bad JSON: {e}") from None
    if not isinstance(info, dict):
        raise WfmError("download_failed", "yt-dlp returned no metadata")
    return info


async def _fetch_info_with_fallback(url: str, opts: RunOptions, net: asyncio.Semaphore) -> tuple[dict[str, Any], bool]:
    """fetch_info; on download_failed (extractor error), once more with latest yt-dlp.
    Returns (info, used_latest)."""
    try:
        return await fetch_info(url, opts, net), False
    except WfmError as e:
        if not _worth_latest_retry(e) or not proc.which("uvx"):
            raise
        try:
            return await fetch_info(url, opts, net, latest=True), True
        except WfmError:
            raise e from None


async def expand_playlist(info: dict[str, Any], n: int) -> list[str]:
    """First n entry URLs of a flat playlist info (entry "url", else "webpage_url",
    else construct from ie_key+id is NOT attempted -> skipped). Each entry is then resolved
    with its own fetch_info. Returns [] if no usable entries.

    resolve() uses _playlist_items instead so fully extracted (X multi-video) entries,
    which all carry the parent status URL, are not re-fetched."""
    return [u for e in _playlist_items(info, n) if (u := _entry_url(e))]


# --------------------------------------------------------------------------
# ffprobe
# --------------------------------------------------------------------------
async def _ffprobe(path: str) -> dict[str, Any] | None:
    try:
        r = await proc.arun(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", path])
    except FileNotFoundError:
        raise WfmError("download_failed", "ffprobe not found", "install ffmpeg (includes ffprobe)") from None
    if r.returncode != 0:
        return None
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    return d if isinstance(d, dict) else None


def _real_video_streams(probe: dict[str, Any]) -> list[dict[str, Any]]:
    """Video streams minus cover art (attached_pic) in audio files."""
    return [s for s in probe.get("streams") or [] if s.get("codec_type") == "video"
            and not (s.get("disposition") or {}).get("attached_pic")]


def _streams_summary(probe: dict[str, Any]) -> tuple[float | None, bool, bool]:
    """(duration, has_audio, has_video) from an ffprobe dict."""
    dur: float | None = None
    try:
        dur = float((probe.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        durs = []
        for s in probe.get("streams") or []:
            try:
                durs.append(float(s.get("duration")))
            except (TypeError, ValueError):
                pass
        dur = max(durs) if durs else None
    has_a = any(s.get("codec_type") == "audio" for s in probe.get("streams") or [])
    return dur, has_a, bool(_real_video_streams(probe))


async def ffprobe_duration(path: str) -> float | None:
    """format.duration of a media file, None if unreadable."""
    p = await _ffprobe(path)
    return _streams_summary(p)[0] if p else None


async def probe_local(path: str) -> dict[str, Any]:
    """ffprobe -v error -show_format -show_streams -of json PATH -> trimmed meta:
    {"id": <key suffix>, "title": format.tags.title or basename, "uploader": None, "channel": None,
     "upload_date": None, "duration": float, "chapters": None, "description": None,
     "webpage_url": None, "extractor_key": "local", "live_status": None, "language": None,
     "view_count": None, "tags": None, "width", "height" (first video stream or None),
     "local_path": abs path, "has_audio": bool, "has_video": bool}.
    Raises WfmError("unsupported_url") if ffprobe cannot read it (message: "not a media file").
    Still images and files with neither audio nor video are "not a media file" too.
    """
    ap = os.path.abspath(os.path.expanduser(path))
    p = await _ffprobe(ap)
    if not p:
        raise WfmError("unsupported_url", f"not a media file: {os.path.basename(ap)}")
    fmt = p.get("format") or {}
    dur, has_a, has_v = _streams_summary(p)
    fmt_name = str(fmt.get("format_name") or "")
    if (not has_a and not has_v) or fmt_name in IMAGE_FORMATS or not dur or dur <= 0:
        raise WfmError("unsupported_url", f"not a media file: {os.path.basename(ap)}")
    vs = _real_video_streams(p)
    tags = {str(k).lower(): v for k, v in (fmt.get("tags") or {}).items()}
    key = cache.key_for_local(ap)
    meta: dict[str, Any] = {k: None for k in META_KEYS}
    meta.update({
        "id": key.split("-", 1)[1], "title": tags.get("title") or os.path.basename(ap), "duration": dur,
        "extractor_key": "local", "width": vs[0].get("width") if vs else None,
        "height": vs[0].get("height") if vs else None,
        "local_path": ap, "has_audio": has_a, "has_video": has_v,
    })
    return meta


# --------------------------------------------------------------------------
# resolve
# --------------------------------------------------------------------------
def _local_path(input_str: str) -> str | None:
    s = input_str[7:] if input_str.lower().startswith("file://") else input_str
    p = os.path.expanduser(s)
    return os.path.abspath(p) if os.path.isfile(p) else None


def _write_info(kp: KeyPaths, info: dict[str, Any]) -> None:
    """info.json (for --load-info-json, atomic) + info.json.gz (kept)."""
    try:
        kp.dir.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(info, ensure_ascii=False, separators=(",", ":")).encode()
        tmp = kp.info_json.with_name(f"info.json.tmp.{os.getpid()}.{id(info)}")
        tmp.write_bytes(raw)
        os.replace(tmp, kp.info_json)
        tmpgz = kp.info_gz.with_name(f"info.json.gz.tmp.{os.getpid()}.{id(info)}")
        tmpgz.write_bytes(gzip.compress(raw, 6))
        os.replace(tmpgz, kp.info_gz)
    except OSError as e:
        raise _disk_error(e) from None


def _read_info_gz(kp: KeyPaths) -> dict[str, Any] | None:
    try:
        d = json.loads(gzip.decompress(kp.info_gz.read_bytes()))
    except (OSError, ValueError, EOFError):
        return None
    return d if isinstance(d, dict) else None


def _inflate_info(kp: KeyPaths, info: dict[str, Any]) -> None:
    try:
        tmp = kp.info_json.with_name(f"info.json.tmp.{os.getpid()}")
        tmp.write_text(json.dumps(info, ensure_ascii=False, separators=(",", ":")))
        os.replace(tmp, kp.info_json)
    except OSError as e:
        raise _disk_error(e) from None


def drop_info_json(kp: KeyPaths) -> None:
    """Delete the uncompressed info.json once downloads are done (info.json.gz stays)."""
    kp.info_json.unlink(missing_ok=True)


def _media_done(kp: KeyPaths, kinds: list[str]) -> bool:
    m = cache.load_manifest(kp)
    return all(cache.stage_fresh(m, f"media/{k}") for k in kinds)


def _store(kp: KeyPaths, info: dict[str, Any], meta: dict[str, Any]) -> None:
    try:
        cache.atomic_write_json(kp.meta, meta)
    except OSError as e:
        raise _disk_error(e) from None
    _write_info(kp, info)


def _resolved_from_info(input_str: str, info: dict[str, Any], *, used_latest: bool,
                        parent: str | None, index_input: bool) -> Resolved:
    """Step 5: key, meta.json, info.json(.gz), index.json."""
    ek = info.get("extractor_key") or info.get("ie_key") or info.get("extractor") or "generic"
    vid = info.get("id")
    if not vid:
        raise WfmError("download_failed", "yt-dlp metadata has no id")
    key = cache.key_for_info(str(ek), str(vid))
    kp = cache.key_paths(key)
    meta = trim_meta(info)
    old = cache.read_json(kp.meta)
    if isinstance(old, dict):  # stream facts learned by an earlier download stay valid
        meta.update({k: old[k] for k in ("has_audio", "has_video") if k in old})
    if not has_any_video(info):
        meta["has_video"] = False
    _store(kp, info, meta)
    if used_latest:
        _LATEST_KEYS.add(key)
    if index_input:
        cache.index_put(cache.normalize_input(input_str), key)
    return Resolved(input=input_str, key=key, meta=meta, info_json=str(kp.info_json), is_local=False,
                    split_formats=has_split_formats(info), index_hit=False, parent_input=parent)


async def _resolve_local(path: str, input_str: str, opts: RunOptions) -> Resolved:
    norm = cache.normalize_input(path)
    key = None if opts.fresh else cache.index_get(norm)
    meta = cache.read_json(cache.key_paths(key).meta) if key else None
    hit = bool(key and isinstance(meta, dict) and meta.get("local_path"))
    if not hit:
        meta = await probe_local(path)
        key = f"local-{meta['id']}"
        try:
            cache.atomic_write_json(cache.key_paths(key).meta, meta)
        except OSError as e:
            raise _disk_error(e) from None
        cache.index_put(norm, key)
    assert key is not None and isinstance(meta, dict)
    old_path = meta.get("local_path")
    if hit and old_path and meta.get("title") == os.path.basename(old_path):
        meta["title"] = os.path.basename(path)  # same content under another name
    meta["local_path"] = path
    err = refusal(meta, opts)
    if err:
        raise err
    return Resolved(input=input_str, key=key, meta=meta, info_json=None, is_local=True,
                    split_formats=False, index_hit=hit)


def _index_hit(input_str: str, opts: RunOptions, parent: str | None) -> Resolved | None:
    """Step 3: cached key -> Resolved without network (None on miss / stale / fresh)."""
    if opts.fresh:
        return None
    key = cache.index_get(cache.normalize_input(input_str))
    if not key:
        return None
    kp = cache.key_paths(key)
    meta = cache.read_json(kp.meta)
    info = _read_info_gz(kp)
    if not isinstance(meta, dict) or info is None:
        return None
    split = has_split_formats(info)
    plan = media_plan(split, opts, has_video=meta.get("has_video", True) is not False)
    kinds = sorted({k for k in plan.values() if k})
    if kinds and not _media_done(kp, kinds):
        try:
            age = time.time() - kp.info_gz.stat().st_mtime
        except OSError:
            return None
        if age > INFO_MAX_AGE_S:  # signed media URLs likely expired: re-fetch meta
            return None
        _inflate_info(kp, info)
    return Resolved(input=input_str, key=key, meta=meta, info_json=str(kp.info_json), is_local=False,
                    split_formats=split, index_hit=True, parent_input=parent)


async def _resolve_url(url: str, opts: RunOptions, net: asyncio.Semaphore, parent: str | None) -> Resolved:
    """Steps 3-5 for one video URL (a playlist entry or a single-video input)."""
    hit = _index_hit(url, opts, parent)
    if hit:
        err = refusal(hit.meta, opts)
        if err:
            raise err
        return hit
    info, latest = await _fetch_info_with_fallback(url, opts, net)
    if info.get("_type") in PLAYLIST_TYPES:
        # an entry URL that is itself a playlist: take its first entry only
        items = _playlist_items(info, 1)
        if not items:
            raise WfmError("download_failed", "playlist entry has no videos")
        if not _embedded(items[0]):
            eu = _entry_url(items[0])
            if not eu or eu == url:
                raise WfmError("download_failed", "playlist entry did not resolve to a video")
            info, latest = await _fetch_info_with_fallback(eu, opts, net)
        else:
            info = items[0]
    err = refusal(info, opts)
    if err:
        raise err
    return _resolved_from_info(url, info, used_latest=latest, parent=parent, index_input=True)


_TWITTER_VIDEO_RE = re.compile(r"/video/\d+/?$")


def _entry_input(entry: dict[str, Any], parent: str, i: int) -> str:
    """Input string for a playlist entry. Embedded X entries all carry the parent status
    URL, so they get their own `/video/<n>` URL (which resolves to that one video)."""
    url = _entry_url(entry) or parent
    if _embedded(entry) and entry.get("extractor_key") == "Twitter":
        base = _TWITTER_VIDEO_RE.sub("", url.split("?", 1)[0]).rstrip("/")
        return f"{base}/video/{entry.get('playlist_index') or i}"
    return url


async def _resolve_playlist(input_str: str, info: dict[str, Any], opts: RunOptions,
                            net: asyncio.Semaphore, latest: bool) -> list[Resolved | FailedEntry]:
    items = _playlist_items(info, opts.playlist or 0)
    if not items:
        raise WfmError("download_failed", "playlist has no playable entries")
    inputs = [_entry_input(e, input_str, i) for i, e in enumerate(items, 1)]

    async def one(e: dict[str, Any], u: str) -> Resolved | FailedEntry:
        try:
            if not _embedded(e):
                return await _resolve_url(u, opts, net, input_str)
            err = refusal(e, opts)
            if err:
                raise err
            unique = u != input_str and inputs.count(u) == 1
            return _resolved_from_info(u, e, used_latest=latest, parent=input_str, index_input=unique)
        except WfmError as ex:
            return FailedEntry(input=u, error=ex, parent_input=input_str)

    return list(await asyncio.gather(*(one(e, u) for e, u in zip(items, inputs))))


async def resolve(input_str: str, opts: RunOptions, net: asyncio.Semaphore) -> list[Resolved | FailedEntry]:
    """Resolve one CLI input into >= 1 items (several only for --playlist N).

    Order:
    1. existing local path -> key_for_local + probe_local; writes meta.json; index_put.
    2. not http(s) -> raise WfmError("unsupported_url", "not a link or an existing file").
    3. index_get(normalize_input) hit and not opts.fresh -> Resolved(index_hit=True, meta
       from <key>/meta.json, split_formats from info.json.gz; info.json re-inflated from
       info.json.gz only when a download stage is not done). No network.
    4. fetch_info -> refusal() (raise) -> if playlist and opts.playlist: expand_playlist and
       resolve each entry URL through steps 3-5 with parent_input=input_str; an entry that
       fails becomes FailedEntry (never raises for entries).
    5. key_for_info -> write meta.json, info.json.gz, info.json; index_put.
    Raises WfmError only for a failure of the top-level (non-playlist) input.
    Side effects: creates <key>/ and the files above (no manifest writes; cli owns the manifest).

    Builder additions: with --playlist N the top-level input skips the index (a cached
    single-video key must not stand in for the playlist; re-enumerating costs ~1-3 s) while
    its entries still hit it; an index hit whose info.json.gz is older than
    INFO_MAX_AGE_S while downloads are still pending re-fetches meta (expired media URLs).
    Meta errors classified download_failed are retried once with the latest yt-dlp.
    """
    s = input_str.strip()
    lp = _local_path(s)
    if lp:
        return [await _resolve_local(lp, s, opts)]
    if not cache.is_url(s):
        raise WfmError("unsupported_url", "not a link or an existing file",
                       "pass a full http(s):// URL or a path to a video/audio file")
    hit = None if opts.playlist is not None else _index_hit(s, opts, None)
    if hit:
        err = refusal(hit.meta, opts)
        if err:
            raise err
        return [hit]
    info, latest = await _fetch_info_with_fallback(s, opts, net)
    err = refusal(info, opts)
    if err:
        raise err
    if info.get("_type") in PLAYLIST_TYPES:
        return await _resolve_playlist(s, info, opts, net, latest)
    return [_resolved_from_info(s, info, used_latest=latest, parent=None, index_input=True)]


# --------------------------------------------------------------------------
# downloads
# --------------------------------------------------------------------------
def clean_partial(kp: KeyPaths, kind: str) -> None:
    """Delete media/<kind>.* before (re)downloading a stage that is not done
    (--no-part hazard, spec 4.2)."""
    d = kp.media_dir
    if not d.is_dir():
        return
    for p in d.glob(f"{kind}.*"):
        if p.is_file() or p.is_symlink():
            p.unlink(missing_ok=True)


def _parse_print(stdout: str) -> tuple[str, str | None] | None:
    for ln in reversed(stdout.splitlines()):
        ln = ln.strip()
        if "|" in ln:
            path, fid = ln.rsplit("|", 1)
            return path, (fid or None)
    return None


def _retryable(e: WfmError) -> bool:
    """403 or a truncated file: worth a fresh-URL retry."""
    return is_retryable_403(e) or e.message.startswith("incomplete ")


async def _refetch(kp: KeyPaths, resolved: Resolved, opts: RunOptions, net: asyncio.Semaphore,
                   *, latest: bool) -> None:
    """Re-fetch metadata (fresh signed URLs) and rewrite info.json/.gz.

    Uses resolved.input with --no-playlist (a playlist entry's input is its own video URL,
    e.g. X `/status/<id>/video/2`); if a playlist still comes back, the entry is matched by id.
    """
    single = dataclasses.replace(opts, playlist=None)
    info = await fetch_info(resolved.input, single, net, latest=latest)
    if info.get("_type") in PLAYLIST_TYPES:
        vid = resolved.meta.get("id")
        match = [e for e in info.get("entries") or [] if isinstance(e, dict) and e.get("id") == vid]
        if not match:
            raise WfmError("download_failed", "could not re-fetch this playlist entry")
        if not _embedded(match[0]):
            eu = _entry_url(match[0])
            if not eu:
                raise WfmError("download_failed", "could not re-fetch this playlist entry")
            info = await fetch_info(eu, single, net, latest=latest)
        else:
            info = match[0]
    _write_info(kp, info)


async def _download_once(kp: KeyPaths, resolved: Resolved, kind: str, opts: RunOptions,
                         net: asyncio.Semaphore, *, latest: bool) -> MediaFile:
    base = _base(latest)
    if base is None:
        raise WfmError("download_failed", "latest yt-dlp fallback needs uvx on PATH")
    if not kp.info_json.is_file():
        info = _read_info_gz(kp)
        if info is None:
            raise WfmError("download_failed", "cached metadata missing; rerun with --fresh")
        _inflate_info(kp, info)
    clean_partial(kp, kind)
    try:
        kp.media_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise _disk_error(e) from None
    res = opts.resolution if kind == "muxed" else None
    cmd = [*base, "--load-info-json", str(kp.info_json), *download_args(kind, res), *DL_COMMON,
           *proc.js_runtime_args(), *_cookie_args(opts), "-o", f"{kp.media_stem(kind)}.%(ext)s"]
    async with net:
        r = await proc.arun(cmd)
    if r.returncode != 0:
        clean_partial(kp, kind)
        raise map_ytdlp_error(r.stderr)
    parsed = _parse_print(r.stdout)
    path = parsed[0] if parsed else None
    if not path or not os.path.isfile(path):
        found = sorted(p for p in kp.media_dir.glob(f"{kind}.*") if p.is_file())
        if not found:
            raise WfmError("download_failed", f"yt-dlp finished but no {kind} file was written")
        path = str(found[0])
    probe = await _ffprobe(path)
    if not probe:
        clean_partial(kp, kind)
        raise WfmError("download_failed", f"downloaded {kind} file is not readable media")
    dur, has_a, has_v = _streams_summary(probe)
    want = resolved.meta.get("duration")
    if isinstance(want, (int, float)) and want > 0 and (dur is None or dur < COMPLETE_RATIO * want):
        clean_partial(kp, kind)
        got = "unknown" if dur is None else f"{dur:.1f}"
        raise WfmError("download_failed", f"incomplete {kind} download ({got} s of {want:.1f} s)")
    if kind in ("audio", "muxed"):
        resolved.meta["has_audio"] = has_a
    if kind != "audio":
        resolved.meta["has_video"] = has_v
    try:
        cache.atomic_write_json(kp.meta, resolved.meta)
    except OSError as e:
        raise _disk_error(e) from None
    fid = parsed[1] if parsed else None
    return MediaFile(path=os.path.abspath(path), kind=kind, format_id=fid, mb=round(os.path.getsize(path) / 1e6, 2))


async def download(kp: KeyPaths, resolved: Resolved, kind: str, opts: RunOptions,
                   net: asyncio.Semaphore) -> MediaFile:
    """Download one media file from resolved.info_json into media/<kind>.%(ext)s.

    kind: "audio" (AUDIO_SORT/AUDIO_FORMAT), "video_720"/"video_1080"
    (VIDEO_SORT res + VIDEO_FORMAT), "muxed" (VIDEO_SORT + MUXED_FORMAT).
    Steps: clean_partial -> `yt-dlp --load-info-json <info> -S .. -f .. DL_COMMON -o ..`
    parse the last "<path>|<format_id>" stdout line -> verify ffprobe_duration >=
    COMPLETE_RATIO x meta duration (skip check when meta duration is None) ->
    MediaFile. Retries: 403 -> re-fetch info (fetch_info, rewrite info.json) and retry,
    up to MAX_403_RETRIES; then one retry with latest=True yt-dlp; then raise.
    Raises WfmError (download_failed, login_required, disk_full, ...).
    Does NOT touch the manifest (caller records stage + media path).

    Builder additions: an incomplete file (duration check) is retried like a 403;
    the latest-yt-dlp retry re-fetches info with that yt-dlp first; a successful
    download records meta["has_audio"] (audio/muxed) / meta["has_video"] (video/muxed)
    in resolved.meta and meta.json so cli can raise no_audio / no_video for a muxed file
    (also on later cached runs).
    kind "audio" with no audio stream raises no_audio; "video_*" without video, no_video.
    """
    if kind not in MEDIA_KINDS:
        raise ValueError(f"unknown media kind {kind!r}")
    latest = resolved.key in _LATEST_KEYS
    mf: MediaFile | None = None
    err: WfmError | None = None
    for attempt in range(MAX_403_RETRIES + 1):
        try:
            if attempt:
                await asyncio.sleep(RETRY_BACKOFF_S[min(attempt - 1, len(RETRY_BACKOFF_S) - 1)])
                await _refetch(kp, resolved, opts, net, latest=latest)
            mf = await _download_once(kp, resolved, kind, opts, net, latest=latest)
            break
        except WfmError as e:
            err = e
            if not _retryable(e):
                break
    if mf is None:
        assert err is not None
        if not _worth_latest_retry(err) or latest or not proc.which("uvx"):
            raise err
        try:
            await _refetch(kp, resolved, opts, net, latest=True)
            mf = await _download_once(kp, resolved, kind, opts, net, latest=True)
        except WfmError:
            raise err from None
        _LATEST_KEYS.add(resolved.key)
    if kind == "audio" and resolved.meta.get("has_audio") is False:
        raise WfmError("no_audio", "the video has no audio track", None)
    if kind.startswith("video_") and resolved.meta.get("has_video") is False:
        raise WfmError("no_video", "the link has no video track", None)
    return mf


def local_media(resolved: Resolved) -> MediaFile:
    """MediaFile(kind="local") pointing at the user's file (never copied into the cache)."""
    path = str(resolved.meta.get("local_path") or resolved.input)
    try:
        mb = round(Path(path).stat().st_size / 1e6, 2)
    except OSError:
        raise WfmError("unsupported_url", f"local file vanished: {path}") from None
    return MediaFile(path=path, kind="local", format_id=None, mb=mb)
