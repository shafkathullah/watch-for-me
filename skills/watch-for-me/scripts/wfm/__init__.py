"""watch-for-me orchestrator package (imported by scripts/watch.py).

Module map and call graph (spec section 2; arrows = "imports and calls"):

    watch.py -> wfm.cli
    wfm.cli  -> wfm.ingest, wfm.frames, wfm.sheets, wfm.asr_client, wfm.plan,
                wfm.cache, wfm.progress, wfm.doctor
    wfm.ingest, wfm.frames, wfm.sheets, wfm.plan, wfm.doctor -> wfm.types, wfm.proc, wfm.cache (paths only)
    wfm.asr_client -> asr_common (protocol constants/codec only), wfm.cache, wfm.types, wfm.proc
    asr_mlx.py / asr_cpu.py (separate uv envs) -> asr_common, wfm.cache.FileLock

Everything under wfm/ is stdlib + Pillow only (the watch.py env has no numpy).
"""

VERSION = "0.1.3"
