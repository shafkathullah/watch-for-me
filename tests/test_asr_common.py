"""ASR shared code (asr_common) + the worker driver (wfm.asr_client), no models, no network.

A fake Engine (tone frequency -> language) stands in for MLX / onnx: the real worker loop,
JSONL protocol, ffmpeg PCM decode, silence cuts, LID splitting, routing, filters and the
asr.lock handshake all run for real. Tests that decode audio need ffmpeg on PATH.
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
import wave
from itertools import pairwise
from pathlib import Path
from typing import Any

import asr_common as ac
import numpy as np
import pytest
from conftest import SCRIPTS_DIR
from wfm import asr_client as asrc
from wfm.cache import FileLock, KeyPaths
from wfm.types import AsrJob, WfmError

SR = ac.SR
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")
LANG_HZ = {"en": 300.0, "fr": 700.0, "es": 1100.0}  # fake LID: dominant tone frequency


# --------------------------------------------------------------------------
# synthetic audio
# --------------------------------------------------------------------------
def voiced(seconds: float, hz: float = 300.0, amp: float = 0.3, seed: int = 0) -> np.ndarray:
    """'Speech': a tone with 4 Hz syllable modulation plus light noise (never silent)."""
    t = np.arange(int(seconds * SR)) / SR
    rng = np.random.default_rng(seed)
    env = 0.6 + 0.4 * np.sin(2 * np.pi * 4 * t)
    return (amp * env * np.sin(2 * np.pi * hz * t) + 0.03 * amp * rng.standard_normal(len(t))).astype(np.float32)


def quiet(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * SR), dtype=np.float32)


def speech_with_pauses(total: float, pauses: list[float], hz: float = 300.0) -> np.ndarray:
    """`total` s of voiced audio with a 0.6 s pause centred on each time in `pauses`."""
    x = voiced(total, hz)
    for p in pauses:
        x[int((p - 0.3) * SR):int((p + 0.3) * SR)] = 0.0
    return x


def write_wav(path: Path, pcm: np.ndarray) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(pcm, -1, 1) * 32767).astype("<i2").tobytes())
    return path


def fake_detect(x: np.ndarray) -> tuple[str, float]:
    spec = np.abs(np.fft.rfft(x[: 30 * SR]))
    hz = float(np.argmax(spec[1:]) + 1) * SR / (2 * (len(spec) - 1))
    lang = min(LANG_HZ, key=lambda k: abs(LANG_HZ[k] - hz))
    return lang, 0.99


class FakeEngine:
    """In-process Engine: LID from the tone, one segment per chunk naming engine + lang."""

    backend = "fake"

    def __init__(self, lid_p: float | None = None) -> None:
        self.ids = {"parakeet": "fake/parakeet", "whisper": "fake/whisper", "lid": "fake/lid"}
        self.loaded: list[str] = []
        self.lid_p = lid_p
        self.whisper_segs: list[dict[str, Any]] | None = None

    def versions(self) -> dict[str, str]:
        return {"fake": "1"}

    def ensure(self, role: str, emit_fn: Any) -> None:
        if role not in self.loaded:
            self.loaded.append(role)

    def detect_language(self, x: np.ndarray) -> tuple[str, float]:
        lang, p = fake_detect(x)
        return lang, self.lid_p if self.lid_p is not None else p

    def parakeet(self, x: np.ndarray) -> list[dict[str, Any]]:
        return [{"t0": 0.1, "t1": len(x) / SR - 0.1, "text": " parakeet words "}]

    def whisper(self, x: np.ndarray, lang: str) -> list[dict[str, Any]]:
        if self.whisper_segs is not None:
            return self.whisper_segs
        return [{"t0": 0.1, "t1": len(x) / SR - 0.1, "text": f"whisper {lang} " + "mot " * int(len(x) / SR * 2),
                 "avg_logprob": -0.2, "compression_ratio": 1.3, "no_speech_prob": 0.01}]


# --------------------------------------------------------------------------
# protocol codec + model helpers
# --------------------------------------------------------------------------
def test_encode_decode_roundtrip() -> None:
    msg = {"ev": "chunk", "text": "é ü 字幕", "t0": 1.5, "segments": []}
    line = ac.encode(msg)
    assert "\n" not in line and "字幕" in line
    assert ac.decode(line) == msg
    assert ac.decode(line.encode() + b"\n") == msg
    for junk in ("", "   ", "hello", "[1,2]", "{broken", "Warning: foo"):
        assert ac.decode(junk) is None


def test_model_ids_env_override() -> None:
    assert ac.model_ids("mlx", {}) == ac.DEFAULT_MODELS["mlx"]
    ids = ac.model_ids("cpu", {"WFM_WHISPER_MODEL": "my/turbo"})
    assert ids["whisper"] == "my/turbo"
    assert ids["lid"] == "Systran/faster-whisper-tiny"
    # spec 4.5: the redirecting alias is pinned to its target repo
    assert ac.DEFAULT_MODELS["cpu"]["whisper"] == "dropbox-dash/faster-whisper-large-v3-turbo"


def test_hf_cache_helpers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    repo = "mlx-community/whisper-tiny-mlx"
    assert not ac.hf_repo_cached(repo)
    assert ac.hf_local_snapshot(repo) is None
    pin = ac.MODEL_REVISIONS[repo]
    other = tmp_path / "models--mlx-community--whisper-tiny-mlx" / "snapshots" / "deadbeef"
    other.mkdir(parents=True)
    (other / "config.json").write_text("{}")
    assert ac.hf_repo_cached(repo)
    assert ac.hf_local_snapshot(repo) is None  # pinned repo: only the pinned snapshot counts
    snap = other.parent / pin
    snap.mkdir()
    (snap / "config.json").write_text("{}")
    assert ac.hf_local_snapshot(repo) == snap
    # alias -> repo; local dir -> itself
    assert ac.hf_repo_id("nemo-parakeet-tdt-0.6b-v3") == "istupakov/parakeet-tdt-0.6b-v3-onnx"
    assert ac.hf_repo_id(str(tmp_path)) is None
    assert ac.hf_local_snapshot(str(tmp_path)) == tmp_path
    assert ac.model_size_mb("tiny") == 76


def test_load_model_with_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    model = "some-org/some-model"
    base = tmp_path / "models--some-org--some-model"
    events: list[dict[str, Any]] = []

    def download() -> Path:
        (base / "blobs").mkdir(parents=True)
        for _ in range(4):
            with open(base / "blobs" / "abc.incomplete", "ab") as f:
                f.write(b"x" * 500_000)
            time.sleep(0.05)
        snap = base / "snapshots" / "rev"
        snap.mkdir(parents=True)
        return snap

    got = ac.load_model_with_events(model, lambda p: ("loaded", p), events.append, download=download, every=0.04)
    assert got == ("loaded", base / "snapshots" / "rev")
    kinds = [e["status"] for e in events]
    assert kinds[0] == "downloading" and kinds[-1] == "done"
    prog = [e for e in events if e["status"] == "progress"]
    assert prog and all("got_mb" in e and "s" in e for e in prog)
    assert events[-1]["mb"] == 2.0

    # cached now (refs-less, unpinned: newest snapshot) -> no events, load gets the snapshot
    (base / "snapshots" / "rev" / "f").write_text("1")
    events.clear()
    assert ac.load_model_with_events(model, lambda p: p, events.append, download=download) == base / "snapshots" / "rev"
    assert events == []

    def full() -> Path:
        raise OSError(errno.ENOSPC, "No space left on device")

    def broken() -> Path:
        raise RuntimeError("HTTP 500")

    with pytest.raises(ac.AsrError) as ei:
        ac.load_model_with_events("x/disk", lambda p: p, events.append, download=full)
    assert ei.value.code == "disk_full"
    with pytest.raises(ac.AsrError) as ei:
        ac.load_model_with_events("x/net", lambda p: p, events.append, download=broken)
    assert ei.value.code == "model_download_failed"


# --------------------------------------------------------------------------
# DSP: rms, silence cuts, LID grid
# --------------------------------------------------------------------------
def test_frame_rms_matches_reference_convolution() -> None:
    x = np.random.default_rng(1).standard_normal(SR * 3 + 123).astype(np.float32) * 0.1
    ref = np.sqrt(np.convolve(x.astype(np.float64) ** 2, np.ones(480) / 480, "same")[::240] + 1e-12)
    got = ac.frame_rms(x)
    assert len(got) == len(ref)
    assert np.allclose(got[1:-1], ref[1:-1], rtol=1e-3)
    assert len(ac.frame_rms(np.zeros(0, dtype=np.float32))) == 0


def test_silence_cuts_at_quietest_point_inside_window() -> None:
    pauses = [52.0, 118.0, 171.0]
    x = speech_with_pauses(200.0, pauses)
    cuts = ac.silence_cuts(x)
    assert cuts[0] == 0.0 and cuts[-1] == pytest.approx(200.0)
    assert len(cuts) == 5
    for got, want in zip(cuts[1:-1], pauses):
        assert abs(got - want) <= 0.3
    prev = 0.0
    for c in cuts[1:-1]:  # each cut inside [prev+T-20, prev+T+10]
        assert prev + 40 - 0.05 <= c <= prev + 70 + 0.05
        prev = c


def test_silence_cuts_remainder_rule() -> None:
    assert ac.silence_cuts(voiced(70.0)) == [0.0, pytest.approx(70.0)]  # <= T+10: one chunk
    assert ac.silence_cuts(voiced(5.0)) == [0.0, pytest.approx(5.0)]
    cuts = ac.silence_cuts(voiced(71.0))
    assert len(cuts) == 3  # > T+10: one cut, last chunk absorbs the remainder
    cuts = ac.silence_cuts(voiced(600.0))
    assert all(b - a <= 70.0 + 1e-6 for a, b in pairwise(cuts))
    assert cuts[-1] - cuts[-2] <= 70.0


def test_lid_grid() -> None:
    assert ac.lid_grid(0) == []
    assert ac.lid_grid(20) == [(0.0, 20)]
    assert ac.lid_grid(65) == [(0.0, 30.0), (30.0, 60.0), (60.0, 65)]
    assert ac.lid_grid(63) == [(0.0, 30.0), (30.0, 63)]  # < 5 s tail merged


def test_is_silent() -> None:
    assert ac.is_silent(quiet(10))
    assert ac.is_silent(voiced(10, amp=0.001))  # ~-60 dBFS
    assert not ac.is_silent(voiced(10))
    x = quiet(10)
    x[: int(1.0 * SR)] = voiced(1.0)  # 10% voiced -> not silent
    assert not ac.is_silent(x)


# --------------------------------------------------------------------------
# language-switch splitting (pure)
# --------------------------------------------------------------------------
W = ac.LangWindow


def test_split_by_language_switch_inside_chunk_coarse() -> None:
    x = speech_with_pauses(70.0, [31.0])
    rms = ac.frame_rms(x)
    wins = [W(0, 30, "en", 0.99), W(30, 60, "fr", 0.99), W(60, 70, "fr", 0.98)]
    plans = ac.split_by_language([0.0, 70.0], wins, rms)
    assert [p.lang for p in plans] == ["en", "fr"]
    assert abs(plans[0].t1 - 31.0) <= 0.3 and plans[1].t0 == plans[0].t1


def test_split_by_language_switch_at_existing_bound_and_majority() -> None:
    rms = ac.frame_rms(voiced(120.0))
    wins = [W(0, 30, "en", 0.99), W(30, 60, "en", 0.99), W(60, 90, "fr", 0.99), W(90, 120, "fr", 0.97)]
    plans = ac.split_by_language([0.0, 61.0, 120.0], wins, rms)
    assert [(p.t0, p.t1, p.lang) for p in plans] == [(0.0, 61.0, "en"), (61.0, 120.0, "fr")]
    # low-confidence disagreement does not split on the coarse path
    wins2 = [W(0, 30, "en", 0.99), W(30, 60, "fr", 0.5)]
    assert len(ac.split_by_language([0.0, 60.0], wins2, ac.frame_rms(voiced(60.0)))) == 1


def test_split_by_language_exact_switches_and_labels() -> None:
    rms = ac.frame_rms(voiced(60.0))
    wins = [W(0, 30, "en", 0.9), W(30, 60, "es", 0.9)]
    fine = [W(20, 30, "es", 0.99), W(10, 20, "en", 0.99)]
    plans = ac.split_by_language([0.0, 60.0], wins, rms, switches=[20.4, 59.8])
    assert [(p.t0, p.t1, p.lang) for p in plans] == [(0.0, 20.4, "en"), (20.4, 60.0, "es")]
    # silent-only chunk -> nearest voiced window's language; nothing voiced -> "und"
    plans = ac.split_by_language([0.0, 30.0, 60.0], [W(0, 30, "", 0.0), W(30, 60, "fr", 0.99)], rms)
    assert [p.lang for p in plans] == ["fr", "fr"]
    assert ac.split_by_language([0.0, 10.0], [W(0, 10, "", 0.0)], rms)[0].lang == "und"
    assert fine  # (fine windows used by plan_chunks; covered end to end below)


def test_switch_helpers() -> None:
    wins = [W(0, 30, "en", 0.99), W(30, 60, "", 0.0), W(60, 90, "fr", 0.69), W(90, 120, "fr", 0.99)]
    assert [(a.t0, b.t0) for a, b in ac.find_switches(wins)] == [(0, 60)]  # silent window skipped
    assert ac.find_switches(wins, both=True) == []
    assert ac.refine_regions(wins) == [(0, 90)]
    fine = [W(0, 10, "en", 0.99), W(5, 15, "en", 0.95), W(10, 20, "fr", 0.5), W(15, 25, "fr", 0.99)]
    assert ac.fine_switches(fine) == [(10.0, 20.0, "en", "fr")]
    assert ac.fine_grid(0, 22) == [(0, 10), (5, 15), (10, 20), (15, 22)]
    pieces = [W(0, 3, "en", 0.9), W(3, 6, "en", 0.8), W(6, 6.5, "fr", 0.9), W(6.5, 9, "fr", 0.9), W(9, 12, "fr", 0.7)]
    assert ac.pick_switch(pieces, "en", "fr") == 6.0
    assert ac.pick_switch(pieces, "fr", "en") is None
    x = speech_with_pauses(20.0, [5.0, 12.0])
    pts = ac.pause_points(ac.frame_rms(x), ac.RMS_HOP / SR, 0, 20)
    assert [round(t) for t, _ in pts] == [5, 12]


# --------------------------------------------------------------------------
# routing + hallucination filters
# --------------------------------------------------------------------------
def test_route_engine() -> None:
    P, Wh = ac.ENGINE_PARAKEET, ac.ENGINE_WHISPER
    assert ac.route_engine("en", 0.99, False) == P
    assert ac.route_engine("fr", 0.99, False) == Wh
    assert ac.route_engine("fr", 0.52, False) == P  # music guard: LID p < 0.7 -> parakeet
    assert ac.route_engine("fr", 0.1, True) == Wh  # --lang forced: no guard
    assert ac.route_engine("en", 1.0, True) == P


def test_filter_whisper_segments() -> None:
    def seg(text: str, lp: float = -0.2, cr: float = 1.4, ns: float = 0.0, t0: float = 0.0, t1: float = 3.0) -> dict:
        return {"t0": t0, "t1": t1, "text": text, "avg_logprob": lp, "compression_ratio": cr, "no_speech_prob": ns}

    keep = [seg("Bonjour à tous, aujourd'hui on parle de cuisine."), seg("Merci."), {"t0": 0, "t1": 2, "text": "ok"}]
    drop = [
        seg(" Aaすごい cierto", lp=-1.4),  # spec synthetic-music garbage
        seg("owoowoowoowoowoowoowoowoo", cr=3.1),
        seg("Musique"), seg("[Music]"), seg("BGM"), seg("♪ ♪"), seg("..."),
        seg("Sous-titrage ST' 501"), seg("Sous-titres réalisés para la communauté d'Amara.org"),
        seg("ご視聴ありがとうございました"), seg("チャンネル登録よろしくお願いします"),
        seg("Thank you.", t0=0.0, t1=30.0),  # 2 words over 30 s
    ]
    assert ac.filter_whisper_segments(keep + drop) == keep


def test_count_words_cjk() -> None:
    assert ac.count_words("hello  world ") == 2
    assert ac.count_words("") == 0
    # ja without spaces: 0.75 word per Han/kana char, not 1 word per sentence
    assert ac.count_words("ググって出てこないようなものであれば") == round(18 * 0.75)
    assert ac.count_words("その windows だと") == 1 + round(4 * 0.75)
    assert ac.count_words("誰が決めたんですか。") == round(9 * 0.75)  # "。" is not a word


def test_filter_whisper_keeps_long_cjk_segments() -> None:
    """Regression (#9, Japanese talk IcQwLGDzmVQ): real ~10 s Whisper segments without
    spaces were 1-2 "words" -> < 0.3 words/s -> dropped as hallucinations."""
    ja = {"t0": 348.9, "t1": 358.7, "text": "独学専門学校国公立大学私立大学のどの道を選ぶべきでしょうか国公立大学 "
          "家庭は裕福な方なのでお金の心配はないですよくさんの意見を聞かせてください",
          "avg_logprob": -0.3, "compression_ratio": 1.2, "no_speech_prob": 0.0}
    zh = {"t0": 0.0, "t1": 12.0, "text": "我们今天来讨论一下这个问题的几个方面以及它对未来的影响",
          "avg_logprob": -0.3, "compression_ratio": 1.2, "no_speech_prob": 0.0}
    credit = {"t0": 0.0, "t1": 3.0, "text": "ご視聴ありがとうございました", "avg_logprob": -0.1}
    assert ac.filter_whisper_segments([ja, zh, credit]) == [ja, zh]


# --------------------------------------------------------------------------
# decode + process_job end to end (fake engine, real ffmpeg)
# --------------------------------------------------------------------------
@needs_ffmpeg
def test_decode_pcm_range_and_no_audio(tmp_path: Path) -> None:
    wav = write_wav(tmp_path / "a.wav", voiced(12.0))
    assert len(ac.decode_pcm(str(wav))) == pytest.approx(12 * SR, abs=10)
    assert len(ac.decode_pcm(str(wav), 2.0, 5.0)) == pytest.approx(3 * SR, abs=200)
    vid = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "testsrc=d=1:s=64x48",
                    "-c:v", "mpeg4", str(vid)], check=True)
    with pytest.raises(ac.AsrError) as ei:
        ac.decode_pcm(str(vid))
    assert ei.value.code == "no_audio"
    with pytest.raises(ac.AsrError) as ei:
        ac.decode_pcm(str(tmp_path / "missing.wav"))
    assert ei.value.code == "asr_failed"


def mixed_file(tmp_path: Path) -> Path:
    """0-40 en, 40-75 fr (pause at 40), 75-95 silence, 95-130 en (pauses at 110)."""
    en1 = speech_with_pauses(40.0, [20.0], LANG_HZ["en"])
    fr = speech_with_pauses(35.0, [15.0], LANG_HZ["fr"])
    en2 = speech_with_pauses(35.0, [15.0], LANG_HZ["en"])
    x = np.concatenate([en1, quiet(0.6), fr, quiet(20.0), en2])
    return write_wav(tmp_path / "mixed.wav", x)


@needs_ffmpeg
def test_process_job_routes_splits_and_skips_silence(tmp_path: Path) -> None:
    eng = FakeEngine()
    evs: list[dict[str, Any]] = []
    job = {"op": "job", "job_id": "j1", "key": "k", "audio": str(mixed_file(tmp_path)), "from": None, "to": None,
           "lang": None}
    ac.process_job(job, eng, evs.append)
    chunks = [e for e in evs if e["ev"] == "chunk"]
    done = evs[-1]
    assert done["ev"] == "done" and done["job_id"] == "j1" and done["audio_s"] == pytest.approx(130.6, abs=0.1)
    assert chunks[0]["t0"] == 0.0 and chunks[-1]["t1"] == pytest.approx(130.6, abs=0.1)
    for a, b in pairwise(chunks):
        assert a["t1"] == b["t0"]  # contiguous, in order
    langs = [(c["lang"], c["engine"]) for c in chunks if not c.get("skipped")]
    assert ("en", "parakeet") in langs and ("fr", "whisper") in langs
    fr = [c for c in chunks if c["lang"] == "fr" and not c.get("skipped")]
    assert all(39.5 <= c["t0"] and c["t1"] <= 96 for c in fr)  # switch split near 40 s
    assert any(abs(c["t0"] - 40.3) < 1.0 for c in fr)
    for c in chunks:
        for s in c["segments"]:
            assert c["t0"] <= s["t0"] <= s["t1"] <= c["t1"]
            assert s["text"] == s["text"].strip()
    # the fr->en switch lands inside the 20 s silence (75.6-95.6); a silent-only chunk is skipped
    assert all(c["t1"] <= 95.6 for c in fr) and max(c["t1"] for c in fr) >= 75.6
    assert any(c.get("skipped") == "silence" and c["segments"] == [] for c in chunks)
    assert "whisper" in eng.loaded and eng.loaded.index("lid") == 0


def test_parakeet_input_padded_and_segments_clamped() -> None:
    """Parakeet TDT drops trailing speech when the input ends right after it: every chunk
    gets PARAKEET_TAIL_PAD s of zeros, and segments reaching into the pad clamp to the chunk."""
    x = voiced(5.0)
    y = ac.pad_tail(x)
    assert len(y) == len(x) + int(ac.PARAKEET_TAIL_PAD * SR) and not y[len(x):].any()
    assert ac.pad_tail(x, 0.0) is x
    segs = ac._abs_segments([{"t0": 0.1, "t1": 6.9, "text": "tail"}], 10.0, 15.0)
    assert segs == [{"t0": 10.1, "t1": 15.0, "text": "tail"}]


def test_parakeet_hole_filled_by_second_pass() -> None:
    """TDT skipped a sentence (voiced 10-16 s uncovered): a second pass over the hole adds
    it; a silent gap (20-24 s) is not a hole; overlapping second-pass output is dropped."""
    x = np.concatenate([voiced(20.0), quiet(4.0), voiced(6.0)])
    calls: list[float] = []

    class Skipper(FakeEngine):
        def parakeet(self, a: np.ndarray) -> list[dict[str, Any]]:
            calls.append(len(a) / SR)
            if len(calls) == 1:  # first pass: misses 10-16 s
                return [{"t0": 0.2, "t1": 10.0, "text": "one"}, {"t0": 16.0, "t1": 20.0, "text": "three"},
                        {"t0": 24.1, "t1": 29.8, "text": "four"}]
            return [{"t0": 0.4, "t1": 6.6, "text": "two"}, {"t0": 6.5, "t1": 6.9, "text": "thr"}]

    holes = ac.find_holes(x, [{"t0": 0.2, "t1": 10.0}, {"t0": 16.0, "t1": 20.0}, {"t0": 24.1, "t1": 29.8}])
    assert holes == [(10.0, 16.0)]
    segs = ac.parakeet_filled(Skipper(), x)
    assert [s["text"] for s in segs] == ["one", "two", "three", "four"]
    assert segs[1]["t0"] == pytest.approx(9.9) and calls[1] == pytest.approx(7.0 + ac.PARAKEET_TAIL_PAD)
    assert ac.find_holes(quiet(10.0), []) == []  # silence is never a hole


@needs_ffmpeg
def test_process_job_range_forced_lang_and_music_guard(tmp_path: Path) -> None:
    wav = mixed_file(tmp_path)
    eng = FakeEngine()
    evs: list[dict[str, Any]] = []
    ac.process_job({"job_id": "j2", "audio": str(wav), "from": 10.0, "to": 30.0, "lang": "fr"}, eng, evs.append)
    chunks = [e for e in evs if e["ev"] == "chunk"]
    assert [(c["t0"], c["t1"], c["lang"], c["engine"]) for c in chunks] == [(10.0, 30.0, "fr", "whisper")]
    assert chunks[0]["segments"][0]["t0"] == pytest.approx(10.1)  # absolute times
    assert "lid" not in eng.loaded  # --lang skips LID
    assert evs[-1]["lid_s"] == 0.0 or evs[-1]["lid_s"] < 0.05

    # music guard: every LID window at p=0.5 -> parakeet even though LID says fr
    eng2 = FakeEngine(lid_p=0.5)
    evs.clear()
    ac.process_job({"job_id": "j3", "audio": str(wav), "from": 40.6, "to": 75.0, "lang": None}, eng2, evs.append)
    chunks = [e for e in evs if e["ev"] == "chunk"]
    assert {c["engine"] for c in chunks} == {"parakeet"} and "whisper" not in eng2.loaded

    # whisper hallucinations dropped
    eng3 = FakeEngine()
    eng3.whisper_segs = [{"t0": 0, "t1": 5, "text": "Musique", "avg_logprob": -0.1, "compression_ratio": 1.0,
                          "no_speech_prob": 0.0}]
    evs.clear()
    ac.process_job({"job_id": "j4", "audio": str(wav), "from": 41.0, "to": 70.0, "lang": "fr"}, eng3, evs.append)
    assert [e["segments"] for e in evs if e["ev"] == "chunk"] == [[]]


# --------------------------------------------------------------------------
# worker subprocess: protocol, asr.lock, EOF (real run_worker, fake engine)
# --------------------------------------------------------------------------
FAKE_WORKER = textwrap.dedent('''
    import os, sys, time
    sys.path.insert(0, {scripts!r})
    sys.path.insert(0, {tests!r})
    import asr_common
    from test_asr_common import FakeEngine

    class Eng(FakeEngine):
        backend = "mlx"
        def ensure(self, role, emit_fn):
            print("library noise on stdout")  # protect_stdout must keep this off the protocol
            if os.environ.get("FAKE_FATAL") and role == "lid":
                raise asr_common.AsrError("model_download_failed", "boom")
            if os.environ.get("FAKE_SLOW"):
                time.sleep(float(os.environ["FAKE_SLOW"]))
            super().ensure(role, emit_fn)

    sys.exit(asr_common.run_worker(Eng(), sys.argv[1:]))
''')


@pytest.fixture
def fake_worker(tmp_path: Path) -> Path:
    p = tmp_path / "fake_worker.py"
    p.write_text(FAKE_WORKER.format(scripts=str(SCRIPTS_DIR), tests=str(Path(__file__).parent)))
    return p


def _spawn(script: Path, lock: Path, **env: str) -> subprocess.Popen[str]:
    return subprocess.Popen([sys.executable, str(script), "--lock", str(lock)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                            env={**os.environ, **env})


def _read_until(p: subprocess.Popen[str], ev: str, limit: float = 30.0) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    deadline = time.monotonic() + limit
    assert p.stdout is not None
    while time.monotonic() < deadline:
        line = p.stdout.readline()
        if not line:
            break
        msg = json.loads(line)  # every stdout line must be protocol JSON
        out.append(msg)
        if msg.get("ev") == ev:
            return out
    raise AssertionError(f"no {ev!r} event; got {out}")


def _send(p: subprocess.Popen[str], msg: dict[str, Any]) -> None:
    assert p.stdin is not None
    p.stdin.write(ac.encode(msg) + "\n")
    p.stdin.flush()


@needs_ffmpeg
def test_worker_protocol_job_prefetch_shutdown(tmp_path: Path, fake_worker: Path) -> None:
    wav = write_wav(tmp_path / "en.wav", voiced(20.0))
    p = _spawn(fake_worker, tmp_path / "asr.lock")
    try:
        ready = _read_until(p, "ready")[-1]
        assert ready["backend"] == "mlx" and ready["preloaded"] is True and ready["versions"] == {"fake": "1"}
        _send(p, {"op": "nonsense"})
        assert p.stdin is not None
        p.stdin.write("not json at all\n")
        _send(p, {"op": "job", "job_id": "j1", "key": "k", "audio": str(wav), "from": None, "to": None, "lang": None})
        evs = _read_until(p, "done")
        assert [e["ev"] for e in evs] == ["chunk", "done"]
        assert evs[0]["engine"] == "parakeet" and evs[0]["model"] == "fake/parakeet"
        _send(p, {"op": "job", "job_id": "j2", "key": "k", "audio": str(tmp_path / "nope.wav")})
        err = _read_until(p, "error")[-1]
        assert err["job_id"] == "j2" and err["code"] == "asr_failed"
        _send(p, {"op": "prefetch", "roles": ["whisper", "bogus"]})
        assert _read_until(p, "prefetched")[-1]["models"] == {"fake/whisper": None}
        _send(p, {"op": "shutdown"})
        assert p.wait(10) == 0
    finally:
        p.kill()


def test_worker_exits_on_stdin_eof(tmp_path: Path, fake_worker: Path) -> None:
    p = _spawn(fake_worker, tmp_path / "asr.lock")
    try:
        _read_until(p, "ready")
        assert p.stdin is not None
        p.stdin.close()
        assert p.wait(10) == 0
    finally:
        p.kill()


def test_worker_fatal_preload_error(tmp_path: Path, fake_worker: Path) -> None:
    p = _spawn(fake_worker, tmp_path / "asr.lock", FAKE_FATAL="1")
    try:
        err = _read_until(p, "error")[-1]
        assert err["job_id"] is None and err["code"] == "model_download_failed"
        assert p.wait(10) == 1
    finally:
        p.kill()


@needs_ffmpeg
def test_worker_lock_busy_defers_load_and_waits(tmp_path: Path, fake_worker: Path) -> None:
    """Spec 4.1: asr.lock before model load. Busy at startup -> no preload; a job waits."""
    wav = write_wav(tmp_path / "en.wav", voiced(10.0))
    lock = FileLock(tmp_path / "asr.lock")
    assert lock.acquire(blocking=False)
    p = _spawn(fake_worker, tmp_path / "asr.lock")
    try:
        ready = _read_until(p, "ready")[-1]
        assert ready["preloaded"] is False
        _send(p, {"op": "job", "job_id": "j1", "key": "k", "audio": str(wav)})
        assert _read_until(p, "waiting")[-1] == {"ev": "waiting", "job_id": "j1", "reason": "asr.lock"}
        time.sleep(0.5)
        lock.release()
        assert _read_until(p, "done")[-1]["job_id"] == "j1"
        # EOF while waiting for the lock -> the worker gives up and exits (no orphan)
        assert lock.acquire(blocking=False)
        _send(p, {"op": "job", "job_id": "j2", "key": "k", "audio": str(wav)})
        _read_until(p, "waiting")
        assert p.stdin is not None
        p.stdin.close()
        assert p.wait(10) == 0
    finally:
        lock.release()
        p.kill()


# --------------------------------------------------------------------------
# wfm.asr_client
# --------------------------------------------------------------------------
def test_select_backend_and_worker_command(tmp_path: Path) -> None:
    assert asrc.select_backend({"WFM_ASR_BACKEND": "cpu"}) == "cpu"
    assert asrc.select_backend({"WFM_ASR_BACKEND": "MLX"}) == "mlx"
    assert asrc.select_backend({}) in ("mlx", "cpu")
    with pytest.raises(ValueError):
        asrc.select_backend({"WFM_ASR_BACKEND": "cuda"})
    cmd = asrc.worker_command("cpu", tmp_path / "asr.lock")
    assert cmd[1:3] == ["run", "--quiet"] and cmd[-2:] == ["--lock", str(tmp_path / "asr.lock")]
    assert cmd[-3].endswith("asr_cpu.py") and os.path.isabs(cmd[-3])
    assert ("--locked" in cmd) == (SCRIPTS_DIR / "asr_cpu.py.lock").is_file()


def _ev(t0: float, t1: float, lang: str, engine: str, segs: list[tuple[float, float, str]],
        skipped: str | None = None) -> dict[str, Any]:
    e = {"ev": "chunk", "job_id": "j1", "t0": t0, "t1": t1, "lang": lang, "lang_p": 0.99, "engine": engine,
         "model": f"m/{engine}", "segments": [{"t0": a, "t1": b, "text": t} for a, b, t in segs]}
    if skipped:
        e["skipped"] = skipped
    return e


def test_build_slice_and_summary() -> None:
    evs = [_ev(60, 100, "fr", "whisper", [(61, 70, "Bonjour tout le monde")]),
           _ev(0, 60, "en", "parakeet", [(0.5, 4, "Hi everyone."), (5, 59, "A long English sentence here")]),
           _ev(100, 120, "en", "parakeet", [], skipped="silence")]
    t = asrc.build_transcript(evs, {"audio_s": 120, "wall_s": 3.0}, key="k", duration=120, backend="mlx", range_=None)
    assert [s["i"] for s in t["segments"]] == [0, 1, 2]
    assert [s["lang"] for s in t["segments"]] == ["en", "en", "fr"]
    assert t["primary_lang"] == "en" and t["stats"] == {"audio_s": 120, "wall_s": 3.0, "speed_x": 40.0, "words": 11}
    assert t["chunks"][2]["skipped"] == "silence" and t["chunks"][0]["model"] == "m/parakeet"
    assert asrc.engines_summary(t) == "parakeet:en:1,whisper:fr:1"
    s = asrc.slice_transcript(t, 50.0, 80.0, "k")
    assert [x["text"] for x in s["segments"]] == ["A long English sentence here", "Bonjour tout le monde"]
    assert s["range"] == [50.0, 80.0] and s["stats"]["words"] == 9 and s["segments"][0]["i"] == 0
    assert asrc.engines_summary({"chunks": [{"skipped": "silence"}]}) == "none"


@needs_ffmpeg
def test_asr_worker_and_transcribe_end_to_end(tmp_path: Path, fake_worker: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(asrc, "worker_command", lambda backend, lock: [sys.executable, str(fake_worker),
                                                                       "--lock", str(lock)])
    wav = mixed_file(tmp_path)
    kp = KeyPaths(tmp_path / "cache", "local-abc")
    progress: list[int] = []
    chunks: list[dict[str, Any]] = []

    async def go() -> dict[str, Any]:
        w = asrc.AsrWorker("mlx", tmp_path / "asr.lock", log_path=tmp_path / "asr.log")
        try:
            assert await w.wait_ready() >= 0
            assert w.versions == {"fake": "1"} and w.pid
            job = AsrJob(job_id="j1", key="local-abc", audio=str(wav))
            t = await asrc.transcribe(w, kp, job=job, rtag="full", duration=130.6, backend="mlx",
                                      on_progress=progress.append, on_chunk=lambda j, ev: chunks.append(ev))
            with pytest.raises(WfmError) as ei:
                await w.submit(AsrJob(job_id="j2", key="x", audio=str(tmp_path / "gone.wav")))
            assert ei.value.code == "asr_failed"
            return t
        finally:
            await w.close()
            assert not w.running

    t = asyncio.run(go())
    assert kp.transcript_json("full").is_file() and not kp.transcript_partial("full").exists()
    assert json.loads(kp.transcript_json("full").read_text()) == t
    assert t["range"] is None and t["key"] == "local-abc" and t["primary_lang"] == "en"
    # 25/50/75 at most once each, in order; a mark crossed by the final chunk is not sent (done follows)
    assert progress in ([25, 50], [25, 50, 75]) and len(chunks) == len(t["chunks"])
    assert {c["engine"] for c in t["chunks"]} == {"parakeet", "whisper"}


def test_asr_worker_fatal_error_surfaces(tmp_path: Path, fake_worker: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_FATAL", "1")
    monkeypatch.setattr(asrc, "worker_command", lambda backend, lock: [sys.executable, str(fake_worker),
                                                                       "--lock", str(lock)])

    async def go() -> None:
        w = asrc.AsrWorker("mlx", tmp_path / "asr.lock")
        try:
            with pytest.raises(WfmError) as ei:
                await w.wait_ready()
            assert ei.value.code == "model_download_failed"
            with pytest.raises(WfmError):
                await w.start()  # fatal: no respawn
        finally:
            await w.close()

    asyncio.run(go())


def test_asr_worker_crash_mid_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    crash = tmp_path / "crash.py"
    crash.write_text("import sys, json\nprint(json.dumps({'ev': 'ready', 'backend': 'mlx', 'load_s': 0}), flush=True)\n"
                     "sys.stdin.readline()\nsys.stderr.write('Segmentation fault in libmlx\\n')\nsys.exit(139)\n")
    monkeypatch.setattr(asrc, "worker_command", lambda backend, lock: [sys.executable, str(crash)])

    async def go() -> None:
        w = asrc.AsrWorker("mlx", tmp_path / "asr.lock")
        try:
            await w.wait_ready()
            with pytest.raises(WfmError) as ei:
                await w.submit(AsrJob(job_id="j1", key="k", audio="/x.wav"))
            assert ei.value.code == "asr_failed" and "Segmentation fault" in ei.value.message
        finally:
            await w.close()

    asyncio.run(go())
