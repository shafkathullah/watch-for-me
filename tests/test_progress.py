"""WFM line + WFM_RESULT / WFM_WAIT / WFM_STARTED format (spec 3 "Progress format"), plus the
cli contract around them: `run --detach` returns fast and `wait` sees the run; `wait` exit
0 / 6 / 7; `cancel`. No network, no models."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import SCRIPTS_DIR
from wfm import cache, cli
from wfm.progress import (
    TAG_RESULT,
    TAG_WAIT,
    Progress,
    format_json_line,
    format_line,
    last_lines_by_key,
    parse_json_line,
    parse_line,
    sanitize_value,
)
from wfm.types import RunResult, VideoResult


# --------------------------------------------------------------------------
# lines
# --------------------------------------------------------------------------
def test_format_line_matches_spec_examples() -> None:
    assert format_line(0, None, "run", "start", run_id="wfm7k2qa", inputs=3, backend="mlx") == \
        "WFM 0.00 - run start run_id=wfm7k2qa inputs=3 backend=mlx"
    assert format_line(10.4, "youtube-zjkBMFhNj_g", "audio", "done", mb=21.0, fmt="249") == \
        "WFM 10.40 youtube-zjkBMFhNj_g audio done mb=21.0 fmt=249"
    assert format_line(99.5, "youtube-zjkBMFhNj_g", "asr", "done", engines="parakeet:en:58,whisper:fr:2",
                       speed="36x") == "WFM 99.50 youtube-zjkBMFhNj_g asr done engines=parakeet:en:58,whisper:fr:2 speed=36x"


def test_sanitize_value() -> None:
    assert sanitize_value("a b\tc\n d") == "a_b_c_d"
    assert sanitize_value(None) == "-"
    assert sanitize_value(True) == "true"
    assert sanitize_value(False) == "false"
    assert sanitize_value(3.14159) == "3.14"
    assert sanitize_value(21.0) == "21.0"
    assert sanitize_value(2.50) == "2.5"
    assert sanitize_value(3588) == "3588"
    assert sanitize_value("") == "-"


def test_values_never_contain_spaces() -> None:
    line = format_line(1.0, "k", "meta", "error", code="login required", message="Sign in to confirm")
    assert line == "WFM 1.00 k meta error code=login_required message=Sign_in_to_confirm"
    ev = parse_line(line)
    assert ev is not None and ev.kv == {"code": "login_required", "message": "Sign_in_to_confirm"}


def test_format_line_rejects_unknown_vocab() -> None:
    with pytest.raises(ValueError):
        format_line(0, None, "bogus", "start")
    with pytest.raises(ValueError):
        format_line(0, None, "run", "finished")


def test_parse_line_roundtrip() -> None:
    line = format_line(24.8, "youtube-zjkBMFhNj_g", "frames", "done", segs=73, sheets=7, grid="3x3")
    ev = parse_line(line)
    assert ev is not None
    assert (ev.elapsed, ev.key, ev.stage, ev.status) == (24.8, "youtube-zjkBMFhNj_g", "frames", "done")
    assert ev.kv == {"segs": "73", "sheets": "7", "grid": "3x3"}
    run = parse_line("WFM 0.00 - run start run_id=x")
    assert run is not None and run.key is None


@pytest.mark.parametrize("line", [
    'WFM_RESULT {"v":1}', 'WFM_WAIT {"v":1}', 'WFM_STARTED {"run_id":"x","pid":1}',
    "", "hello", "WFM abc - run start", "WFM 1.0 - run", "WFM 1.0 - run start novalue",
])
def test_parse_line_rejects_non_progress(line: str) -> None:
    assert parse_line(line) is None


def test_json_lines_roundtrip() -> None:
    v = VideoResult(input="https://youtu.be/x", key="youtube-x", status="done", title="Ünïcode title",
                    duration=19.0, stages={"meta": "done", "audio": "done", "video": "done", "frames": "done",
                                           "asr": "done"})
    rr = RunResult("wfm7k2qa", 0, 99.6, "mlx", "/p/plan.json", "visual", [v])
    line = format_json_line(TAG_RESULT, rr.to_dict())
    assert line.startswith("WFM_RESULT {") and "\n" not in line
    assert "Ünïcode" in line  # ensure_ascii=False
    obj = parse_json_line(line, TAG_RESULT)
    assert obj is not None
    assert obj["v"] == 1 and obj["ok"] is True and obj["exit"] == 0 and obj["plan_mode"] == "visual"
    assert obj["videos"][0]["key"] == "youtube-x"
    assert set(obj) >= {"v", "run_id", "ok", "exit", "elapsed_s", "backend", "plan", "plan_mode", "videos"}
    back = VideoResult.from_dict(obj["videos"][0])
    assert back == v
    assert parse_json_line(line, TAG_WAIT) is None
    assert parse_json_line("WFM_RESULT {not json", TAG_RESULT) is None
    assert parse_json_line("WFM_RESULT [1]", TAG_RESULT) is None


def test_progress_emitter_quiet_and_log(tmp_path: Path) -> None:
    out = io.StringIO()
    log = tmp_path / "log"
    p = Progress(time.monotonic(), quiet=True, stream=out, log_path=log)
    line = p.event("k1", "meta", "done", dur=12)
    p.event(None, "run", "start", inputs=1)
    p.result({"v": 1, "ok": True})
    assert out.getvalue().splitlines() == ['WFM_RESULT {"v":1,"ok":true}']  # quiet: JSON only
    logged = log.read_text().splitlines()
    assert logged[0] == line and logged[-1].startswith("WFM_RESULT")
    assert p.last()["k1"] == line
    assert p.last()["-"].endswith("run start inputs=1")


def test_last_lines_by_key(tmp_path: Path) -> None:
    log = tmp_path / "log"
    lines = [
        format_line(0, None, "run", "start", inputs=2),
        format_line(1, "a", "meta", "done"),
        "some stderr noise from a library\r",
        format_line(2, "b", "meta", "done"),
        format_line(3, "a", "asr", "progress", pct=25),
        'WFM_RESULT {"v":1}',
    ]
    log.write_text("\n".join(lines) + "\n")
    last = last_lines_by_key(log)
    assert last == {"-": lines[0], "a": lines[4], "b": lines[3]}
    assert last_lines_by_key(tmp_path / "missing") == {}
    # tail only: a tiny window still returns the newest complete lines
    assert last_lines_by_key(log, max_bytes=60).get("a") == lines[4]


# --------------------------------------------------------------------------
# cli: wait / cancel / detach
# --------------------------------------------------------------------------
def _run_json(run_id: str, **kw: object) -> None:
    base = {"v": 1, "run_id": run_id, "pid": os.getpid(), "worker_pid": None, "detached": False,
            "started": "2026-09-29T00:00:00Z", "started_ts": time.time(), "inputs": ["x"], "keys": ["k1"],
            "flags": {}, "state": "running", "frames_reached": False, "exit": None,
            "videos": [VideoResult(input="x", key="k1").to_dict()], "result": None}
    base.update(kw)
    cache.write_run_json(run_id, base)


def _wait(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, dict]:
    code = cli.main(["wait", *args])
    out = capsys.readouterr().out.strip().splitlines()
    obj = parse_json_line(out[-1], TAG_WAIT) if out else None
    return code, obj or {}


def test_wait_unknown_run(wfm_cache: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["wait", "--run", "nope", "--until", "done", "--timeout", "0"]) == 2
    assert cli.main(["wait", "--run", "../evil", "--until", "done", "--timeout", "0"]) == 2


def test_wait_timeout_is_exit_6(wfm_cache: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _run_json("wfmtime01")  # our own pid: alive
    rdir = wfm_cache / "runs" / "wfmtime01"
    (rdir / "log").write_text(format_line(1, "k1", "audio", "done", mb=1.0) + "\n")
    t = time.monotonic()
    code, obj = _wait(capsys, "--run", "wfmtime01", "--until", "frames", "--timeout", "0.6")
    assert code == 6 and 0.5 < time.monotonic() - t < 3
    assert obj["reached"] is False and obj["alive"] is True and obj["exit"] == 6
    assert obj["videos"][0]["last"].endswith("audio done mb=1.0")
    assert obj["log"].endswith("wfmtime01/log") and obj["plan"] is None


def test_wait_dead_pid_is_exit_7(wfm_cache: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    _run_json("wfmdead01", pid=p.pid)
    code, obj = _wait(capsys, "--run", "wfmdead01", "--until", "done", "--timeout", "5")
    assert code == 7 and obj["state"] == "crashed" and obj["alive"] is False
    assert cache.read_run_json("wfmdead01")["state"] == "crashed"


def test_wait_reached(wfm_cache: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _run_json("wfmfr0001", frames_reached=True)
    (wfm_cache / "runs" / "wfmfr0001" / "plan.json").write_text('{"v":1,"stage":"frames"}')
    code, obj = _wait(capsys, "--run", "wfmfr0001", "--until", "frames", "--timeout", "0")
    assert code == 0 and obj["reached"] is True and obj["plan"]["stage"] == "frames"
    assert obj["plan_path"].endswith("wfmfr0001/plan.json")
    # --until done is not reached by frames_reached alone
    code, _ = _wait(capsys, "--run", "wfmfr0001", "--until", "done", "--timeout", "0")
    assert code == 6
    _run_json("wfmdone01", state="done", exit=4, result={"v": 1, "exit": 4}, pid=None)
    code, obj = _wait(capsys, "--run", "wfmdone01", "--until", "done", "--timeout", "0")
    assert code == 0 and obj["run_exit"] == 4 and "result" not in obj  # on disk only (run.json)
    assert obj["plan"] is None and obj["plan_path"] is None


def test_wait_on_cancelled_run_is_exit_130(wfm_cache: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _run_json("wfmcanc01", state="cancelled", exit=130, frames_reached=True, pid=None)
    code, obj = _wait(capsys, "--run", "wfmcanc01", "--until", "done", "--timeout", "0")
    assert code == 130 and obj["reached"] is False and obj["run_exit"] == 130
    code, _ = _wait(capsys, "--run", "wfmcanc01", "--until", "frames", "--timeout", "0")
    assert code == 0  # frames were ready before the cancel


def test_wait_detach_stub_counts_as_alive(wfm_cache: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _run_json("wfmstub01", pid=None, started_ts=time.time())
    code, obj = _wait(capsys, "--run", "wfmstub01", "--until", "done", "--timeout", "0.2")
    assert code == 6 and obj["alive"] is True
    _run_json("wfmstub02", pid=None, started_ts=time.time() - 60)
    code, _ = _wait(capsys, "--run", "wfmstub02", "--until", "done", "--timeout", "0.2")
    assert code == 7


def _orphan_sleeper() -> int:
    """A detached `sleep` that is NOT our child (a zombie child would look alive to kill(pid, 0))."""
    code = ("import subprocess, sys; p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
            "start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); print(p.pid)")
    return int(subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout)


def test_cancel(wfm_cache: Path, capsys: pytest.CaptureFixture[str]) -> None:
    pid = _orphan_sleeper()
    try:
        _run_json("wfmcanc01", pid=pid, detached=True)
        t = time.monotonic()
        assert cli.main(["cancel", "--run", "wfmcanc01"]) == 0
        assert time.monotonic() - t < 3 and not cache.pid_alive(pid)
        assert cache.read_run_json("wfmcanc01")["state"] == "cancelled"
        assert "cancelled run wfmcanc01" in capsys.readouterr().out
        assert cli.main(["cancel", "--run", "wfmcanc01"]) == 0  # already dead: still 0
        assert "not running" in capsys.readouterr().out
        assert cli.main(["cancel", "--run", "missing1"]) == 2
    finally:
        if cache.pid_alive(pid):
            os.kill(pid, 9)


def test_wait_on_crashed_run_stays_exit_7(wfm_cache: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Review #2: the second wait on a crashed run must not report success."""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    _run_json("wfmcrash1", pid=p.pid)
    assert _wait(capsys, "--run", "wfmcrash1", "--until", "done", "--timeout", "0")[0] == 7
    code, obj = _wait(capsys, "--run", "wfmcrash1", "--until", "done", "--timeout", "0")
    assert code == 7 and obj["reached"] is False and obj["state"] == "crashed"
    _run_json("wfmcrash2", pid=p.pid, state="crashed", frames_reached=True)
    assert _wait(capsys, "--run", "wfmcrash2", "--until", "frames", "--timeout", "0")[0] == 0


def test_wait_ignores_reused_pid(wfm_cache: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Review #1: a live pid that started long after the run is someone else's process."""
    _run_json("wfmreuse1", pid=os.getpid(), pid_start=time.time() - 86400)
    code, obj = _wait(capsys, "--run", "wfmreuse1", "--until", "done", "--timeout", "5")
    assert code == 7 and obj["alive"] is False
    _run_json("wfmreuse2", pid=os.getpid(), pid_start=cache.process_start(os.getpid()))
    assert _wait(capsys, "--run", "wfmreuse2", "--until", "done", "--timeout", "0")[0] == 6


def test_process_start() -> None:
    t = time.time()
    pid = _orphan_sleeper()
    try:
        st = cache.process_start(pid)
        assert st is not None and abs(st - t) < 3
        assert cache.same_process(pid, t) and not cache.same_process(pid, t - 3600)
        assert cache.same_process(pid, None)  # older run.json: plain liveness
    finally:
        os.kill(pid, 9)
    assert cache.process_start(None) is None


def test_cancel_never_signals_a_finished_run(wfm_cache: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Review #1: run.json of a finished run whose pid now belongs to another process."""
    pid = _orphan_sleeper()
    try:
        _run_json("wfmdone02", pid=pid, detached=True, state="done", exit=0)
        assert cli.main(["cancel", "--run", "wfmdone02"]) == 0
        assert "not running (state done)" in capsys.readouterr().out
        assert cache.pid_alive(pid)
        _run_json("wfmreuse3", pid=pid, detached=True, pid_start=time.time() - 86400)  # "running", reused
        assert cli.main(["cancel", "--run", "wfmreuse3"]) == 0
        assert "already stopped" in capsys.readouterr().out
        assert cache.pid_alive(pid) and cache.read_run_json("wfmreuse3")["state"] == "cancelled"
    finally:
        os.kill(pid, 9)


def test_cancel_reaps_children_of_a_killed_run(wfm_cache: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Review #3: ffmpeg etc. run in their own sessions; after the orchestrator is SIGKILLed,
    cancel finds them in children.json and stops them (and leaves reused pids alone)."""
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    t = time.time()
    orphan = _orphan_sleeper()
    other = _orphan_sleeper()
    try:
        _run_json("wfmorph01", pid=dead.pid, detached=True)
        (wfm_cache / "runs" / "wfmorph01" / cli.CHILDREN_FILE).write_text(json.dumps(
            {"children": [[orphan, t], [other, t - 3600], [dead.pid, t]]}))
        assert cli.main(["cancel", "--run", "wfmorph01"]) == 0
        out = capsys.readouterr().out
        assert "stopped 1 leftover process" in out, out
        deadline = time.monotonic() + 3
        while cache.pid_alive(orphan) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not cache.pid_alive(orphan) and cache.pid_alive(other)
        assert cache.read_run_json("wfmorph01")["state"] == "cancelled"
    finally:
        for p in (orphan, other):
            if cache.pid_alive(p):
                os.kill(p, 9)


def test_registry_file_mirrors_children(tmp_path: Path) -> None:
    from wfm import proc

    f = tmp_path / "children.json"
    proc.set_registry_file(f)
    try:
        code = f"import json, pathlib; print(json.loads(pathlib.Path({str(f)!r}).read_text())['children'][-1][0])"
        r = proc.run([sys.executable, "-c", code])
        assert r.returncode == 0 and int(r.stdout) > 0  # the child saw itself registered
        assert proc.read_registry(f) == []  # reaped -> removed
    finally:
        proc.set_registry_file(None)
    assert proc.read_registry(tmp_path / "missing.json") == []


def _watch(env: dict[str, str], *args: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPTS_DIR / "watch.py"), *args], env=env, text=True,
                          capture_output=True, timeout=timeout, check=False)


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg")
def test_detach_returns_fast_and_wait_sees_it(wfm_cache: Path, tmp_path: Path) -> None:
    env = dict(os.environ, WFM_CACHE_DIR=str(wfm_cache))
    missing = str(tmp_path / "does-not-exist.mp4")
    t = time.monotonic()
    r = _watch(env, "run", "--detach", "--video-only", "--run-id", "wfmdet001", missing)
    took = time.monotonic() - t
    assert r.returncode == 0, r.stderr
    started = parse_json_line(r.stdout.strip().splitlines()[-1], "WFM_STARTED")
    assert started is not None and started["run_id"] == "wfmdet001" and started["pid"] > 0
    assert took < 1.0 + 0.5  # < 1 s of our own work; slack for a cold interpreter start
    w = _watch(env, "wait", "--run", "wfmdet001", "--until", "done", "--timeout", "30")
    assert w.returncode == 0, w.stdout + w.stderr
    obj = parse_json_line(w.stdout.strip().splitlines()[-1], TAG_WAIT)
    assert obj is not None and obj["state"] == "done" and obj["run_exit"] == 5
    assert obj["videos"][0]["error"]["code"] == "unsupported_url"
    log = (wfm_cache / "runs" / "wfmdet001" / "log").read_text()
    assert "WFM_RESULT" in log and " - run start " in log
    assert json.loads(log.strip().splitlines()[-1].split(" ", 1)[1])["exit"] == 5


def test_usage_errors_exit_2(wfm_cache: Path, capsys: pytest.CaptureFixture[str]) -> None:
    urls = [f"https://youtu.be/{i}" for i in range(11)]
    assert cli.main(["run", *urls]) == 2
    assert cli.main(["run", "x", "--audio-only", "--video-only"]) == 2
    assert cli.main(["run", "x", "--playlist", "11"]) == 2
    assert cli.main(["run", "x", "--from", "1:00", "--to", "0:30"]) == 2
    assert cli.main(["run", "x", "--from", "abc"]) == 2
    assert cli.main(["run", "x", "--lang", "english"]) == 2
    assert cli.main(["run", "x", "--cookies", "netscape"]) == 2
    assert cli.main(["run", "x", "--run-id", "../x"]) == 2
    assert cli.main(["bogus"]) == 2
    assert cli.main([]) == 2
    assert cli.main(["--version"]) == 0
    assert "0.1.0" in capsys.readouterr().out


def test_options_intermixed_and_code_implies_hires() -> None:
    parser = cli.build_parser()
    args = parser.run_parser.parse_intermixed_args(["a", "--code", "b", "--from", "1:30", "--lang", "FR"])
    args.cmd = "run"
    opts = cli.options_from_args(args)
    assert opts.inputs == ["a", "b"] and opts.hires and opts.code and opts.resolution == 1080
    assert opts.from_s == 90.0 and opts.lang == "fr" and opts.run_id.startswith("wfm")
    assert opts.jobs_cpu >= 2


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg")
def test_run_video_only_pipeline_and_cached_rerun(wfm_cache: Path, tmp_path: Path,
                                                   capsys: pytest.CaptureFixture[str]) -> None:
    """The whole `run` DAG on a synthetic local file without ASR: frames, sheets, plan, context,
    run.json, WFM_RESULT last; then a cached rerun reuses everything; a bad input -> exit 4."""
    pytest.importorskip("PIL")
    src = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=10:duration=4",
                    "-f", "lavfi", "-i", "color=c=blue:size=320x180:rate=10:duration=4",
                    "-filter_complex", "[0][1]concat=n=2:v=1[v]", "-map", "[v]", "-pix_fmt", "yuv420p",
                    str(src)], check=True)
    assert cli.main(["run", str(src), "--video-only", "--run-id", "wfmvid001"]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    res = parse_json_line(out[-1], TAG_RESULT)
    assert res is not None and res["exit"] == 0 and res["ok"]
    v = res["videos"][0]
    assert v["status"] == "done" and v["key"].startswith("local-") and v["is_local"]
    assert v["stages"] == {"meta": "done", "audio": "skipped", "video": "done", "frames": "done", "asr": "skipped"}
    assert v["sheets"] and all(Path(p).is_file() for p in v["sheets"])
    assert Path(v["context_md"]).read_text().startswith("# clip.mp4\n")
    plan_obj = json.loads(Path(res["plan"]).read_text())
    assert plan_obj["stage"] == "done" and plan_obj["inline_sheets"][0]["key"] == v["key"]
    events = [e for e in map(parse_line, out) if e]
    assert next((e.stage, e.status) for e in events if e.key is None) == ("run", "start")
    assert ("frames", "done") in [(e.stage, e.status) for e in events if e.key == v["key"]]
    rj = cache.read_run_json("wfmvid001")
    assert rj is not None and rj["state"] == "done" and rj["frames_reached"] and rj["exit"] == 0

    assert cli.main(["run", str(src), str(tmp_path / "missing.mp4"), "--video-only", "--quiet"]) == 4
    out2 = capsys.readouterr().out.strip().splitlines()
    assert len(out2) == 1  # --quiet: only WFM_RESULT
    res2 = parse_json_line(out2[0], TAG_RESULT)
    assert res2 is not None and res2["exit"] == 4
    assert res2["videos"][0]["stages"]["frames"] == "done" and res2["videos"][1]["key"] is None
    man = json.loads((wfm_cache / v["key"] / "manifest.json").read_text())
    assert man["stages"]["views/full-720/frames"]["status"] == "done"

    # review #8: a stored visual.md is reused while the frames are cached, and dropped
    # as soon as the frames are recomputed (its #n would point at the old tiles)
    vmd = Path(v["view_dir"]) / "visual.md"
    vmd.write_text("# visual x full-720 flags=- skill=0.1.0\nV V01 x\nEND\n")
    assert cli.main(["run", str(src), "--video-only", "--quiet"]) == 0
    v3 = parse_json_line(capsys.readouterr().out.strip().splitlines()[-1], TAG_RESULT)["videos"][0]
    assert v3["visual_cached"] is True and v3["visual_md"] == str(vmd)
    assert cli.main(["run", str(src), "--video-only", "--quiet", "--fresh"]) == 0
    v4 = parse_json_line(capsys.readouterr().out.strip().splitlines()[-1], TAG_RESULT)["videos"][0]
    assert v4["visual_cached"] is False and v4["visual_md"] is None and not vmd.exists()


def test_wait_output_is_compact(wfm_cache: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """WFM_WAIT is read by the agent every round: compact plan (task names, not batches with
    every sheet path) and only the video fields the skill uses; no duplicate `result`."""
    from wfm import plan

    big = VideoResult(input="u", key="k1", status="done", title="T", duration=6980.0,
                      view_dir="/c/k1/views/full-720", sheets=[f"/c/k1/views/full-720/sheets/s{i}.jpg" for i in range(23)],
                      frames_json="/c/k1/views/full-720/frames.json").to_dict()
    _run_json("wfmcomp01", state="done", exit=0, frames_reached=True, pid=None, videos=[big],
              result={"v": 1, "videos": [big]})
    rdir = wfm_cache / "runs" / "wfmcomp01"
    p = {"v": 1, "run_id": "wfmcomp01", "stage": "done", "mode": "windowed", "transcript_tokens_est": 40_000,
         "code": False, "views": {"k1": "/c/k1/views/full-720"}, "inline_sheets": [],
         "visual_batches": [{"id": f"V{i:02d}", "key": "k1", "sheets": [f"/c/s{j}.jpg" for j in range(4)],
                             "tiles": [1, 36], "t0": 0.0, "t1": 1.0} for i in range(1, 7)],
         "transcript_windows": [{"key": "k1", "id": f"T{i:02d}", "file": f"/c/w/T{i:02d}.md", "t0": 0, "t1": 1,
                                 "words": 2000} for i in range(1, 9)],
         "light_sheets": [{"key": "k1", "sheets": ["/c/s0.jpg", "/c/s9.jpg"]}]}
    plan.write_tasks(rdir, p, "/skill/scripts/watch.py")
    plan.write_plan(rdir, p)
    code, obj = _wait(capsys, "--run", "wfmcomp01", "--until", "done", "--timeout", "0")
    assert code == 0
    cp = obj["plan"]
    assert cp["v_tasks"] == [f"k1.V{i:02d}" for i in range(1, 7)]
    assert cp["t_tasks"] == [f"k1.T{i:02d}" for i in range(1, 9)] and cp["m_tasks"] == ["k1.M"]
    assert cp["tasks_dir"] == str(rdir / "tasks") and cp["parts_dir"] == str(rdir / "parts")
    assert all((rdir / "tasks" / f"{n}.json").is_file() for n in cp["v_tasks"] + cp["t_tasks"] + cp["m_tasks"])
    assert cp["light_sheets"] == p["light_sheets"] and "visual_batches" not in cp
    v = obj["videos"][0]
    assert v["title"] == "T" and v["view_dir"] == "/c/k1/views/full-720" and "sheets" not in v
    assert "result" not in obj
    line = format_json_line(TAG_WAIT, obj)
    assert len(line) < 2_000, len(line)  # the full plan + result of this run was ~10k chars
