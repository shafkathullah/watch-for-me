"""`doctor`: prerequisite + model-cache checks with install hints (spec 3, 4.5).

Blocking (-> exit 3): ffmpeg, ffprobe, uv missing. Everything else is a warning.
No network calls. Model cache checks use asr_common.hf_repo_cached (stdlib).

JSON shape (`doctor --json`), skeleton decision:
{"v":1,"ok":bool,"exit":0|3,"backend":"mlx"|"cpu","python":"3.13.1","platform":"darwin-arm64",
 "checks":[{"name":"ffmpeg","ok":true,"value":"8.0.1","blocking":true,"hint":null}, ...],
 "models":[{"role":"parakeet","id":"…","repo":"…","cached":false,"mb":2510}, ...],
 "download_mb":{"english":int,"other_languages":int},   # models still missing (+ env est.)
 "free_gb":float,"cache_dir":str}
`doctor --quick --brief` (the agent's preflight, ~1/10 the size): one JSON line
{"v":1,"ok","exit","backend","failed":[{"name","blocking","hint"}] (failed checks only),
 "models_missing":[roles not cached],"download_mb":{...} (only when something is missing, else null)}
Check names: ffmpeg, ffprobe, uv, ffmpeg_fps_mode (warn < 5.1), js_runtime (info: deno ships
with yt-dlp[deno]; node/bun listed), free_disk (warn < 5 GB), backend, macos_version
(mlx: warn < 14), uv_script_lock (warn if uv too old for script lockfiles), models.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import asr_common

from . import proc
from .cache import cache_root

FREE_DISK_WARN_GB = 5.0
# [U] exact minimum: PEP 723 script lockfiles (`uv lock --script`, adjacent .lock used by
# `uv run --script`) landed in the uv 0.5.x line; 0.5.17 is the conservative floor used here.
UV_MIN_SCRIPT_LOCK = (0, 5, 17)
ENV_MB = {"mlx": 540, "cpu": 400, "watch": 110}  # approx env sizes for the first-run estimate
INSTALL_HINTS = {
    "ffmpeg": {"darwin": "brew install ffmpeg", "linux": "sudo apt install ffmpeg  (or your distro's package)",
               "win32": "winget install ffmpeg"},
    "uv": {"darwin": "brew install uv  (or see https://docs.astral.sh/uv/getting-started/installation/)",
           "*": "install uv: https://docs.astral.sh/uv/getting-started/installation/"},
}


@dataclass
class Check:
    name: str
    ok: bool
    value: str | None = None
    blocking: bool = False
    hint: str | None = None


@dataclass
class Report:
    backend: str
    checks: list[Check] = field(default_factory=list)
    models: list[dict[str, Any]] = field(default_factory=list)
    free_gb: float = 0.0
    cache_dir: str = ""
    asr_env_installed: bool | None = None

    @property
    def blocking_failed(self) -> bool:
        return any(c.blocking and not c.ok for c in self.checks)

    @property
    def exit_code(self) -> int:
        return 3 if self.blocking_failed else 0


def install_hint(tool: str) -> str | None:
    """Platform-specific hint from INSTALL_HINTS (sys.platform key, then "*")."""
    hints = INSTALL_HINTS.get("ffmpeg" if tool == "ffprobe" else tool)
    if not hints:
        return None
    plat = "linux" if sys.platform.startswith("linux") else sys.platform
    return hints.get(plat) or hints.get("*")


def check_models(backend: str) -> list[dict[str, Any]]:
    """One dict per role in asr_common.ROLES (+ {"role":"vad","id":"silero"} on cpu):
    {"role","id","repo","cached","mb"}."""
    ids = asr_common.model_ids(backend)
    rows = [(role, ids[role]) for role in asr_common.ROLES]
    if backend == "cpu":
        rows.append(("vad", "silero"))
    return [{"role": role, "id": mid, "repo": asr_common.hf_repo_id(mid),
             "cached": asr_common.hf_repo_cached(mid), "mb": asr_common.model_size_mb(mid)}
            for role, mid in rows]


def asr_env_installed(backend: str) -> bool | None:
    """Best effort: does uv's cache hold an environment for asr_<backend>.py?
    uv names script envs "<script stem>-<hash>" under <uv cache>/environments-v2. None = unknown."""
    if not proc.which("uv"):
        return None
    try:
        r = proc.run(["uv", "cache", "dir"], timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    envs = Path(r.stdout.strip()) / "environments-v2"
    if r.returncode or not envs.is_dir():
        return None
    stems = (f"asr_{backend}-", f"asr-{backend}-")
    return any(p.name.startswith(stems) for p in envs.iterdir())


def first_run_download_mb(report: Report) -> dict[str, int]:
    """{"english": missing parakeet + lid + VAD + asr env (if not yet installed; unknown ->
    assume missing) , "other_languages": english + missing whisper}."""
    def missing(role: str) -> int:
        return sum(int(m.get("mb") or 0) for m in report.models if m["role"] == role and not m["cached"])

    env = 0 if report.asr_env_installed else ENV_MB.get(report.backend, 500)
    english = missing("parakeet") + missing("lid") + missing("vad") + env
    return {"english": english, "other_languages": english + missing("whisper")}


def _ver_tuple(v: str | None) -> tuple[int, ...]:
    if not v:
        return ()
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def run_checks(*, quick: bool = False, backend: str | None = None) -> Report:
    """Collect all checks. quick=True: only local probes that take < 1 s total
    (tool versions via wfm.proc.tool_version, disk space, model cache dirs); never
    starts yt-dlp or uv envs. Full mode additionally reports `python -m yt_dlp --version`."""
    from .asr_client import select_backend

    notes: list[Check] = []
    if backend is None:
        try:
            backend = select_backend()
        except ValueError as e:
            backend = "cpu"
            notes.append(Check("backend_env", False, os.environ.get("WFM_ASR_BACKEND"), False, str(e)))
    root = cache_root()
    rep = Report(backend=backend, cache_dir=str(root))
    checks = rep.checks

    for tool in ("ffmpeg", "ffprobe", "uv"):
        v = proc.tool_version(tool)
        checks.append(Check(tool, v is not None, v, True, None if v else install_hint(tool)))

    ffv = proc.ffmpeg_version()
    if ffv is not None:
        ok = ffv >= (5, 1)
        checks.append(Check("ffmpeg_fps_mode", ok, "-fps_mode" if ok else "-vsync (ffmpeg < 5.1)", False,
                            None if ok else "ffmpeg >= 5.1 recommended; older versions use the -vsync fallback"))

    uv_v = proc.tool_version("uv")
    if uv_v:
        ok = _ver_tuple(uv_v) >= UV_MIN_SCRIPT_LOCK
        checks.append(Check("uv_script_lock", ok, uv_v, False,
                            None if ok else "uv self update  (needs uv >= "
                            + ".".join(map(str, UV_MIN_SCRIPT_LOCK)) + " for script lockfiles)"))

    runtimes = [n for n in ("deno", "node", "bun") if proc.which(n)]
    venv_deno = any((Path(sys.executable).parent / n).exists() for n in ("deno", "deno.exe"))
    if venv_deno and "deno" not in runtimes:
        runtimes.insert(0, "deno(env)")
    checks.append(Check("js_runtime", True, ",".join(runtimes) or "deno via yt-dlp[deno]", False, None))

    try:
        free_gb = shutil.disk_usage(root).free / 1e9
    except OSError:
        free_gb = 0.0
    rep.free_gb = round(free_gb, 1)
    checks.append(Check("free_disk", free_gb >= FREE_DISK_WARN_GB, f"{free_gb:.1f}GB", False,
                        None if free_gb >= FREE_DISK_WARN_GB else
                        f"under {FREE_DISK_WARN_GB:.0f} GB free; first run needs ~3-5 GB"))

    bval = f"{backend} ({sys.platform}-{platform.machine()})"
    checks.append(Check("backend", True, bval, False, None))
    checks += notes

    if backend == "mlx" and sys.platform == "darwin":
        mac = platform.mac_ver()[0]
        ok = _ver_tuple(mac) >= (14,)
        checks.append(Check("macos_version", ok, mac or "?", False,
                            None if ok else "MLX wheels target macOS 14+; older macOS is untested"))

    rep.models = check_models(backend)
    rep.asr_env_installed = asr_env_installed(backend)
    missing = [m["id"] for m in rep.models if not m["cached"] and m["role"] != "whisper"]
    checks.append(Check("models", not missing, "cached" if not missing else "missing: " + ",".join(missing),
                        False, None if not missing else "run `watch.py setup` to prefetch"))

    if not quick:
        try:
            r = proc.run([*proc.ytdlp_base(), "--version"], timeout=30)
            yv = r.stdout.strip() if r.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            yv = None
        checks.append(Check("yt_dlp", yv is not None, yv, False, None if yv else "yt-dlp not importable"))
    return rep


def to_json(report: Report) -> dict[str, Any]:
    """Module docstring JSON shape."""
    return {
        "v": 1, "ok": not report.blocking_failed, "exit": report.exit_code, "backend": report.backend,
        "python": platform.python_version(), "platform": f"{sys.platform}-{platform.machine()}",
        "checks": [asdict(c) for c in report.checks], "models": report.models,
        "asr_env_installed": report.asr_env_installed,
        "download_mb": first_run_download_mb(report), "free_gb": report.free_gb, "cache_dir": report.cache_dir,
    }


def render_table(report: Report) -> str:
    """Human table: one line per check "ok|WARN|MISSING  name  value  hint", then models with
    sizes, then the first-run download estimate. No promo text (spec hard rule 3)."""
    lines = []
    for c in report.checks:
        status = "ok" if c.ok else ("MISSING" if c.blocking else "WARN")
        row = f"{status:<8} {c.name:<16} {c.value or '-'}"
        if c.hint:
            row += f"  ({c.hint})"
        lines.append(row)
    lines.append("")
    lines.append(f"models ({report.backend}):")
    for m in report.models:
        size = f"{m['mb']} MB" if m.get("mb") else "? MB"
        state = "cached" if m["cached"] else "missing"
        note = "  (only for non-English speech)" if m["role"] == "whisper" else ""
        lines.append(f"  {m['role']:<9} {m['id']:<46} {size:>8}  {state}{note}")
    dl = first_run_download_mb(report)
    lines.append("")
    if dl["other_languages"]:
        lines.append(f"first run downloads: ~{dl['english']} MB for English speech, "
                     f"~{dl['other_languages']} MB with other languages")
    else:
        lines.append("first run downloads: nothing (all models cached)")
    lines.append(f"cache: {report.cache_dir}  ({report.free_gb} GB free)")
    return "\n".join(lines)


def to_brief(report: Report) -> dict[str, Any]:
    """Module docstring `--brief` shape: only what the agent acts on."""
    missing = [m["role"] for m in report.models if not m["cached"]]
    dl = first_run_download_mb(report)
    return {
        "v": 1, "ok": not report.blocking_failed, "exit": report.exit_code, "backend": report.backend,
        "failed": [{"name": c.name, "blocking": c.blocking, "hint": c.hint} for c in report.checks if not c.ok],
        "models_missing": missing, "download_mb": dl if any(dl.values()) else None,
    }
