"""wfm.frames: dHash, segmenting rules (spec 4.3), ffmpeg command, pts parsing, file
finalizing, zoom naming/source. Pure tests need only Pillow; the `ffmpeg` tests generate a
tiny lavfi fixture under tmp_path and are skipped when ffmpeg/ffprobe are missing."""

from __future__ import annotations

import json
import random
import shutil
from itertools import pairwise
from pathlib import Path

import pytest
from PIL import Image, ImageDraw
from wfm import frames
from wfm.cache import KeyPaths, ViewPaths
from wfm.types import FramesParams, HashedFrame, WfmError

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not on PATH")

RNG = random.Random(7)
FULL = (1 << 256) - 1


def flip(h: int, bits: int, *, start: int = 0) -> int:
    """h with `bits` distinct bits flipped (deterministic positions from `start`)."""
    for i in range(bits):
        h ^= 1 << ((start + i * 37) % 256)
    return h


def rand_hash() -> int:
    return RNG.getrandbits(256)


def hf(t: float, h: int) -> HashedFrame:
    return HashedFrame(t=t, hash=h, path=f"/raw/{t:.3f}.jpg")


def params(cap: int = 240) -> FramesParams:
    return FramesParams(cap=cap)


# --------------------------------------------------------------------------
# hashing
# --------------------------------------------------------------------------
def _img(path: Path, draw_rect: bool, color: str = "white") -> Path:
    im = Image.new("RGB", (320, 180), "black")
    d = ImageDraw.Draw(im)
    for x in range(0, 320, 40):
        d.rectangle((x, 0, x + 19, 179), fill=(x % 255, 80, 200 - x % 200))
    if draw_rect:
        d.rectangle((60, 40, 260, 140), fill=color)
    im.save(path, quality=90)
    return path


def test_dhash_is_256_bit_and_stable(tmp_path: Path) -> None:
    a = _img(tmp_path / "a.jpg", False)
    h = frames.dhash(a)
    assert 0 <= h < 1 << 256
    assert h.bit_length() > 200  # a striped image sets bits across the whole grid
    assert frames.dhash(a) == h
    # re-encoded at another size/quality: near-identical
    b = tmp_path / "b.jpg"
    Image.open(a).resize((640, 360)).save(b, quality=60)
    assert frames.hamming(h, frames.dhash(b)) <= 10
    c = _img(tmp_path / "c.jpg", True)
    assert frames.hamming(h, frames.dhash(c)) > 20


def test_hamming() -> None:
    assert frames.hamming(0, 0) == 0
    assert frames.hamming(0, FULL) == 256
    assert frames.hamming(0b1011, 0b0001) == 2


# --------------------------------------------------------------------------
# cap / params / mode
# --------------------------------------------------------------------------
def test_resolve_cap() -> None:
    assert frames.resolve_cap(30) == 24  # clamp low
    assert frames.resolve_cap(300) == 100  # 20 x 5 min
    assert frames.resolve_cap(3600) == 240  # clamp high
    assert frames.resolve_cap(3600, 60, 240) == 60  # 3 min range
    assert frames.resolve_cap(100, None, 1000) == 33  # to clamped to duration


def test_frames_params() -> None:
    p = frames.frames_params(149, hires=False)
    assert p.to_dict() == {"mode": "scene", "scene": 0.15, "floor_s": 7, "width": 1280, "dup_bits": 20,
                           "repeat_bits": 10, "heartbeat_s": 60, "cap": 50}
    assert frames.frames_params(149, hires=True, mode="key").width == 1920
    assert frames.frames_params(149, hires=True, mode="key").mode == "key"


def test_choose_mode_and_gop() -> None:
    assert frames.choose_mode(7201, 8.0) == "key"
    assert frames.choose_mode(7200, 2.0) == "scene"  # needs > 2 h
    assert frames.choose_mode(9000, 8.4) == "scene"  # GOP too long
    assert frames.choose_mode(9000, None) == "scene"
    csv = "0.000000,K__\n0.033,___\n2.0,K_\n\n10.5,K__\n11,__\nbad,K\n"
    assert frames.max_gop_from_csv(csv) == pytest.approx(8.5)
    assert frames.max_gop_from_csv("0.0,K_\n1.0,__\n") == float("inf")


# --------------------------------------------------------------------------
# ffmpeg command + pts parsing
# --------------------------------------------------------------------------
def test_ffmpeg_extract_cmd_scene() -> None:
    cmd = frames.ffmpeg_extract_cmd("/v.mp4", Path("/f"), params(), from_s=None, to_s=None,
                                    ffmpeg_version=(8, 0))
    assert cmd == ["ffmpeg", "-v", "info", "-hide_banner", "-nostats", "-threads", "0", "-skip_frame", "noref",
                   "-i", "/v.mp4", "-an", "-sn", "-dn", "-vf",
                   ("select='isnan(prev_selected_t)+gt(scene\\,0.15)+gte(t-prev_selected_t\\,7)',showinfo,"
                    "scale='min(1280,iw)':-2"), "-fps_mode", "passthrough", "-q:v", "3", "/f/raw_%05d.jpg"]
    assert "-hwaccel" not in cmd


def test_ffmpeg_extract_cmd_variants() -> None:
    p = FramesParams(mode="key", width=1920)
    cmd = frames.ffmpeg_extract_cmd("/v.mp4", Path("/f"), p, from_s=12.5, to_s=30, ffmpeg_version=(4, 4))
    assert cmd[cmd.index("-skip_frame") + 1] == "nokey"
    assert cmd[cmd.index("-vf") + 1] == "showinfo,scale='min(1920,iw)':-2"
    assert "-vsync" in cmd and "-fps_mode" not in cmd  # ffmpeg < 5.1
    i = cmd.index("-ss")
    assert cmd[i:i + 4] == ["-ss", "12.500", "-to", "30.000"] and cmd.index("-i") == i + 4  # input options
    new = frames.ffmpeg_extract_cmd("/v", Path("/f"), p, from_s=None, to_s=None, ffmpeg_version=(5, 1))
    assert "-fps_mode" in new and "-ss" not in new
    assert "-fps_mode" in frames.ffmpeg_extract_cmd("/v", Path("/f"), p, from_s=0, to_s=None, ffmpeg_version=None)


def test_parse_pts() -> None:
    err = (
        "[Parsed_showinfo_1 @ 0x1] config in time_base: 1/15360\n"
        "[Parsed_showinfo_1 @ 0x1] n:   0 pts:      0 pts_time:0       duration:512 fmt:yuv420p\n"
        "[Parsed_showinfo_1 @ 0x1]   side data - ...\n"
        "[Parsed_showinfo_1 @ 0x1] n:   1 pts: 107520 pts_time:7.0     duration:512\n"
        "[Parsed_showinfo_1 @ 0x1] n:   2 pts: 122880 pts_time:8.333333 duration:512\n"
    )
    assert frames.parse_pts(err) == [0.0, 7.0, 8.333333]


# --------------------------------------------------------------------------
# segment(): spec 4.3 steps 2-6
# --------------------------------------------------------------------------
def test_segment_groups_and_uses_last_frame() -> None:
    a, b = rand_hash(), rand_hash()
    fr = [hf(0, a), hf(7, flip(a, 5)), hf(14, flip(a, 20)),  # <= 20 bits from the FIRST frame: same
          hf(20, b), hf(27, flip(b, 3))]
    segs = frames.segment(fr, t_start=0, t_end=30, params=params())
    assert [(s.n, s.t0, s.t1, s.t) for s in segs] == [(1, 0, 20, 14), (2, 20, 30, 27)]
    assert segs[0].file == "/raw/14.000.jpg" and segs[0].hash == flip(a, 20)
    assert segs[0].novelty == 256
    assert segs[1].novelty == frames.hamming(b, a)
    assert all(s.repeat_of is None for s in segs)


def test_segment_distance_is_to_first_frame_not_previous() -> None:
    a = rand_hash()
    # each step drifts 8 bits (never > 20 from the previous frame), but 24 from the first
    fr = [hf(0, a), hf(7, flip(a, 8)), hf(14, flip(a, 16)), hf(21, flip(a, 24))]
    segs = frames.segment(fr, t_start=0, t_end=28, params=params())
    assert [s.t0 for s in segs] == [0, 21]


def test_segment_first_t0_is_range_start_and_last_t1_range_end() -> None:
    a = rand_hash()
    segs = frames.segment([hf(12.3, a), hf(19.3, a)], t_start=12.0, t_end=33.0, params=params())
    assert len(segs) == 1 and segs[0].t0 == 12.0 and segs[0].t1 == 33.0 and segs[0].t == 19.3


def test_segment_heartbeat() -> None:
    a = rand_hash()
    # static talk: frames flicker by 5 bits (> 4, far below dup_bits) every 7 s
    fr = [hf(t, flip(a, 5 * (k % 2))) for k, t in enumerate(range(0, 140, 7))]
    segs = frames.segment(fr, t_start=0, t_end=140, params=params())
    assert [s.t0 for s in segs] == [0, 63, 126]
    # drift <= 4 bits: no heartbeat split
    fr2 = [hf(t, flip(a, 4 * (k % 2))) for k, t in enumerate(range(0, 140, 7))]
    assert len(frames.segment(fr2, t_start=0, t_end=140, params=params())) == 1


def test_segment_micro_merges_into_next() -> None:
    a, fade, b = rand_hash(), rand_hash(), rand_hash()
    fr = [hf(0, a), hf(10.0, fade), hf(10.3, b), hf(17.3, b)]
    segs = frames.segment(fr, t_start=0, t_end=20, params=params())
    assert [(s.t0, s.t1, s.t) for s in segs] == [(0, 10.0, 0), (10.0, 20, 17.3)]
    assert segs[1].hash == b  # representative stays the later (real) slide
    assert segs[1].novelty == max(frames.hamming(fade, a), frames.hamming(b, fade))


def test_segment_micro_last_merges_into_previous() -> None:
    a, b = rand_hash(), rand_hash()
    segs = frames.segment([hf(0, a), hf(19.8, b)], t_start=0, t_end=20, params=params())
    assert [(s.t0, s.t1, s.t) for s in segs] == [(0, 20, 0)]
    one = frames.segment([hf(0, a)], t_start=0, t_end=0.2, params=params())
    assert len(one) == 1 and one[0].t1 == 0.2


def test_segment_repeats() -> None:
    a, b, c = rand_hash(), rand_hash(), rand_hash()
    fr = [hf(0, a), hf(10, b), hf(20, flip(a, 10)), hf(30, c), hf(40, flip(b, 11)), hf(50, flip(a, 3))]
    segs = frames.segment(fr, t_start=0, t_end=60, params=params())
    assert [s.repeat_of for s in segs] == [None, None, 1, None, None, 1]


def test_segment_cap_merges_least_novel_into_predecessor() -> None:
    hashes = [rand_hash() for _ in range(40)]
    fr = [hf(i * 10.0, h) for i, h in enumerate(hashes)]
    segs = frames.segment(fr, t_start=0, t_end=400, params=params(cap=24))
    assert len(segs) == 24
    assert [s.n for s in segs] == list(range(1, 25))
    # spans tile the range with no gaps
    assert segs[0].t0 == 0 and segs[-1].t1 == 400
    assert all(x.t1 == y.t0 for x, y in pairwise(segs))
    # max span rule respected: 3 x (400 / 24) = 50 s
    assert max(s.t1 - s.t0 for s in segs) <= 50 + 1e-9
    # representative of a merged segment is the LAST merged frame
    for s in segs:
        assert s.t == max(f.t for f in fr if s.t0 <= f.t < s.t1)


def test_segment_cap_picks_lowest_novelty_respecting_maxspan() -> None:
    a = rand_hash()
    fr = [hf(0, a)]
    h = a
    for i in range(1, 30):
        h = flip(h, 21 + i, start=i)  # novelty rises with i (each > dup_bits)
        fr.append(hf(i * 10.0, h))
    segs = frames.segment(fr, t_start=0, t_end=300, params=params(cap=24))
    # maxspan = 3 x 300 / 24 = 37.5 s. Least novel first: 10, 20 -> into 0; 30 blocked
    # (0..40 > 37.5) so 40, 50 -> into 30; 60 blocked, 70, 80 -> into 60.
    assert [s.t0 for s in segs] == [0, 30, 60, *[90 + 10 * i for i in range(21)]]
    assert segs[0].t == 20 and segs[1].t == 50 and segs[2].t == 80


def test_segment_cap_when_all_merges_blocked() -> None:
    fr = [hf(i * 1000.0, rand_hash()) for i in range(26)]
    p = params(cap=24)
    work = frames._group(fr, p)
    frames._set_spans(work, 0, 26000)
    frames._apply_cap(work, 24, 1.0)  # maxspan = 0.125 s: every merge is blocked
    assert len(work) == 24  # defined outcome: span rule dropped, least-novel merges still happen
    assert work[0].t0 == 0 and work[-1].t1 == 26000
    assert all(x.t1 == y.t0 for x, y in pairwise(work))


def test_segment_repeats_detected_after_cap() -> None:
    a, b = rand_hash(), rand_hash()
    fr = [hf(0, a), hf(10, b)]
    fr += [hf(20 + i * 10, rand_hash()) for i in range(30)]
    fr += [hf(400, flip(a, 2))]
    segs = frames.segment(fr, t_start=0, t_end=410, params=params(cap=24))
    by_n = {s.n: s for s in segs}
    for s in segs:
        if s.repeat_of is not None:
            assert s.repeat_of < s.n
            assert by_n[s.repeat_of].repeat_of is None
            assert frames.hamming(s.hash, by_n[s.repeat_of].hash) <= 10


def test_segment_unsorted_input_and_empty() -> None:
    a, b = rand_hash(), rand_hash()
    segs = frames.segment([hf(10, b), hf(0, a)], t_start=0, t_end=20, params=params())
    assert [s.t for s in segs] == [0, 10]
    assert frames.segment([], t_start=0, t_end=10, params=params()) == []


# --------------------------------------------------------------------------
# files / frames.json / zoom helpers
# --------------------------------------------------------------------------
def test_finalize_files(tmp_path: Path) -> None:
    view = ViewPaths(tmp_path / "views" / "full-720", "full-720")
    view.frames_dir.mkdir(parents=True)
    raws = []
    for i in range(1, 5):
        p = view.frames_dir / f"raw_{i:05d}.jpg"
        p.write_bytes(b"x" * i)
        raws.append(p)
    a, b = rand_hash(), rand_hash()
    segs = frames.segment([HashedFrame(0, a, str(raws[0])), HashedFrame(7, a, str(raws[1])),
                           HashedFrame(12.1, b, str(raws[2])), HashedFrame(19.1, b, str(raws[3]))],
                          t_start=0, t_end=20, params=params())
    frames.finalize_files(segs, view)
    assert [s.file for s in segs] == ["frames/f_000007000.jpg", "frames/f_000019100.jpg"]
    assert sorted(p.name for p in view.frames_dir.iterdir()) == ["f_000007000.jpg", "f_000019100.jpg"]
    assert (view.frames_dir / "f_000019100.jpg").read_bytes() == b"xxxx"


def test_zoom_path_and_source(tmp_path: Path) -> None:
    kp = KeyPaths(tmp_path, "youtube-abc")
    assert frames.zoom_path(kp, 235.0, 1456, None).name == "f_000235000_w1456.jpg"
    assert frames.zoom_path(kp, 1.2345, 800, (1, 2, 30, 40)).name == "f_000001234_w800_c1-2-30-40.jpg"
    assert frames.zoom_path(kp, 1.0, 800, None, "v1080-abc123").name == "f_000001000_w800_v1080-abc123.jpg"
    (kp.media_dir).mkdir(parents=True)
    with pytest.raises(WfmError) as ei:
        frames.pick_zoom_source(kp, {"media": {"audio": "media/audio.webm"}})
    assert ei.value.code == "no_video"
    (kp.media_dir / "video_720.mp4").write_bytes(b"v")
    (kp.media_dir / "muxed.mp4").write_bytes(b"m")
    m = {"media": {"video_720": "media/video_720.mp4", "muxed": "media/muxed.mp4", "video_1080": None}}
    assert frames.pick_zoom_source(kp, m).endswith("video_720.mp4")
    (kp.media_dir / "video_1080.mp4").write_bytes(b"v")
    m["media"]["video_1080"] = "media/video_1080.mp4"
    assert frames.pick_zoom_source(kp, m).endswith("video_1080.mp4")
    local = tmp_path / "clip.mov"
    local.write_bytes(b"l")
    assert frames.pick_zoom_source(kp, {"media": {}, "source": {"local_path": str(local)}}) == str(local)


# --------------------------------------------------------------------------
# real ffmpeg on a generated fixture (never written into the repo)
# --------------------------------------------------------------------------
def _fixture(tmp_path: Path) -> Path:
    """40 s, 1280x720, 4 scenes of 10 s (testsrc2, bars, mandelbrot, testsrc2 again), keyint 250."""
    out = tmp_path / "fixture.mp4"
    fc = ("[2:v]trim=duration=10,setpts=PTS-STARTPTS[m];"
          "[0:v][1:v][m][3:v]concat=n=4:v=1:a=0,format=yuv420p[v]")
    frames.proc.run(["ffmpeg", "-v", "error", "-y",
                     "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25:duration=10",
                     "-f", "lavfi", "-i", "smptebars=size=1280x720:rate=25:duration=10",
                     "-f", "lavfi", "-i", "mandelbrot=size=1280x720:rate=25",
                     "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25:duration=10",
                     "-filter_complex", fc, "-map", "[v]", "-c:v", "libx264", "-g", "250",
                     "-preset", "ultrafast", str(out)], check=True)
    return out


@pytest.fixture(scope="module")
def fixture_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg/ffprobe not on PATH")
    return _fixture(tmp_path_factory.mktemp("fx"))


@needs_ffmpeg
def test_extract_segments_real(tmp_path: Path, fixture_video: Path) -> None:
    view = ViewPaths(tmp_path / "views" / "full-720", "full-720")
    out = frames.extract_segments(str(fixture_video), view, duration=40.0, hires=False)
    starts = [s.t0 for s in out.segments]
    for cut in (10.0, 20.0):  # hard cuts caught exactly (scene mode)
        assert any(abs(t - cut) < 0.05 for t in starts), starts
    assert out.segments[-1].repeat_of == 1  # testsrc2 comes back
    assert (out.src_w, out.src_h) == (1280, 720)
    assert out.raw_count >= len(out.segments)
    names = sorted(p.name for p in view.frames_dir.iterdir())
    assert names == sorted(Path(s.file).name for s in out.segments)  # raws deleted
    frames.write_frames_json(view, out, [])
    data = frames.load_frames_json(view)
    assert data is not None and data["v"] == 1 and data["params"]["cap"] == 24
    assert len(data["segments"]) == len(out.segments) and data["light_sheets"] == []


@needs_ffmpeg
def test_extract_range_timestamps_absolute(tmp_path: Path, fixture_video: Path) -> None:
    view = ViewPaths(tmp_path / "v", "r12000-33000-720")
    out = frames.extract_segments(str(fixture_video), view, duration=40.0, hires=False, from_s=12, to_s=33)
    assert out.segments[0].t0 == 12 and out.segments[-1].t1 == 33
    assert any(abs(s.t0 - 20.0) < 0.05 for s in out.segments)  # the 20 s cut stays at 20 s
    assert all(12 <= s.t <= 33 for s in out.segments)


def test_extraction_parts() -> None:
    assert frames.extraction_parts(0, 3588, "scene", cpus=10) == [
        (0, 897.0), (897.0, 1794.0), (1794.0, 2691.0), (2691.0, 3588)]
    assert frames.extraction_parts(0, 1500, "scene", cpus=10) == [(0, 750.0), (750.0, 1500)]
    assert frames.extraction_parts(0, 3588, "scene", cpus=4) == [(0, 1794.0), (1794.0, 3588)]
    assert frames.extraction_parts(0, 599, "scene", cpus=10) == [(0, 599)]  # short: serial
    assert frames.extraction_parts(0, 3588, "key", cpus=10) == [(0, 3588)]
    assert frames.extraction_parts(0, 3588, "scene", cpus=10, ffmpeg_version=(4, 4)) == [(0, 3588)]
    assert frames.extraction_parts(100, 1300, "scene", cpus=10)[0] == (100, 700.0)


@needs_ffmpeg
def test_extract_parallel_matches_serial(tmp_path: Path, fixture_video: Path,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """Parts split mid-scene: cuts stay exact, raws renumbered, part dirs removed."""
    monkeypatch.setattr(frames, "PAR_PART_MIN_S", 13.0)
    monkeypatch.setattr(frames.os, "cpu_count", lambda: 8)
    view = ViewPaths(tmp_path / "views" / "full-720", "full-720")
    out = frames.extract_segments(str(fixture_video), view, duration=40.0, hires=False)
    starts = [s.t0 for s in out.segments]
    for cut in (10.0, 20.0):
        assert any(abs(t - cut) < 0.05 for t in starts), starts
    assert out.segments[-1].repeat_of == 1
    assert sorted(p.name for p in view.frames_dir.iterdir()) == sorted(Path(s.file).name for s in out.segments)


@needs_ffmpeg
def test_extract_no_video(tmp_path: Path) -> None:
    audio = tmp_path / "a.m4a"
    frames.proc.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=duration=2", str(audio)], check=True)
    with pytest.raises(WfmError) as ei:
        frames.extract_candidates(str(audio), tmp_path / "f", params())
    assert ei.value.code == "no_video" and not ei.value.fatal


@needs_ffmpeg
def test_zoom_real(tmp_path: Path, fixture_video: Path) -> None:
    kp = KeyPaths(tmp_path, "local-x")
    kp.dir.mkdir()
    (kp.dir / "meta.json").write_text('{"duration": 40.0}')
    m = {"media": {}, "source": {"local_path": str(fixture_video)}}
    p = frames.zoom(kp, m, 15, width=640)
    assert p.is_file() and Image.open(p).size == (640, 360)
    assert frames.zoom(kp, m, 15, width=640) == p  # cached
    big = frames.zoom(kp, m, 15, width=5000)  # clamped to 2000, never upscaled
    assert "_w2000_local-" in big.name and Image.open(big).size == (1280, 720)
    c = frames.zoom(kp, m, 15, width=640, crop=(320, 180, 160, 90))  # a box in the plain image
    assert Image.open(c).size == (640, 360)  # upscaled to the requested width
    end = frames.zoom(kp, m, 99, width=320)  # past the end: clamped, last frame
    assert Image.open(end).size == (320, 180)
    with pytest.raises(ValueError):
        frames.zoom(kp, m, 15, width=640, crop=(1300, 0, 10, 10))


@needs_ffmpeg
def test_zoom_crop_box_independent_of_width(tmp_path: Path, fixture_video: Path) -> None:
    """Review #5: the crop box is measured on the plain (default-width) image, so asking for a
    bigger crop (--width 2000) cuts the same region, only larger."""
    kp = KeyPaths(tmp_path, "local-x")
    kp.dir.mkdir()
    (kp.dir / "meta.json").write_text('{"duration": 40.0}')
    m = {"media": {}, "source": {"local_path": str(fixture_video)}}
    box = (640, 0, 640, 360)  # top-right quarter of the 1280x720 plain image
    small = Image.open(frames.zoom(kp, m, 25, width=640, crop=box)).convert("L").resize((64, 36))
    large = Image.open(frames.zoom(kp, m, 25, width=2000, crop=box)).convert("L").resize((64, 36))
    diff = sum(abs(a - b) for a, b in zip(small.tobytes(), large.tobytes(), strict=True)) / (64 * 36)
    assert diff < 8, diff


@needs_ffmpeg
def test_zoom_cache_follows_source(tmp_path: Path, fixture_video: Path) -> None:
    """Review #4: a zoom cut from 720p is not served again once a 1080p video is cached."""
    kp = KeyPaths(tmp_path, "youtube-x")
    kp.media_dir.mkdir(parents=True)
    (kp.dir / "meta.json").write_text('{"duration": 40.0}')
    v720 = kp.media_dir / "video_720.mp4"
    frames.proc.run(["ffmpeg", "-v", "error", "-y", "-i", str(fixture_video), "-vf", "scale=640:360",
                     "-preset", "ultrafast", str(v720)], check=True)
    m = {"media": {"video_720": "media/video_720.mp4"}}
    a = frames.zoom(kp, m, 15, width=1920)
    assert Image.open(a).size == (640, 360) and "_v720-" in a.name
    (kp.media_dir / "video_1080.mp4").write_bytes(fixture_video.read_bytes())
    m["media"]["video_1080"] = "media/video_1080.mp4"
    b = frames.zoom(kp, m, 15, width=1920)
    assert b != a and "_v1080-" in b.name and Image.open(b).size == (1280, 720)


@needs_ffmpeg
def test_frame_cli_crop_outside_is_exit_2(wfm_cache: Path, fixture_video: Path,
                                          capsys: pytest.CaptureFixture[str]) -> None:
    """Review #6: a crop box outside the image is a usage error (exit 2), not a traceback."""
    from wfm import cli

    kp = KeyPaths(wfm_cache, "local-x")
    kp.dir.mkdir()
    (kp.dir / "meta.json").write_text('{"duration": 40.0}')
    (kp.dir / "manifest.json").write_text(json.dumps(
        {"v": 1, "stages": {}, "media": {}, "source": {"local_path": str(fixture_video)}}))
    assert cli.main(["frame", "local-x", "--t", "15", "--crop", "5000,5000,100,100"]) == 2
    assert "outside" in capsys.readouterr().err
    assert cli.main(["frame", "local-x", "--t", "15", "--crop", "0,0,100,100"]) == 0
