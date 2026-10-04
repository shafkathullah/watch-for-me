# /// script
# requires-python = ">=3.10,<3.14"
# dependencies = [
#     "parakeet-mlx==0.5.2; sys_platform == 'darwin' and platform_machine == 'arm64'",
#     "mlx-whisper==0.4.3; sys_platform == 'darwin' and platform_machine == 'arm64'",
#     "numpy>=1.26; sys_platform == 'darwin' and platform_machine == 'arm64'",
# ]
# [tool.uv]
# override-dependencies = ["torch; sys_platform == 'never'"]
# ///
"""ASR worker for macOS arm64 (MLX): Parakeet TDT 0.6B v3 for English,
Whisper large-v3-turbo for other languages, whisper-tiny for LID (spec 4.5).

Spawned by wfm.asr_client as
    uv run [--locked] --script asr_mlx.py --lock <CACHE>/asr.lock
long-lived, JSONL protocol on stdin/stdout (see asr_common module docstring),
exits on stdin EOF. All protocol handling, chunking, LID splitting and routing
live in asr_common.run_worker/process_job; this file only implements the Engine.

Proven calls (scratchpad/asr/route.py, lid.py):
  parakeet: parakeet_mlx.from_pretrained(id); model.generate(get_logmel(mx.array(x),
            model.preprocessor_config))[0].sentences -> .start/.end/.text (bf16 default, greedy)
  whisper:  mlx_whisper.transcribe(x, path_or_hf_repo=id, language=lang,
            condition_on_previous_text=False)["segments"]
  lid:      mlx_whisper.load_models.load_model(id); detect_language(model,
            log_mel_spectrogram(pad_or_trim(mx.array(x), N_SAMPLES), n_mels=model.dims.n_mels)
            .astype(mx.float16)) -> (_, probs)
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from importlib import metadata
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import asr_common


class MlxEngine:
    """asr_common.Engine implementation on MLX. Models loaded lazily per role.

    Model ids: asr_common.model_ids("mlx") (env WFM_PARAKEET_MODEL / WFM_WHISPER_MODEL /
    WFM_LID_MODEL override). Whisper turbo (1.61 GB) loads only on the first
    non-English chunk. Cached models load from their local snapshot dir (no HF
    network round trip); missing ones download at the pinned revision with
    EV_MODEL events (asr_common.load_model_with_events).
    """

    backend = "mlx"

    def __init__(self) -> None:
        self.ids = asr_common.model_ids("mlx")
        self._pk: Any = None
        self._lid: Any = None
        self._whisper_path: str | None = None

    def versions(self) -> dict[str, str]:
        """{"parakeet_mlx": <ver>, "mlx_whisper": <ver>, "mlx": <ver>} via importlib.metadata."""
        out = {}
        for dist in ("parakeet-mlx", "mlx-whisper", "mlx"):
            try:
                out[dist.replace("-", "_")] = metadata.version(dist)
            except metadata.PackageNotFoundError:
                pass
        return out

    def ensure(self, role: str, emit_fn: Callable[[dict[str, Any]], None]) -> None:
        """See asr_common.Engine.ensure."""
        if role == "parakeet" and self._pk is None:
            self._pk = asr_common.load_model_with_events(self.ids["parakeet"], self._load_parakeet, emit_fn)
        elif role == "lid" and self._lid is None:
            self._lid = asr_common.load_model_with_events(self.ids["lid"], self._load_whisper_model, emit_fn)
        elif role == "whisper" and self._whisper_path is None:
            self._whisper_path = asr_common.load_model_with_events(self.ids["whisper"], self._load_turbo, emit_fn)

    def _load_parakeet(self, path: Path | None) -> Any:
        from parakeet_mlx import from_pretrained

        return from_pretrained(str(path) if path else self.ids["parakeet"])  # bf16 default, greedy

    def _load_whisper_model(self, path: Path | None) -> Any:
        from mlx_whisper.load_models import load_model

        return load_model(str(path) if path else self.ids["lid"])

    def _load_turbo(self, path: Path | None) -> str:
        """Warm mlx_whisper's ModelHolder with the exact (path, dtype) transcribe() will ask for."""
        import mlx.core as mx
        from mlx_whisper.transcribe import ModelHolder

        p = str(path) if path else self.ids["whisper"]
        ModelHolder.get_model(p, mx.float16)
        return p

    def detect_language(self, x: Any) -> tuple[str, float]:
        """See asr_common.Engine.detect_language (whisper-tiny, ~27 ms per window)."""
        import mlx.core as mx
        from mlx_whisper.audio import N_SAMPLES, log_mel_spectrogram, pad_or_trim
        from mlx_whisper.decoding import detect_language

        mel = log_mel_spectrogram(pad_or_trim(mx.array(x), N_SAMPLES), n_mels=self._lid.dims.n_mels)
        _, probs = detect_language(self._lid, mel.astype(mx.float16))
        lang = max(probs, key=probs.get)
        return lang, float(probs[lang])

    def parakeet(self, x: Any) -> list[dict[str, Any]]:
        """See asr_common.Engine.parakeet."""
        import mlx.core as mx
        from parakeet_mlx.audio import get_logmel

        r = self._pk.generate(get_logmel(mx.array(x), self._pk.preprocessor_config))[0]
        return [{"t0": float(s.start), "t1": float(s.end), "text": s.text.strip()}
                for s in r.sentences if s.text.strip()]

    def whisper(self, x: Any, lang: str) -> list[dict[str, Any]]:
        """See asr_common.Engine.whisper."""
        import mlx_whisper

        r = mlx_whisper.transcribe(x, path_or_hf_repo=self._whisper_path or self.ids["whisper"],
                                   language=lang, condition_on_previous_text=False)
        return [{"t0": float(s["start"]), "t1": float(s["end"]), "text": s["text"].strip(),
                 "avg_logprob": s.get("avg_logprob"), "compression_ratio": s.get("compression_ratio"),
                 "no_speech_prob": s.get("no_speech_prob")} for s in r.get("segments", [])]


def main(argv: list[str] | None = None) -> int:
    """Entry: asr_common.run_worker(MlxEngine(), argv)."""
    return asr_common.run_worker(MlxEngine(), argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
