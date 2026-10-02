"""Cache keys (URL, local), index hits, params_hash invalidation, manifest io, locks (incl. the
no-fcntl fallback), LRU eviction never touching protected / in-use keys (spec 3 "Cache layout",
spec 9 unit list)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import SCRIPTS_DIR
from wfm import cache


# --------------------------------------------------------------------------
# keys / tags
# --------------------------------------------------------------------------
def test_key_for_info_keeps_id_case() -> None:
    assert cache.key_for_info("Youtube", "zjkBMFhNj_g") == "youtube-zjkBMFhNj_g"
    assert cache.key_for_info("Twitter", "1577855447914409984") == "twitter-1577855447914409984"
    assert cache.key_for_info("Generic", "a/b c.mp4") == "generic-a_b_c_mp4"
    assert cache.valid_key("youtube-zjkBMFhNj_g") and not cache.valid_key("../x") and not cache.valid_key("")


def test_key_for_local_stable_across_rename(tmp_path: Path) -> None:
    a = tmp_path / "a.mp4"
    a.write_bytes(b"x" * 5000)
    k1 = cache.key_for_local(a)
    b = tmp_path / "b.mp4"
    a.rename(b)
    assert cache.key_for_local(b) == k1 and k1.startswith("local-") and len(k1) == 6 + 16
    b.write_bytes(b"x" * 5001)
    assert cache.key_for_local(b) != k1


def test_normalize_input_urls() -> None:
    n = cache.normalize_input
    base = n("https://www.youtube.com/watch?v=jNQXAC9IVRw")
    assert base == "https://youtube.com/watch?v=jNQXAC9IVRw"
    # spec E2E #21: tracking/start params dropped, same key as #3
    assert n("https://www.youtube.com/watch?v=jNQXAC9IVRw&t=5s&si=x") == base
    assert n("HTTPS://M.YouTube.com/watch?utm_source=a&v=jNQXAC9IVRw#frag") == base
    assert n("https://x.com/a/status/1/") == "https://x.com/a/status/1"
    assert n("https://site.test/p?b=2&a=1") == "https://site.test/p?a=1&b=2"
    # YouTube short forms share the canonical entry
    assert n("https://youtu.be/jNQXAC9IVRw?si=abc&t=3") == base
    assert n("https://www.youtube.com/shorts/jNQXAC9IVRw") == base
    assert n("https://youtube.com/embed/jNQXAC9IVRw/") == base
    assert n("https://youtu.be/jNQXAC9IVRw?list=PL1") == "https://youtube.com/watch?list=PL1&v=jNQXAC9IVRw"
    # ids stay case-sensitive
    assert n("https://youtu.be/AbCdEfGhIjK") != n("https://youtu.be/abcdefghijk")
    assert n("https://youtu.be/AbC") != n("https://youtu.be/abc")


def test_normalize_input_local(tmp_path: Path) -> None:
    f = tmp_path / "v.mp4"
    f.write_bytes(b"1")
    n1 = cache.normalize_input(str(f))
    assert n1.startswith("file:" + str(f.resolve()) + "#")
    os.utime(f, ns=(1, 2_000_000_000))
    assert cache.normalize_input(str(f)) != n1  # mtime is part of it
    with pytest.raises(FileNotFoundError):
        cache.normalize_input(str(tmp_path / "missing.mp4"))
    assert cache.is_url("HTTP://x") and not cache.is_url("ftp://x") and not cache.is_url("/tmp/x")


def test_tags() -> None:
    assert cache.range_tag(None, None, 3588) == "full"
    assert cache.range_tag(0, 9999, 3588) == "full"
    assert cache.range_tag(720, 1200, 3588) == "r720000-1200000"
    assert cache.range_tag(1.5, None, 100) == "r1500-100000"
    assert cache.range_tag(None, 60, 100) == "r0-60000"
    assert cache.range_tag(None, None, None) == "full"
    assert cache.range_tag(5, None, None) == "r5000-end"
    assert cache.transcript_tag("full", None) == "full"
    assert cache.transcript_tag("full", "fr") == "full-lang_fr"
    assert cache.view_tag("r0-60000", 1080) == "r0-60000-1080"


def test_params_hash_is_order_independent_and_sensitive() -> None:
    a = cache.params_hash({"x": 1, "y": [1, 2]})
    assert a == cache.params_hash({"y": [1, 2], "x": 1})
    assert a != cache.params_hash({"x": 2, "y": [1, 2]})
    assert len(a) == 40


# --------------------------------------------------------------------------
# manifest / index
# --------------------------------------------------------------------------
def test_manifest_roundtrip_and_stage_freshness(wfm_cache: Path) -> None:
    kp = cache.key_paths("youtube-abc")
    assert kp.dir == wfm_cache / "youtube-abc"
    assert cache.load_manifest(kp) is None
    m = cache.new_manifest("youtube-abc", {"input": "u", "webpage_url": "w", "extractor": "Youtube", "id": "abc"})
    assert m["v"] == 1 and m["skill_version"] == __import__("wfm").VERSION and m["source"]["local_path"] is None
    cache.set_stage(m, "transcripts/full", "running", params_hash="h1")
    assert m["stages"]["transcripts/full"]["status"] == "running"
    assert not cache.stage_fresh(m, "transcripts/full")
    cache.set_stage(m, "transcripts/full", "done", params_hash="h1")
    st = m["stages"]["transcripts/full"]
    assert st["t_end"] >= st["t_start"]
    assert cache.stage_fresh(m, "transcripts/full")
    assert cache.stage_fresh(m, "transcripts/full", "h1")
    assert not cache.stage_fresh(m, "transcripts/full", "h2")  # params changed -> recompute
    assert not cache.stage_fresh(None, "meta")
    for i in range(25):
        cache.add_error(m, "meta", {"code": "download_failed", "message": str(i), "hint": None})
    assert len(m["errors"]) == 20 and m["errors"][-1]["message"] == "24"
    cache.save_manifest(kp, m)
    back = cache.load_manifest(kp)
    assert back is not None and back["stages"] == m["stages"]
    assert not list(kp.dir.glob("*.tmp.*"))  # atomic write left no temp file
    kp.manifest.write_text("{corrupt")
    assert cache.load_manifest(kp) is None


def test_index_hit_requires_meta_done(wfm_cache: Path) -> None:
    norm = cache.normalize_input("https://youtu.be/abc")
    cache.index_put(norm, "youtube-abc")
    assert cache.index_get(norm) is None  # no manifest yet
    kp = cache.key_paths("youtube-abc")
    m = cache.new_manifest("youtube-abc", {})
    cache.save_manifest(kp, m)
    assert cache.index_get(norm) is None  # meta not done
    cache.set_stage(m, "meta", "done")
    cache.save_manifest(kp, m)
    assert cache.index_get(norm) == "youtube-abc"
    data = json.loads((wfm_cache / "index.json").read_text())
    assert data == {"v": 1, "inputs": {norm: "youtube-abc"}}
    cache.index_drop_keys({"youtube-abc"})
    assert cache.index_get(norm) is None


def test_read_json_defaults(tmp_path: Path) -> None:
    assert cache.read_json(tmp_path / "nope", {"d": 1}) == {"d": 1}
    (tmp_path / "bad").write_text("[")
    assert cache.read_json(tmp_path / "bad") is None


# --------------------------------------------------------------------------
# locks
# --------------------------------------------------------------------------
def test_filelock_exclusive_within_process(tmp_path: Path) -> None:
    p = tmp_path / "k" / "run.lock"
    a, b = cache.FileLock(p), cache.FileLock(p)
    assert a.acquire(blocking=False)
    assert b.is_held_elsewhere()
    assert not b.acquire(blocking=False)
    t = time.monotonic()
    assert not b.acquire(timeout=0.5)
    assert time.monotonic() - t >= 0.4
    a.release()
    a.release()  # idempotent
    assert not b.is_held_elsewhere()
    with b:
        assert a.is_held_elsewhere()
    assert a.acquire(blocking=False)
    with pytest.raises(RuntimeError):
        a.acquire()
    a.release()


def test_filelock_across_processes(tmp_path: Path) -> None:
    p = tmp_path / "asr.lock"
    code = (f"import sys, time; sys.path.insert(0, {str(SCRIPTS_DIR)!r}); from wfm.cache import FileLock; "
            f"l = FileLock({str(p)!r}); l.acquire(); print('held', flush=True); time.sleep(30)")
    child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "held"
        lk = cache.FileLock(p)
        assert lk.is_held_elsewhere() and not lk.acquire(blocking=False)
        child.kill()
        child.wait()
        assert lk.acquire(timeout=5)  # the kernel dropped the dead holder's lock
        lk.release()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_lock_fallback_without_fcntl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows-safe path (spec 3 review fix): no fcntl, no msvcrt -> O_EXCL sidecar."""
    monkeypatch.setitem(sys.modules, "fcntl", None)
    monkeypatch.setitem(sys.modules, "msvcrt", None)
    assert cache._lock_backend() == "excl"
    p = tmp_path / "run.lock"
    a, b = cache.FileLock(p), cache.FileLock(p)
    assert a.acquire(blocking=False)
    assert (tmp_path / "run.lock.excl").read_text() == str(os.getpid())
    assert b.is_held_elsewhere() and not b.acquire(blocking=False)
    a.release()
    assert not (tmp_path / "run.lock.excl").exists()
    assert b.acquire(blocking=False)
    b.release()
    # stale sidecar from a dead process is taken over
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    (tmp_path / "run.lock.excl").write_text(str(dead.pid))
    assert a.acquire(blocking=False)
    a.release()


def test_cache_module_imports_without_fcntl() -> None:
    code = (f"import sys; sys.modules['fcntl'] = None; sys.modules['msvcrt'] = None; "
            f"sys.path.insert(0, {str(SCRIPTS_DIR)!r}); import wfm.cache as c; print(c._lock_backend())")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "excl"


# --------------------------------------------------------------------------
# LRU / clear / runs
# --------------------------------------------------------------------------
def _entry(root: Path, key: str, mb: int, age_s: float, norm: str | None = None) -> None:
    kp = cache.key_paths(key, root)
    (kp.media_dir).mkdir(parents=True)
    (kp.media_dir / "audio.m4a").write_bytes(b"\0" * (mb * 1_000_000))
    m = cache.new_manifest(key, {})
    cache.set_stage(m, "meta", "done")
    cache.atomic_write_json(kp.manifest, m)
    ts = time.time() - age_s
    m["last_used"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
    cache.atomic_write_json(kp.manifest, m)
    (kp.meta).write_text(json.dumps({"title": f"title {key}"}))
    if norm:
        cache.index_put(norm, key, root)


def test_lru_evicts_oldest_but_never_protected_or_busy(wfm_cache: Path) -> None:
    _entry(wfm_cache, "k-old", 3, 4000, "https://a.test/old")
    _entry(wfm_cache, "k-older", 3, 5000)
    _entry(wfm_cache, "k-mid", 3, 3000)
    _entry(wfm_cache, "k-new", 3, 10)
    (wfm_cache / "runs" / "wfmx").mkdir(parents=True)  # never counted / deleted
    entries = cache.list_entries()
    assert [e.key for e in entries] == ["k-new", "k-mid", "k-old", "k-older"]
    assert entries[0].title == "title k-new" and entries[0].bytes >= 3_000_000
    busy = cache.FileLock(wfm_cache / "k-mid" / "run.lock")
    assert busy.acquire(blocking=False)
    try:
        # 12 MB total, cap 4 MB: k-older is protected (current run), k-mid is busy
        deleted = cache.evict_lru(protect={"k-older"}, limit_bytes=4_000_000)
    finally:
        busy.release()
    assert deleted == ["k-old", "k-new"]
    assert (wfm_cache / "k-older").is_dir() and (wfm_cache / "k-mid").is_dir()
    assert not (wfm_cache / "k-old").exists() and (wfm_cache / "runs" / "wfmx").is_dir()
    assert cache.index_get("https://a.test/old") is None
    assert "https://a.test/old" not in json.loads((wfm_cache / "index.json").read_text())["inputs"]
    assert cache.evict_lru(set(), limit_bytes=10**12) == []


def test_max_bytes_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WFM_CACHE_MAX_GB", raising=False)
    assert cache.max_bytes() == 10_000_000_000
    monkeypatch.setenv("WFM_CACHE_MAX_GB", "0.5")
    assert cache.max_bytes() == 500_000_000
    monkeypatch.setenv("WFM_CACHE_MAX_GB", "junk")
    assert cache.max_bytes() == 10_000_000_000


def test_clear(wfm_cache: Path) -> None:
    _entry(wfm_cache, "k1", 1, 10, "https://a.test/1")
    _entry(wfm_cache, "k2", 1, 10)
    with pytest.raises(KeyError):
        cache.clear("nope")
    assert cache.clear("k1") == ["k1"]
    assert cache.index_get("https://a.test/1") is None
    cache.write_run_json("wfmold", {"pid": None, "state": "done"})
    assert cache.clear(all_=True) == ["k2"]
    assert not (wfm_cache / "runs" / "wfmold").exists()


def test_runs_dir_and_prune(wfm_cache: Path) -> None:
    rid = cache.new_run_id()
    assert rid.startswith("wfm") and len(rid) == 9 and cache.valid_run_id(rid)
    assert not cache.valid_run_id("../x") and not cache.valid_run_id("") and not cache.valid_run_id("a" * 65)
    with pytest.raises(ValueError):
        cache.run_dir("../x")
    cache.write_run_json("wfmold1", {"pid": None, "started_ts": time.time() - 8 * 86400})
    cache.write_run_json("wfmnew1", {"pid": None, "started_ts": time.time()})
    cache.write_run_json("wfmlive", {"pid": os.getpid(), "started_ts": time.time() - 8 * 86400})
    assert cache.read_run_json("wfmnew1")["updated"].endswith("Z")
    assert cache.prune_runs() == ["wfmold1"]
    assert cache.read_run_json("wfmlive") is not None


def test_pid_alive() -> None:
    assert cache.pid_alive(os.getpid())
    assert not cache.pid_alive(None) and not cache.pid_alive(0)
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    assert not cache.pid_alive(p.pid)


def test_cache_root_env_and_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WFM_CACHE_DIR", str(tmp_path / "a" / "b"))
    assert cache.cache_root() == (tmp_path / "a" / "b").resolve() and (tmp_path / "a" / "b").is_dir()
    monkeypatch.delenv("WFM_CACHE_DIR")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    root = cache.cache_root()
    if sys.platform == "darwin":
        assert root == (tmp_path / "home" / "Library" / "Caches" / "watch-for-me").resolve()
    elif sys.platform.startswith("linux"):
        assert root == (tmp_path / "xdg" / "watch-for-me").resolve()
