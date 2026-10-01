"""wfm.ingest: pure parts (error mapping, format/split detection, refusals, yt-dlp args,
playlist entry handling) plus retry/resolve flows with yt-dlp and ffprobe faked.
No network. Tests that need ffmpeg or the cache implementation skip when missing."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from wfm import cache, ingest, proc
from wfm.types import ERROR_CODES, Resolved, RunOptions, WfmError


def opts(**kw: Any) -> RunOptions:
    return RunOptions(inputs=[], run_id="wfmtest01", **kw)


def cache_ready() -> bool:
    try:
        cache.key_for_info("Youtube", "x")
    except NotImplementedError:
        return False
    return True


needs_cache = pytest.mark.skipif(not cache_ready(), reason="wfm.cache not implemented yet")
needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg missing")


# --------------------------------------------------------------------------
# error mapping
# --------------------------------------------------------------------------
UNAVAILABLE = "unavailable" if "unavailable" in ERROR_CODES else "download_failed"
MEMBERS = "members_only" if "members_only" in ERROR_CODES else "login_required"


@pytest.mark.parametrize(("stderr", "code"), [
    (("ERROR: [youtube] abc: Sign in to confirm you're not a bot. Use --cookies-from-browser or --cookies "
      "for the authentication."), "login_required"),
    ("ERROR: [youtube] abc: Sign in to confirm your age. This video may be inappropriate for some users.",
     "login_required"),
    (("ERROR: [Instagram] C1: Requested content is not available, rate-limit reached or login required. "
      "Use --cookies, --cookies-from-browser, --username and --password"), "login_required"),
    ("ERROR: [youtube] abc: Private video. Sign in if you've been granted access to this video", "private"),
    ("ERROR: [youtube] abc: The uploader has not made this video available in your country", "geo_blocked"),
    ("ERROR: Unsupported URL: https://example.com/page", "unsupported_url"),
    ("ERROR: [generic] 'nope' is not a valid URL.", "unsupported_url"),
    ("ERROR: unable to write data: [Errno 28] No space left on device", "disk_full"),
    ("ERROR: unable to download video data: HTTP Error 403: Forbidden", "download_failed"),
    ("ERROR: [youtube] abc: This live event will begin in 3 hours.", "live_stream"),
    ("ERROR: [youtube] abc: Premieres in 10 hours", "live_stream"),
    ("ERROR: [youtube] abc: Join this channel to get access to members-only content like this video",
     MEMBERS),
    ("ERROR: [youtube] abc: Video unavailable. This video has been removed by the uploader", UNAVAILABLE),
    ("ERROR: [twitter] 123: No video could be found in this tweet", UNAVAILABLE),
    ("ERROR: [youtube] jfKfPfyJRdk: This live stream recording is not available.", UNAVAILABLE),
    ("ERROR: [youtube] aaaaaaaaaaa: This video is unavailable", UNAVAILABLE),
    (("ERROR: [Instagram] X: Instagram sent an empty media response. Check if this post is accessible in "
      "your browser without being logged-in. If it is not, then use --cookies-from-browser"), "login_required"),
    ("ERROR: something completely new broke", "download_failed"),
    ("", "download_failed"),
])
def test_map_error_codes(stderr: str, code: str) -> None:
    assert map_code(stderr) == code


def map_code(stderr: str) -> str:
    return ingest.map_ytdlp_error(stderr).code


def test_map_error_message_hint_and_warning_lines() -> None:
    e = ingest.map_ytdlp_error(
        "WARNING: [youtube] cookies are old, use --cookies to refresh\n"
        "ERROR: [youtube] abc: Sign in to confirm you're not a bot; please report this issue on https://x\n")
    assert e.code == "login_required"
    assert e.hint == "retry with --cookies chrome"
    assert e.message == "[youtube] abc: Sign in to confirm you're not a bot"
    # a WARNING that mentions cookies must not classify a plain network error
    e2 = ingest.map_ytdlp_error("WARNING: use --cookies for better results\nERROR: timed out\n")
    assert e2.code == "download_failed" and e2.message == "timed out"
    long = ingest.map_ytdlp_error("ERROR: " + "x" * 500)
    assert len(long.message) <= 200


def test_latest_retry_only_for_unclassified() -> None:
    worth = ingest._worth_latest_retry
    assert worth(ingest.map_ytdlp_error("ERROR: [youtube] x: nsig extraction failed"))
    assert worth(ingest.map_ytdlp_error("ERROR: HTTP Error 403: Forbidden"))
    assert not worth(ingest.map_ytdlp_error("ERROR: [youtube] x: Video unavailable"))
    assert not worth(ingest.map_ytdlp_error("ERROR: [youtube] x: Private video"))
    assert not worth(ingest.map_ytdlp_error("ERROR: Unsupported URL: https://a.b"))


def test_retryable_403() -> None:
    e = ingest.map_ytdlp_error("ERROR: unable to download video data: HTTP Error 403: Forbidden")
    assert ingest.is_retryable_403(e)
    assert not ingest.is_retryable_403(ingest.map_ytdlp_error("ERROR: Private video"))
    assert not ingest.is_retryable_403(WfmError("download_failed", "timeout"))


# --------------------------------------------------------------------------
# info dict helpers
# --------------------------------------------------------------------------
YT_FORMATS = [
    {"format_id": "233", "protocol": "m3u8_native", "vcodec": "none", "acodec": None},
    {"format_id": "249", "protocol": "https", "vcodec": "none", "acodec": "opus", "abr": 47},
    {"format_id": "140", "protocol": "https", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 129},
    {"format_id": "134", "protocol": "https", "vcodec": "avc1.4d400c", "acodec": "none", "height": 240},
]
# measured shape of an X video: split only over HLS, https formats muxed with codecs None
X_FORMATS = [
    {"format_id": "hls-audio-64000-Audio", "protocol": "m3u8_native", "vcodec": "none", "acodec": None},
    {"format_id": "http-632", "protocol": "https", "vcodec": None, "acodec": None},
    {"format_id": "hls-138", "protocol": "m3u8_native", "vcodec": "avc1.4D4015", "acodec": "none"},
    {"format_id": "http-2176", "protocol": "https", "vcodec": None, "acodec": None},
]


def test_split_detection() -> None:
    assert ingest.has_split_formats({"formats": YT_FORMATS})
    assert not ingest.has_split_formats({"formats": X_FORMATS})
    hls_only = [f for f in X_FORMATS if f["protocol"] == "m3u8_native"]
    assert ingest.has_split_formats({"formats": hls_only})
    assert not ingest.has_split_formats({"formats": []})
    assert not ingest.has_split_formats({})
    muxed = [{"protocol": "https", "vcodec": "avc1", "acodec": "mp4a"}]
    assert not ingest.has_split_formats({"formats": muxed})


def test_has_any_video() -> None:
    audio_site = [{"protocol": "https", "vcodec": "none", "acodec": "mp3"}]
    assert not ingest.has_any_video({"formats": audio_site})
    assert ingest.has_any_video({"formats": X_FORMATS})
    assert ingest.has_any_video({"formats": YT_FORMATS})


def test_media_plan() -> None:
    assert ingest.media_plan(True, opts()) == {"audio": "audio", "video": "video_720"}
    assert ingest.media_plan(True, opts(code=True)) == {"audio": "audio", "video": "video_1080"}
    assert ingest.media_plan(False, opts()) == {"audio": "muxed", "video": "muxed"}
    assert ingest.media_plan(True, opts(audio_only=True)) == {"audio": "audio", "video": None}
    assert ingest.media_plan(False, opts(video_only=True)) == {"audio": None, "video": "muxed"}
    assert ingest.media_plan(False, opts(), has_video=False) == {"audio": "audio", "video": None}


def test_trim_meta() -> None:
    info = {"id": "x", "title": "t", "formats": [1], "duration": 19, "extractor_key": "Youtube", "junk": 1}
    m = ingest.trim_meta(info)
    assert m["id"] == "x" and m["duration"] == 19 and m["uploader"] is None
    assert "formats" not in m and "junk" not in m


def test_refusals() -> None:
    assert ingest.refusal({"live_status": "is_live"}, opts()).code == "live_stream"
    assert ingest.refusal({"live_status": "is_upcoming"}, opts()).code == "live_stream"
    assert ingest.refusal({"live_status": "was_live", "duration": 60}, opts()) is None
    assert ingest.refusal({"live_status": "post_live", "duration": 60}, opts()) is None
    pl = {"_type": "playlist", "playlist_count": 246, "entries": [{}]}
    e = ingest.refusal(pl, opts())
    assert e.code == "playlist_refused" and "246" in e.message and "--playlist" in e.hint
    assert ingest.refusal(pl, opts(playlist=3)) is None
    x = {"_type": "playlist", "extractor_key": "Twitter", "entries": [{}, {}]}
    assert "multi-video post with 2" in ingest.refusal(x, opts()).message
    long = ingest.refusal({"duration": 241 * 60}, opts())
    assert long.code == "too_long" and "--from/--to" in long.hint
    assert ingest.refusal({"duration": 241 * 60}, opts(max_minutes=300)) is None
    assert ingest.refusal({"duration": None}, opts()) is None
    # review #7: a range is judged by its own length, not the whole video's
    assert ingest.refusal({"duration": 300 * 60}, opts(from_s=0.0, to_s=3600.0)) is None
    assert ingest.refusal({"duration": 40 * 60}, opts(max_minutes=10, from_s=0.0, to_s=60.0)) is None
    assert ingest.refusal({"duration": 300 * 60}, opts(from_s=17000.0)) is None  # last 20 min
    r = ingest.refusal({"duration": 300 * 60}, opts(from_s=60.0))
    assert r.code == "too_long" and "min range" in r.message and "shorter" in r.hint
    assert ingest.refusal({"duration": 12}, opts(max_minutes=0)).message.startswith("0.2 min")
    assert "not supported while live" in ingest.refusal({"live_status": "is_live"}, opts()).message


def test_meta_args() -> None:
    a = ingest.meta_args(opts())
    assert a[:4] == ["-J", "--no-playlist", "--flat-playlist", "--no-warnings"]
    assert a[-2:] == ["-I", "1:1"]
    b = ingest.meta_args(opts(playlist=3))
    assert "--yes-playlist" in b and "--no-playlist" not in b and b[-1] == "1:3"


def test_download_args() -> None:
    assert ingest.download_args("audio") == ["-S", "lang,proto:https", "-f", "ba[abr<=96]/ba/b"]
    assert ingest.download_args("video_720") == ["-S", "lang,res:720,vcodec:h264,proto:https", "-f", "bv*/b"]
    assert ingest.download_args("video_1080")[1] == "lang,res:1080,vcodec:h264,proto:https"
    assert ingest.download_args("muxed", 1080) == ["-S", "lang,res:1080,vcodec:h264,proto:https",
                                                   "-f", "b/bv*+ba"]
    with pytest.raises(ValueError):
        ingest.download_args("bogus")


def test_playlist_entries() -> None:
    status = "https://x.com/u/status/1600649710662213632"
    info = {"_type": "playlist", "entries": [
        {"id": "a", "url": status, "formats": [{}], "extractor_key": "Twitter", "playlist_index": 1},
        {"id": "b", "url": status + "/video/1", "formats": [{}], "extractor_key": "Twitter", "playlist_index": 2},
        {"_type": "url", "id": "c", "url": "https://www.youtube.com/watch?v=c", "ie_key": "Youtube"},
        {"_type": "url", "id": "d"},  # unusable: no url, no formats
    ]}
    items = ingest._playlist_items(info, 10)
    assert [e["id"] for e in items] == ["a", "b", "c"]
    assert ingest._playlist_items(info, 2) == items[:2]
    assert ingest._entry_input(items[0], status, 1) == status + "/video/1"
    assert ingest._entry_input(items[1], status, 2) == status + "/video/2"
    assert ingest._entry_input(items[2], status, 3) == "https://www.youtube.com/watch?v=c"
    assert asyncio.run(ingest.expand_playlist(info, 10)) == [status, status + "/video/1",
                                                             "https://www.youtube.com/watch?v=c"]


def test_parse_print() -> None:
    out = "noise\n/c/k/media/audio.webm|249\n"
    assert ingest._parse_print(out) == ("/c/k/media/audio.webm", "249")
    assert ingest._parse_print("") is None


def test_clean_partial(tmp_path: Path) -> None:
    kp = cache.KeyPaths(tmp_path, "youtube-x")
    kp.media_dir.mkdir(parents=True)
    for n in ("audio.webm", "audio.m4a", "video_720.mp4", "muxed.mp4"):
        (kp.media_dir / n).write_bytes(b"x")
    ingest.clean_partial(kp, "audio")
    assert sorted(p.name for p in kp.media_dir.iterdir()) == ["muxed.mp4", "video_720.mp4"]
    ingest.clean_partial(cache.KeyPaths(tmp_path, "missing"), "audio")  # no media dir: no-op


# --------------------------------------------------------------------------
# download retry flow with yt-dlp / ffprobe faked
# --------------------------------------------------------------------------
class FakeTools:
    """Stands in for proc.arun: scripted yt-dlp download results, real-shaped ffprobe json."""

    def __init__(self, kp: cache.KeyPaths, results: list[str], duration: float = 19.0) -> None:
        self.kp, self.results, self.duration = kp, list(results), duration
        self.calls: list[list[str]] = []

    async def __call__(self, cmd: list[str], **_: Any) -> proc.ProcResult:
        self.calls.append(cmd)
        if cmd[0] == "ffprobe":
            probe = {"format": {"duration": str(self.duration)},
                     "streams": [{"codec_type": "audio"}, {"codec_type": "video"}]}
            return proc.ProcResult(0, json.dumps(probe), "")
        if "-J" in cmd:
            return proc.ProcResult(0, json.dumps({"id": "x", "extractor_key": "Youtube", "formats": []}), "")
        res = self.results.pop(0)
        if res == "403":
            return proc.ProcResult(1, "", "ERROR: unable to download video data: HTTP Error 403: Forbidden\n")
        if res == "private":
            return proc.ProcResult(1, "", "ERROR: [youtube] x: Private video\n")
        path = self.kp.media_dir / "audio.webm"
        path.write_bytes(b"\0" * 1000)
        return proc.ProcResult(0, f"{path}|249\n", "")


def _setup_dl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, results: list[str],
              duration: float = 19.0) -> tuple[cache.KeyPaths, Resolved, FakeTools]:
    kp = cache.KeyPaths(tmp_path, "youtube-x")
    kp.dir.mkdir(parents=True)
    kp.info_json.write_text("{}")
    fake = FakeTools(kp, results, duration)
    monkeypatch.setattr(proc, "arun", fake)
    monkeypatch.setattr(ingest, "RETRY_BACKOFF_S", (0.0, 0.0))
    monkeypatch.setattr(ingest, "_write_info", lambda kp, info: None)
    monkeypatch.setattr(proc, "which", lambda n: None if n == "uvx" else shutil.which(n))
    r = Resolved(input="https://youtu.be/x", key="youtube-x", meta={"id": "x", "duration": 19.0},
                 info_json=str(kp.info_json), is_local=False, split_formats=True)
    return kp, r, fake


def _dl(kp: cache.KeyPaths, r: Resolved, o: RunOptions | None = None) -> Any:
    return asyncio.run(ingest.download(kp, r, "audio", o or opts(), asyncio.Semaphore(4)))


def test_download_ok_args(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kp, r, fake = _setup_dl(tmp_path, monkeypatch, ["ok"])
    mf = _dl(kp, r, opts(cookies="firefox"))
    assert mf.kind == "audio" and mf.format_id == "249" and mf.path.endswith("audio.webm")
    cmd = fake.calls[0]
    assert cmd[:3] == proc.ytdlp_base()
    for flag in ("--load-info-json", "--no-part", "-N", "--print", "-o"):
        assert flag in cmd
    assert cmd[cmd.index("--cookies-from-browser") + 1] == "firefox"
    assert cmd[cmd.index("-o") + 1] == f"{kp.media_stem('audio')}.%(ext)s"
    assert r.meta["has_audio"] is True


def test_download_403_then_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kp, r, fake = _setup_dl(tmp_path, monkeypatch, ["403", "403", "ok"])
    assert _dl(kp, r).format_id == "249"
    refetches = [c for c in fake.calls if "-J" in c]
    assert len(refetches) == 2
    assert "--no-playlist" in refetches[0] and r.input in refetches[0]


def test_download_403_exhausted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kp, r, _ = _setup_dl(tmp_path, monkeypatch, ["403", "403", "403"])
    with pytest.raises(WfmError) as ei:
        _dl(kp, r)
    assert ei.value.code == "download_failed" and "403" in ei.value.message


def test_download_non_retryable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kp, r, fake = _setup_dl(tmp_path, monkeypatch, ["private"])
    with pytest.raises(WfmError) as ei:
        _dl(kp, r)
    assert ei.value.code == "private"
    assert len([c for c in fake.calls if c[0] != "ffprobe"]) == 1


def test_download_truncated_file_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # --no-part hazard: a file shorter than 0.98 x meta duration is deleted and retried
    kp, r, _ = _setup_dl(tmp_path, monkeypatch, ["ok", "ok", "ok"], duration=10.0)
    with pytest.raises(WfmError) as ei:
        _dl(kp, r)
    assert ei.value.message.startswith("incomplete audio download")
    assert not list(kp.media_dir.glob("audio.*"))


def test_download_cleans_stale_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kp, r, _ = _setup_dl(tmp_path, monkeypatch, ["ok"])
    kp.media_dir.mkdir(parents=True, exist_ok=True)
    stale = kp.media_dir / "audio.m4a"
    stale.write_bytes(b"truncated")
    _dl(kp, r)
    assert not stale.exists()


# --------------------------------------------------------------------------
# resolve (needs the cache implementation)
# --------------------------------------------------------------------------
@needs_cache
def test_resolve_not_a_link(wfm_cache: Path) -> None:
    with pytest.raises(WfmError) as ei:
        asyncio.run(ingest.resolve("not a link", opts(), asyncio.Semaphore(1)))
    assert ei.value.code == "unsupported_url"


@needs_cache
def test_resolve_url_key_and_index_hit(wfm_cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    info = {"id": "jNQXAC9IVRw", "extractor_key": "Youtube", "title": "Me at the zoo", "duration": 19,
            "live_status": "not_live", "webpage_url": "https://www.youtube.com/watch?v=jNQXAC9IVRw",
            "formats": YT_FORMATS}

    async def fake(cmd: list[str], **_: Any) -> proc.ProcResult:
        calls.append(cmd)
        return proc.ProcResult(0, json.dumps(info), "")

    monkeypatch.setattr(proc, "arun", fake)
    url = "https://www.youtube.com/watch?v=jNQXAC9IVRw&t=5s&si=x"
    [r] = asyncio.run(ingest.resolve(url, opts(), asyncio.Semaphore(1)))
    assert r.key == "youtube-jNQXAC9IVRw" and r.split_formats and not r.index_hit
    kp = cache.key_paths(r.key)
    assert json.loads(kp.meta.read_text())["title"] == "Me at the zoo"
    assert kp.info_gz.is_file() and kp.info_json.is_file()
    assert cache.read_json(wfm_cache / "index.json") is not None
    # index hit requires manifest meta done (cli writes it)
    m = cache.new_manifest(r.key, {"input": url})
    cache.set_stage(m, "meta", "done")
    cache.save_manifest(kp, m)
    kp.info_json.unlink()
    [r2] = asyncio.run(ingest.resolve("https://youtube.com/watch?v=jNQXAC9IVRw", opts(), asyncio.Semaphore(1)))
    assert r2.index_hit and r2.key == r.key and len(calls) == 1
    assert kp.info_json.is_file()  # re-inflated: downloads not done yet
    [r3] = asyncio.run(ingest.resolve(url, opts(fresh=True), asyncio.Semaphore(1)))
    assert not r3.index_hit and len(calls) == 2


@needs_cache
def test_resolve_playlist_embedded_entries(wfm_cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    status = "https://x.com/u/status/1600649710662213632"
    entry = {"url": status, "extractor_key": "Twitter", "duration": 30, "formats": X_FORMATS}
    info = {"_type": "playlist", "extractor_key": "Twitter", "id": "1600649710662213632", "playlist_count": 2,
            "entries": [{**entry, "id": "111", "playlist_index": 1}, {**entry, "id": "222", "playlist_index": 2}]}

    async def fake(cmd: list[str], **_: Any) -> proc.ProcResult:
        return proc.ProcResult(0, json.dumps(info), "")

    monkeypatch.setattr(proc, "arun", fake)
    with pytest.raises(WfmError) as ei:
        asyncio.run(ingest.resolve(status, opts(), asyncio.Semaphore(1)))
    assert ei.value.code == "playlist_refused"
    items = asyncio.run(ingest.resolve(status, opts(playlist=2), asyncio.Semaphore(1)))
    assert [r.key for r in items] == ["twitter-111", "twitter-222"]
    assert [r.input for r in items] == [status + "/video/1", status + "/video/2"]
    assert all(r.parent_input == status and not r.split_formats for r in items)


@needs_cache
@needs_ffmpeg
def test_resolve_local_file(wfm_cache: Path, tmp_path: Path) -> None:
    f = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=2",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-shortest", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", str(f)], check=True)
    [r] = asyncio.run(ingest.resolve(str(f), opts(), asyncio.Semaphore(1)))
    assert r.is_local and r.key.startswith("local-") and r.info_json is None
    assert r.meta["has_audio"] and r.meta["has_video"] and r.meta["width"] == 320
    assert r.meta["title"] == "clip.mp4" and abs(r.meta["duration"] - 2.0) < 0.2
    moved = tmp_path / "renamed.mp4"
    f.rename(moved)
    [r2] = asyncio.run(ingest.resolve(str(moved), opts(), asyncio.Semaphore(1)))
    assert r2.key == r.key  # stable across runs and renames
    mf = ingest.local_media(r2)
    assert mf.kind == "local" and mf.path == str(moved)


@needs_cache
@needs_ffmpeg
def test_resolve_local_not_media(wfm_cache: Path, tmp_path: Path) -> None:
    f = tmp_path / "notes.txt"
    f.write_text("hello")
    with pytest.raises(WfmError) as ei:
        asyncio.run(ingest.resolve(str(f), opts(), asyncio.Semaphore(1)))
    assert ei.value.code == "unsupported_url" and "not a media file" in ei.value.message
