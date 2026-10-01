# /// script
# requires-python = ">=3.10,<3.14"
# dependencies = ["onnx-asr[cpu,hub]==0.12.0", "faster-whisper==1.2.1", "numpy>=1.26"]
# ///
"""ASR worker for CPU (Intel Mac, Linux, Windows): onnx-asr Parakeet int8 for
English, faster-whisper large-v3-turbo int8 otherwise, faster-whisper tiny for LID
(spec 4.5 "CPU backend mapping").

Spawned by wfm.asr_client as
    uv run [--locked] --script asr_cpu.py --lock <CACHE>/asr.lock
Same JSONL protocol, chunking and routing as asr_mlx.py (asr_common.run_worker).

Proven calls (spec 4.5, [M] on M1 CPU):
  parakeet: onnx_asr.load_model("nemo-parakeet-tdt-0.6b-v3", quantization="int8",
            providers=["CPUExecutionProvider"])            # providers is MANDATORY (5.0x vs 18.6x)
            .with_vad(onnx_asr.load_vad("silero"), max_speech_duration_s=30)
  whisper:  faster_whisper.WhisperModel(<id>, device="cpu", compute_type="int8")
            .transcribe(x, language=lang, condition_on_previous_text=False)
  lid:      WhisperModel("tiny"-repo).detect_language(audio=np.ndarray) -> (lang, p, all_probs)
Roles: "parakeet" also needs the silero VAD (istupakov/silero-vad-onnx, 6 MB).
Whisper runs greedy (beam_size=1; faster-whisper defaults to 5, MLX path is greedy)
without its own VAD, so chunking/filters match the MLX backend.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from importlib import metadata
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import asr_common

ONNX_PROVIDERS = ["CPUExecutionProvider"]  # mandatory: CoreML provider ran 5.0x vs 18.6x [M]
VAD_ID = "silero"


class CpuEngine:
    """asr_common.Engine implementation on onnxruntime + ctranslate2 (CPU only).

    Model ids: asr_common.model_ids("cpu"). cpu_threads: library defaults.
    faster-whisper repos load from their local snapshot (pinned revision) when cached;
    onnx-asr resolves its own files local-first (hub download only when missing).
    """

    backend = "cpu"

    def __init__(self) -> None:
        self.ids = asr_common.model_ids("cpu")
        self._pk: Any = None
        self._lid: Any = None
        self._whisper: Any = None

    def versions(self) -> dict[str, str]:
        """{"onnx_asr": <ver>, "faster_whisper": <ver>, "onnxruntime": <ver>, "ctranslate2": <ver>}."""
        out = {}
        for dist in ("onnx-asr", "faster-whisper", "onnxruntime", "ctranslate2"):
            try:
                out[dist.replace("-", "_")] = metadata.version(dist)
            except metadata.PackageNotFoundError:
                pass
        return out

    def ensure(self, role: str, emit_fn: Callable[[dict[str, Any]], None]) -> None:
        """See asr_common.Engine.ensure."""
        if role == "parakeet" and self._pk is None:
            vad = asr_common.load_model_with_events(VAD_ID, self._load_vad, emit_fn, self_download=True)
            model = asr_common.load_model_with_events(self.ids["parakeet"], self._load_parakeet, emit_fn,
                                                      self_download=True)
            self._pk = model.with_vad(vad, max_speech_duration_s=30)
        elif role == "lid" and self._lid is None:
            self._lid = asr_common.load_model_with_events(self.ids["lid"], self._load_fw, emit_fn)
        elif role == "whisper" and self._whisper is None:
            self._whisper = asr_common.load_model_with_events(self.ids["whisper"], self._load_fw, emit_fn)

    def _load_vad(self, _path: Path | None) -> Any:
        import onnx_asr

        return onnx_asr.load_vad(VAD_ID, providers=ONNX_PROVIDERS)

    def _load_parakeet(self, _path: Path | None) -> Any:
        """onnx-asr resolves alias -> repo and tries local files first, then downloads
        only the int8 files (so we never pass a snapshot dir that may lack them)."""
        import onnx_asr

        mid = self.ids["parakeet"]
        if asr_common.hf_repo_id(mid) is None:  # local dir override
            return onnx_asr.load_model("nemo-parakeet-tdt-0.6b-v3", mid, quantization="int8",
                                       providers=ONNX_PROVIDERS)
        return onnx_asr.load_model(mid, quantization="int8", providers=ONNX_PROVIDERS)

    @staticmethod
    def _load_fw(path: Path | None) -> Any:
        from faster_whisper import WhisperModel

        if path is None:
            raise FileNotFoundError("model snapshot missing after download")
        return WhisperModel(str(path), device="cpu", compute_type="int8")

    def detect_language(self, x: Any) -> tuple[str, float]:
        """See asr_common.Engine.detect_language (faster-whisper tiny)."""
        lang, p, _all = self._lid.detect_language(audio=x)
        return lang, float(p)

    def parakeet(self, x: Any) -> list[dict[str, Any]]:
        """See asr_common.Engine.parakeet (VAD segments -> t0/t1/text)."""
        return [{"t0": float(s.start), "t1": float(s.end), "text": s.text.strip()}
                for s in self._pk.recognize(x, sample_rate=asr_common.SR) if s.text.strip()]

    def whisper(self, x: Any, lang: str) -> list[dict[str, Any]]:
        """See asr_common.Engine.whisper (faster-whisper Segment fields map 1:1)."""
        segs, _info = self._whisper.transcribe(x, language=lang, beam_size=1, condition_on_previous_text=False)
        return [{"t0": float(s.start), "t1": float(s.end), "text": s.text.strip(), "avg_logprob": s.avg_logprob,
                 "compression_ratio": s.compression_ratio, "no_speech_prob": s.no_speech_prob} for s in segs]


def main(argv: list[str] | None = None) -> int:
    """Entry: asr_common.run_worker(CpuEngine(), argv)."""
    return asr_common.run_worker(CpuEngine(), argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
