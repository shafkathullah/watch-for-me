"""Keyframes: ffmpeg scene extraction, dHash dedupe, segmenting, caps, frames.json,
and on-demand zoom for the `frame` subcommand (spec 4.3).

All functions are SYNC and CPU-bound; cli runs them via asyncio.to_thread under
the CPU semaphore. ffmpeg/ffprobe go through wfm.proc.run (registered children;
nice=True on the cpu backend). Pillow only (no imagehash/scipy/numpy).

cli call order for one view:
    out = extract_segments(video, vp, duration=..., from_s, to_s, hires, nice)
    sheets = sheets.make_sheets(out.segments, vp.dir, src_w=out.src_w, src_h=out.src_h, hires=...)
    light = sheets.make_light_sheets(out.segments, vp.dir, ..., chapters=meta["chapters"])
    write_frames_json(vp, out, sheets, light=light)
Port of scratchpad/ingest/frames.py (run_extract, dhash, select), with the
spec's changes: 256-bit dHash, repeat_bits=10, 0.4 s micro-segment merge,
cap = clamp(20 x minutes, 24, 240), f_<ms9>.jpg renames.

Builder decisions (see segment() docstring for the exact order):
- Repeat detection runs AFTER the cap merges (spec lists it before). Merges move
  representatives around, so detecting repeats last keeps every `repeat_of` pointing
  at a final segment whose tile really shows that content.
- The cap counts every segment (repeats included): conservative, frames.json stays bounded.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from itertools import pairwise
from pathlib import Path

from . import proc
from .cache import KeyPaths, ViewPaths, atomic_write_json, read_json
from .types import (
    MAX_IMAGE_SIDE,
    FramesOutput,
    FramesParams,
    HashedFrame,
    RawFrame,
    Segment,
    Sheet,
    WfmError,
    ms9,
)

SCENE_THRESHOLD = 0.15
FLOOR_S = 7
DUP_BITS = 20
REPEAT_BITS = 10
HEARTBEAT_S = 60
HEARTBEAT_BITS = 4
MICRO_SEGMENT_S = 0.4
CAP_PER_MIN = 20
CAP_MIN, CAP_MAX = 24, 240
MAXSPAN_FACTOR = 3.0
KEY_MODE_MIN_DURATION = 7200.0  # keyframe mode only when duration > 2 h ...
KEY_MODE_MAX_GOP = 8.0  # ... AND max GOP <= 8 s
WIDTH_DEFAULT, WIDTH_HIRES = 1280, 1920
PAR_PART_MIN_S = 600.0  # scene mode: split extraction into ranges of >= 10 min ...
PAR_MAX_PARTS = 4  # ... run in parallel (the select/scene filter is single-threaded:
# 1 h 720p 16.3 s serial -> 5.6 s with 4 parts on an M1 Pro; the extra frame each part
# selects at its start is absorbed by dedupe)
ZOOM_DEFAULT_WIDTH = 1456
ZOOM_MIN_WIDTH = 64
FIRST_NOVELTY = 256  # novelty of the first segment (all 256 bits "new")
FFPROBE_TIMEOUT_S = 120.0
ZOOM_TIMEOUT_S = 120.0

_PTS_RE = re.compile(r"\bpts_time:\s*(-?[0-9]+(?:\.[0-9]+)?(?:e-?[0-9]+)?)")
_RAW_RE = re.compile(r"^raw_\d{5,}\.jpg$")


# --------------------------------------------------------------------------
# Params / mode
# --------------------------------------------------------------------------
def _span(duration: float, from_s: float | None, to_s: float | None) -> tuple[float, float]:
    """(t_start, t_end) of the view range inside [0, duration]."""
    t0 = max(0.0, from_s or 0.0)
    t1 = duration if to_s is None else min(to_s, duration) if duration > 0 else to_s
    return t0, max(t0, t1)


def resolve_cap(duration: float, from_s: float | None = None, to_s: float | None = None) -> int:
    """clamp(round(CAP_PER_MIN x minutes of the view range), CAP_MIN, CAP_MAX)."""
    t0, t1 = _span(duration, from_s, to_s)
    return max(CAP_MIN, min(CAP_MAX, round(CAP_PER_MIN * (t1 - t0) / 60.0)))


def frames_params(duration: float, *, hires: bool, mode: str = "scene",
                  from_s: float | None = None, to_s: float | None = None) -> FramesParams:
    """FramesParams for a view (width 1920 if hires else 1280, cap = resolve_cap)."""
    return FramesParams(
        mode="key" if mode == "key" else "scene", scene=SCENE_THRESHOLD, floor_s=FLOOR_S,
        width=WIDTH_HIRES if hires else WIDTH_DEFAULT, dup_bits=DUP_BITS, repeat_bits=REPEAT_BITS,
        heartbeat_s=HEARTBEAT_S, cap=resolve_cap(duration, from_s, to_s),
    )


def probe_max_gop(video: str) -> float:
    """Max keyframe interval (s): ffprobe -v error -select_streams v:0
    -show_entries packet=pts_time,flags -of csv=p=0 VIDEO (lines with ",K").
    Returns inf when < 2 keyframes."""
    r = proc.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                  "packet=pts_time,flags", "-of", "csv=p=0", video], timeout=FFPROBE_TIMEOUT_S)
    return max_gop_from_csv(r.stdout)


def max_gop_from_csv(text: str) -> float:
    """Pure part of probe_max_gop: largest gap between consecutive keyframe pts."""
    keys: list[float] = []
    for line in text.splitlines():
        parts = line.strip().split(",")
        if len(parts) < 2 or "K" not in parts[1]:
            continue
        try:
            keys.append(float(parts[0]))
        except ValueError:
            continue
    if len(keys) < 2:
        return float("inf")
    keys.sort()
    return max(b - a for a, b in pairwise(keys))


def choose_mode(duration: float, max_gop: float | None) -> str:
    """"key" iff duration > KEY_MODE_MIN_DURATION and max_gop <= KEY_MODE_MAX_GOP, else "scene".
    (cli only probes the GOP when duration > 2 h.)"""
    if duration > KEY_MODE_MIN_DURATION and max_gop is not None and max_gop <= KEY_MODE_MAX_GOP:
        return "key"
    return "scene"


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------
def ffmpeg_extract_cmd(video: str, frames_dir: Path, params: FramesParams, *,
                       from_s: float | None, to_s: float | None,
                       ffmpeg_version: tuple[int, int] | None) -> list[str]:
    """Exact spec 4.3 command. scene mode:
    ffmpeg -v info -hide_banner -nostats -threads 0 -skip_frame noref [-ss FROM -to TO] -i VIDEO
      -an -sn -dn -vf "select='isnan(prev_selected_t)+gt(scene\\,S)+gte(t-prev_selected_t\\,F)',
      showinfo,scale='min(W,iw)':-2" (-fps_mode|-vsync) passthrough -q:v 3 frames/raw_%05d.jpg
    key mode: -skip_frame nokey, vf "showinfo,scale=...". `-fps_mode` iff ffmpeg >= 5.1
    (None -> assume new). Never -hwaccel."""
    scale = f"scale='min({params.width},iw)':-2"
    if params.mode == "key":
        skip, vf = "nokey", f"showinfo,{scale}"
    else:
        sel = (f"select='isnan(prev_selected_t)+gt(scene\\,{params.scene:g})"
               f"+gte(t-prev_selected_t\\,{params.floor_s:g})'")
        skip, vf = "noref", f"{sel},showinfo,{scale}"
    rng: list[str] = []
    if from_s:
        rng += ["-ss", f"{from_s:.3f}"]
    if to_s is not None:
        rng += ["-to", f"{to_s:.3f}"]
    sync = "-fps_mode" if ffmpeg_version is None or ffmpeg_version >= (5, 1) else "-vsync"
    return ["ffmpeg", "-v", "info", "-hide_banner", "-nostats", "-threads", "0",
            "-skip_frame", skip, *rng, "-i", video, "-an", "-sn", "-dn", "-vf", vf,
            sync, "passthrough", "-q:v", "3", str(frames_dir / "raw_%05d.jpg")]


def parse_pts(stderr: str) -> list[float]:
    """All showinfo `pts_time:` values, in output order."""
    return [float(m) for m in _PTS_RE.findall(stderr)]


def probe_video_stream(video: str) -> tuple[int, int] | None:
    """(width, height) of the first real video stream, or None when the file has none.
    Cover art (disposition attached_pic, e.g. an mp3/m4a with album art) is not video."""
    r = proc.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries",
                  "stream=width,height:stream_disposition=attached_pic", "-of", "csv=p=0", video],
                 timeout=FFPROBE_TIMEOUT_S)
    if r.returncode != 0:
        tail = " ".join(r.stderr.split())[-200:]
        raise WfmError("frames_failed", f"ffprobe failed: {tail or 'rc=' + str(r.returncode)}")
    for line in r.stdout.splitlines():
        parts = [p for p in line.strip().split(",") if p != ""]
        if len(parts) < 3:
            continue
        try:
            w, h, pic = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        if not pic and w > 0 and h > 0:
            return w, h
    return None


def _clear_dir(frames_dir: Path, *patterns: str) -> None:
    for pat in patterns:
        for p in frames_dir.glob(pat):
            try:
                p.unlink()
            except FileNotFoundError:
                pass


def extract_candidates(video: str, frames_dir: Path, params: FramesParams, *,
                       from_s: float | None = None, to_s: float | None = None,
                       nice: bool = False) -> list[RawFrame]:
    """Run ffmpeg_extract_cmd; parse `pts_time:` from showinfo stderr; assert count ==
    raw files. With -ss as an input option pts restart near 0, so from_s is ADDED to every
    parsed pts (timestamps stay absolute; verified on a real file, see tests).
    Deletes stale raw_*.jpg first. Raises WfmError("frames_failed") on ffmpeg failure or
    count mismatch; WfmError("no_video") when the file has no video stream."""
    if probe_video_stream(video) is None:
        raise WfmError("no_video", "no video stream in the media file")
    frames_dir.mkdir(parents=True, exist_ok=True)
    _clear_dir(frames_dir, "raw_*.jpg")
    cmd = ffmpeg_extract_cmd(video, frames_dir, params, from_s=from_s, to_s=to_s,
                             ffmpeg_version=proc.ffmpeg_version())
    r = proc.run(cmd, nice=nice)
    if r.returncode != 0:
        tail = " ".join(r.stderr.split())[-200:]
        raise WfmError("frames_failed", f"ffmpeg exited {r.returncode}: {tail}")
    pts = parse_pts(r.stderr)
    files = sorted(p for p in os.listdir(frames_dir) if _RAW_RE.match(p))
    if len(files) != len(pts):
        raise WfmError("frames_failed", f"showinfo reported {len(pts)} frames but ffmpeg wrote {len(files)}")
    if not files:
        raise WfmError("frames_failed", "ffmpeg extracted no frames (range outside the video?)")
    off = from_s or 0.0
    return [RawFrame(t=max(0.0, t + off), path=str(frames_dir / f)) for t, f in zip(pts, files)]


def extraction_parts(t_start: float, t_end: float, mode: str, *, cpus: int | None = None,
                     ffmpeg_version: tuple[int, int] | None = None) -> list[tuple[float, float]]:
    """Ranges for parallel scene extraction: one part per PAR_PART_MIN_S, at most
    PAR_MAX_PARTS and cpus // 2. A single part (serial, the spec command) for key mode,
    short spans, unknown durations and ffmpeg < 5.1 (input-side -to)."""
    span = t_end - t_start
    cpus = cpus if cpus is not None else (os.cpu_count() or 2)
    n = min(PAR_MAX_PARTS, max(1, cpus // 2), int(span // PAR_PART_MIN_S)) if span > 0 else 1
    old = ffmpeg_version is not None and ffmpeg_version < (5, 1)
    if mode != "scene" or n <= 1 or old:
        return [(t_start, t_end)]
    step = span / n
    return [(t_start + i * step, t_end if i == n - 1 else t_start + (i + 1) * step) for i in range(n)]


def extract_parallel(video: str, frames_dir: Path, params: FramesParams,
                     parts: list[tuple[float, float]], *, open_end: bool = False,
                     nice: bool = False) -> list[RawFrame]:
    """extract_candidates per part (into frames_dir/part<i>/, concurrently), then move the
    raws into frames_dir as one raw_%05d sequence in time order. Absolute timestamps.
    `open_end`: no -to on the last part (metadata durations are often rounded down)."""
    from concurrent.futures import ThreadPoolExecutor

    dirs = [frames_dir / f"part{i}" for i in range(len(parts))]
    try:
        with ThreadPoolExecutor(len(parts)) as ex:
            last = len(parts) - 1
            futs = [ex.submit(extract_candidates, video, d, params, from_s=a or None,
                              to_s=None if open_end and i == last else b, nice=nice)
                    for i, (d, (a, b)) in enumerate(zip(dirs, parts))]
            results = [f.result() for f in futs]
        out: list[RawFrame] = []
        for raws in results:
            for r in raws:
                dst = frames_dir / f"raw_{len(out) + 1:05d}.jpg"
                os.replace(r.path, dst)
                out.append(RawFrame(t=r.t, path=str(dst)))
        return out
    finally:
        for d in dirs:
            if d.is_dir():
                _clear_dir(d, "raw_*.jpg")
                try:
                    d.rmdir()
                except OSError:
                    pass


# --------------------------------------------------------------------------
# Hashing + segmenting
# --------------------------------------------------------------------------
def dhash(path: str | Path, size: int = 16) -> int:
    """256-bit difference hash: grayscale, resize to (size+1) x size BILINEAR, bit =
    left > right, row-major, MSB first (port of prototype dhash)."""
    from PIL import Image

    with Image.open(path) as im:
        px = im.convert("L").resize((size + 1, size), Image.Resampling.BILINEAR).tobytes()
    bits = 0
    stride = size + 1
    for y in range(size):
        row = px[y * stride:(y + 1) * stride]
        for x in range(size):
            bits = (bits << 1) | (row[x] > row[x + 1])
    return bits


def hamming(a: int, b: int) -> int:
    """Popcount of a ^ b."""
    return (a ^ b).bit_count()


class _Seg:
    """Mutable working segment for segment()."""

    __slots__ = ("h_first", "h_rep", "novelty", "path", "t", "t0", "t1")

    def __init__(self, t0: float, f: HashedFrame, novelty: int) -> None:
        self.t0 = t0
        self.t1 = t0
        self.t = f.t
        self.path = f.path
        self.h_first = f.hash
        self.h_rep = f.hash
        self.novelty = novelty

    def take_rep(self, other: _Seg) -> None:
        self.t, self.path, self.h_rep = other.t, other.path, other.h_rep


def _group(frames: list[HashedFrame], params: FramesParams) -> list[_Seg]:
    """Step 2-3: consecutive near-duplicates -> segments; representative = last frame."""
    segs: list[_Seg] = []
    cur: _Seg | None = None
    for f in frames:
        if cur is None:
            cur = _Seg(f.t, f, FIRST_NOVELTY)
            continue
        d = hamming(f.hash, cur.h_first)
        if d > params.dup_bits or (f.t - cur.t0 >= params.heartbeat_s and d > HEARTBEAT_BITS):
            segs.append(cur)
            cur = _Seg(f.t, f, d)
        else:
            cur.t, cur.path, cur.h_rep = f.t, f.path, f.hash
    if cur is not None:
        segs.append(cur)
    return segs


def _set_spans(segs: list[_Seg], t_start: float, t_end: float) -> None:
    if not segs:
        return
    segs[0].t0 = t_start
    for a, b in pairwise(segs):
        a.t1 = b.t0
    segs[-1].t1 = max(t_end, segs[-1].t0)


def _merge_micro(segs: list[_Seg]) -> list[_Seg]:
    """Step 4: a segment shorter than MICRO_SEGMENT_S merges into the NEXT one (next.t0 =
    its t0; next keeps its representative; novelty = max of both). The last segment has no
    next: it merges into its predecessor (predecessor keeps its representative)."""
    out: list[_Seg] = []
    carry: _Seg | None = None
    for s in segs:
        if carry is not None:
            s.t0 = carry.t0
            s.novelty = max(s.novelty, carry.novelty)
            carry = None
        if s.t1 - s.t0 < MICRO_SEGMENT_S:
            carry = s
            continue
        out.append(s)
    if carry is not None:
        if out:
            out[-1].t1 = carry.t1
        else:
            out.append(carry)  # the whole view is one micro segment: keep it
    return out


def _apply_cap(segs: list[_Seg], cap: int, span_total: float) -> None:
    """Step 6: while over cap, merge the least-novel segment into its predecessor
    (predecessor takes its t1 + representative), skipping merges whose span would exceed
    MAXSPAN_FACTOR x (duration / cap). When EVERY merge is blocked by the span rule, the
    rule is dropped for that step (the least-novel merge happens anyway), so the result is
    always exactly `cap` segments. Ties: lower novelty, then shorter merged span, then earlier."""
    cap = max(cap, 1)
    maxspan = MAXSPAN_FACTOR * span_total / cap
    while len(segs) > cap:
        best: tuple[int, float, int] | None = None
        fallback: tuple[int, float, int] | None = None
        for k in range(1, len(segs)):
            span = segs[k].t1 - segs[k - 1].t0
            cand = (segs[k].novelty, span, k)
            if fallback is None or cand < fallback:
                fallback = cand
            if span <= maxspan and (best is None or cand < best):
                best = cand
        pick = best or fallback
        assert pick is not None
        k = pick[2]
        prev, s = segs[k - 1], segs[k]
        prev.t1 = s.t1
        prev.take_rep(s)
        del segs[k]


def segment(frames: list[HashedFrame], *, t_start: float, t_end: float,
            params: FramesParams) -> list[Segment]:
    """Pure dedupe/segmenting (spec 4.3 steps 2-6). Input in time order.

    2. New segment when hamming(frame, segment's FIRST frame) > dup_bits, or when the
       segment is >= heartbeat_s old and distance > HEARTBEAT_BITS.
       novelty = that distance (FIRST_NOVELTY for the first segment).
    3. Representative = LAST frame of the segment (t, file=frame path for now, hash).
       t0 = first frame t (first segment t0 = t_start); t1 = next segment's t0 (last: t_end).
    4. Segments shorter than MICRO_SEGMENT_S merge into the NEXT one (next.t0 = this.t0;
       the last one merges into its predecessor).
    6. While count > params.cap: merge the least-novel segment into its predecessor
       (predecessor takes its t1 and representative), skipping merges whose span would
       exceed MAXSPAN_FACTOR x (duration / cap); if no merge is allowed, ignore the span rule.
    5. (run last, see module docstring) repeat_of = n of the earliest earlier non-repeat
       segment whose representative is within repeat_bits.
    Numbered n = 1..N in time order. `file` holds the raw path here; finalize_files rewrites it.
    """
    frames = sorted(frames, key=lambda f: f.t)
    segs = _group(frames, params)
    _set_spans(segs, t_start, t_end)
    segs = _merge_micro(segs)
    _apply_cap(segs, params.cap, max(0.0, t_end - t_start))
    out: list[Segment] = []
    originals: list[Segment] = []
    for i, s in enumerate(segs, start=1):
        seg = Segment(n=i, t0=s.t0, t1=s.t1, t=s.t, file=s.path, novelty=s.novelty, hash=s.h_rep)
        for o in originals:
            if hamming(seg.hash, o.hash) <= params.repeat_bits:
                seg.repeat_of = o.n
                break
        else:
            originals.append(seg)
        out.append(seg)
    return out


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------
def finalize_files(segments: list[Segment], view: ViewPaths) -> None:
    """Rename each representative raw jpg to frames/f_<ms9(t)>.jpg, set seg.file to the
    view-relative "frames/f_<ms9>.jpg", delete every remaining raw_*.jpg (spec 4.3 step 7)."""
    fdir = view.frames_dir
    used: set[str] = set()
    for seg in segments:
        name = f"f_{ms9(seg.t)}.jpg"
        if name in used:  # two representatives within the same ms: never overwrite
            ms = int(ms9(seg.t))
            while f"f_{ms:09d}.jpg" in used:
                ms += 1
            name = f"f_{ms:09d}.jpg"
        used.add(name)
        src = Path(seg.file)
        dst = fdir / name
        if src != dst:
            os.replace(src, dst)
        seg.file = f"frames/{name}"
    _clear_dir(fdir, "raw_*.jpg")


def extract_segments(video: str, view: ViewPaths, *, duration: float, hires: bool,
                     from_s: float | None = None, to_s: float | None = None,
                     nice: bool = False) -> FramesOutput:
    """Full frames stage minus sheets: mode choice (GOP probe only if the view range > 2 h),
    extract_candidates, dhash each, segment, finalize_files. Returns FramesOutput
    (src_w/src_h from the first kept jpg). Raises WfmError(frames_failed|no_video)."""
    from PIL import Image

    t_start, t_end = _span(duration, from_s, to_s)
    span = t_end - t_start
    mode = "scene"
    if span > KEY_MODE_MIN_DURATION:
        mode = choose_mode(span, probe_max_gop(video))
    params = frames_params(duration, hires=hires, mode=mode, from_s=from_s, to_s=to_s)
    fdir = view.frames_dir
    fdir.mkdir(parents=True, exist_ok=True)
    _clear_dir(fdir, "raw_*.jpg", "f_*.jpg")
    t = time.monotonic()
    parts = extraction_parts(t_start, t_end, mode, ffmpeg_version=proc.ffmpeg_version()) if duration > 0 \
        else [(t_start, t_end)]
    if len(parts) > 1:
        raw = extract_parallel(video, fdir, params, parts, open_end=to_s is None, nice=nice)
    else:
        raw = extract_candidates(video, fdir, params, from_s=from_s, to_s=to_s, nice=nice)
    extract_s = time.monotonic() - t
    t = time.monotonic()
    try:
        hashed = [HashedFrame(t=r.t, hash=dhash(r.path), path=r.path) for r in raw]
    except OSError as e:
        raise WfmError("frames_failed", f"unreadable frame: {e}") from e
    if duration <= 0:  # unknown duration (some local files): end at the last frame
        t_end = max(t_end, raw[-1].t)
    segs = segment(hashed, t_start=t_start, t_end=t_end, params=params)
    finalize_files(segs, view)
    dedupe_s = time.monotonic() - t
    with Image.open(view.dir / segs[0].file) as im:
        src_w, src_h = im.size
    return FramesOutput(params=params, raw_count=len(raw), segments=segs, src_w=src_w,
                        src_h=src_h, extract_s=extract_s, dedupe_s=dedupe_s)


def write_frames_json(view: ViewPaths, out: FramesOutput, sheets: list[Sheet], *,
                      light: list[Sheet] | None = None) -> None:
    """Atomic write of views/<vtag>/frames.json (spec 3 shape):
    {"v":1,"params":out.params.to_dict(),"raw_count","segments":[Segment.to_dict()],
     "sheets":[Sheet.to_dict()]} plus (builder addition) "light_sheets":[Sheet.to_dict()]
    for the --tldr light-visuals path (sheets.make_light_sheets); [] when not built."""
    atomic_write_json(view.frames_json, {
        "v": 1,
        "params": out.params.to_dict(),
        "raw_count": out.raw_count,
        "src": {"w": out.src_w, "h": out.src_h},
        "segments": [s.to_dict() for s in out.segments],
        "sheets": [s.to_dict() for s in sheets],
        "light_sheets": [s.to_dict() for s in (light or [])],
    })


def load_frames_json(view: ViewPaths) -> dict | None:
    """Parsed frames.json or None."""
    data = read_json(view.frames_json)
    return data if isinstance(data, dict) else None


# --------------------------------------------------------------------------
# Zoom (`frame` subcommand)
# --------------------------------------------------------------------------
def zoom_path(kp: KeyPaths, t: float, width: int, crop: tuple[int, int, int, int] | None,
              src: str = "") -> Path:
    """zoom/f_<ms9(t)>_w<width>[_<src>][_c<x>-<y>-<w>-<h>].jpg. `src` names the video the frame
    was cut from (zoom_source_tag), so a zoom made from 720p is never served once 1080p exists."""
    name = f"f_{ms9(t)}_w{width}"
    if src:
        name += f"_{src}"
    if crop is not None:
        name += "_c{}-{}-{}-{}".format(*crop)
    return kp.zoom_dir / f"{name}.jpg"


def pick_zoom_source(kp: KeyPaths, manifest: dict) -> str:
    """Best cached video for zoom: media video_1080 > video_720 > muxed > source.local_path.
    Raises WfmError("no_video") when none exists."""
    return _zoom_source(kp, manifest)[1]


def zoom_source_tag(kind: str, path: str) -> str:
    """"v1080" | "v720" | "mux" | "local" plus 6 hex of (size, mtime): changes whenever the
    zoom source changes (better download, --fresh, edited local file)."""
    short = {"video_1080": "v1080", "video_720": "v720", "muxed": "mux"}.get(kind, "local")
    st = os.stat(path)
    return f"{short}-{hashlib.sha1(f'{st.st_size}:{st.st_mtime_ns}'.encode()).hexdigest()[:6]}"


def _zoom_source(kp: KeyPaths, manifest: dict) -> tuple[str, str]:
    """(kind, path) of pick_zoom_source."""
    media = manifest.get("media") or {}
    for kind in ("video_1080", "video_720", "muxed"):
        rel = media.get(kind)
        if rel:
            p = Path(rel) if Path(rel).is_absolute() else kp.dir / rel
            if p.is_file():
                return kind, str(p)
    local = (manifest.get("source") or {}).get("local_path")
    if local and Path(local).is_file():
        return "local", str(local)
    raise WfmError("no_video", "no cached video for this key (audio-only run?)",
                   hint="rerun without --audio-only to download the video")


def _clamp_width(width: int) -> int:
    return max(ZOOM_MIN_WIDTH, min(MAX_IMAGE_SIDE, int(width)))


def _meta_duration(kp: KeyPaths) -> float | None:
    meta = read_json(kp.meta)
    if isinstance(meta, dict):
        d = meta.get("duration")
        if isinstance(d, (int, float)) and d > 0:
            return float(d)
    return None


def _ffmpeg_grab(video: str, t: float, vf: str, out: Path) -> None:
    """One frame at t through `vf` into out (atomic: tmp + os.replace). When t is at/after
    the real end of the stream (meta duration is often rounded up), the LAST frame is used
    instead (`-sseof -1`, every frame overwrites the same file)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.stem}.tmp{os.getpid()}.jpg")
    tail_cmd = ["-an", "-sn", "-dn", "-vf", vf, "-q:v", "2", "-update", "1", str(tmp)]
    base = ["ffmpeg", "-v", "error", "-hide_banner", "-nostdin", "-y"]
    r = proc.run([*base, "-ss", f"{t:.3f}", "-i", video, "-frames:v", "1", *tail_cmd], timeout=ZOOM_TIMEOUT_S)
    if not _nonempty(tmp):
        r = proc.run([*base, "-sseof", "-1", "-i", video, *tail_cmd], timeout=ZOOM_TIMEOUT_S)
    if not _nonempty(tmp):
        tmp.unlink(missing_ok=True)
        tail = " ".join(r.stderr.split())[-200:]
        raise WfmError("frames_failed", f"no frame at t={t:.3f}s: {tail or 'empty output'}")
    os.replace(tmp, out)


def _nonempty(p: Path) -> bool:
    return p.is_file() and p.stat().st_size > 0


def zoom(kp: KeyPaths, manifest: dict, t: float, *, width: int = ZOOM_DEFAULT_WIDTH,
         crop: tuple[int, int, int, int] | None = None) -> Path:
    """`frame KEY --t T [--crop X,Y,W,H] [--width N]` (spec 3; 0.26 s seek, 0.45 s crop [M]).

    width is clamped to [64, types.MAX_IMAGE_SIDE]. t is clamped to [0, duration - 0.05]
    when meta.json knows the duration. Without crop: the frame at t (accurate input seek
    `-ss T -i VIDEO -frames:v 1`) scaled to fit width x MAX_IMAGE_SIDE (never upscaled).
    With crop: X,Y,W,H are pixel coords IN THE PLAIN `frame KEY --t T` IMAGE (default width
    ZOOM_DEFAULT_WIDTH, whatever `width` this call asks for), so the box the agent measured
    lands in the same place however large it asks for the crop. That reference image is
    produced first (cached), the box is clamped to it, mapped back to source pixels via
    iw/ih ratios, cut from the full-res frame, and scaled so its width is `width` (upscaling
    allowed, max side MAX_IMAGE_SIDE). Raises ValueError for a crop box outside the image
    (cli: exit 2). Cached per source video (zoom_source_tag). Returns the absolute jpg path.
    """
    from PIL import Image

    width = _clamp_width(width)
    dur = _meta_duration(kp)
    t = max(0.0, float(t))
    if dur is not None:
        t = min(t, max(0.0, dur - 0.05))
    kind, video = _zoom_source(kp, manifest)
    out = zoom_path(kp, t, width, crop, zoom_source_tag(kind, video))
    if out.is_file():
        return out.resolve()
    if crop is None:
        vf = (f"scale=w='min({width},iw)':h='min({MAX_IMAGE_SIDE},ih)'"
              ":force_original_aspect_ratio=decrease")
        _ffmpeg_grab(video, t, vf, out)
        return out.resolve()
    base = zoom(kp, manifest, t, width=ZOOM_DEFAULT_WIDTH, crop=None)
    with Image.open(base) as im:
        bw, bh = im.size
    x, y, cw, ch = crop
    x, y = max(0, x), max(0, y)
    cw, ch = min(cw, bw - x), min(ch, bh - y)
    if cw < 2 or ch < 2:
        raise ValueError(f"crop {crop} is outside the {bw}x{bh} frame image")
    ow = width
    oh = max(2, round(width * ch / cw))
    if oh > MAX_IMAGE_SIDE:
        oh = MAX_IMAGE_SIDE
        ow = max(2, round(MAX_IMAGE_SIDE * cw / ch))
    vf = (f"crop=w='iw*{cw}/{bw}':h='ih*{ch}/{bh}':x='iw*{x}/{bw}':y='ih*{y}/{bh}':exact=1,"
          f"scale={ow}:{oh}:flags=lanczos")
    _ffmpeg_grab(video, t, vf, out)
    return out.resolve()
