# Changelog

All notable changes to watch-for-me. Versions follow [Semantic Versioning](https://semver.org).

## [0.1.1] - 2026-10-02

### Changed
- English speech model now downloads from [usedeepmark/parakeet-tdt-0.6b-v3-mlx-bf16](https://huggingface.co/usedeepmark/parakeet-tdt-0.6b-v3-mlx-bf16), a bf16 copy of `mlx-community/parakeet-tdt-0.6b-v3` with identical output. First run drops from about 3 GB to about 2 GB. Set `WFM_PARAKEET_MODEL=mlx-community/parakeet-tdt-0.6b-v3` to keep the old model.

## [0.1.0] - 2026-09-29

First release.

### Added
- `/watch-for-me` skill: 1 to 10 links or local files per call, any site yt-dlp supports.
- Local speech transcription: Parakeet TDT 0.6B v3 for English, Whisper large-v3-turbo for other languages, per-chunk language detection. MLX on Apple Silicon, CPU fallback elsewhere.
- Keyframes: scene detection, perceptual-hash dedupe, labelled contact sheets, on-demand full-resolution frames and crops (`frame`).
- Parallel pipeline: concurrent inputs, audio and video branches overlapped, one long-lived transcription worker, visual subagents start while transcription runs (`run --detach` + `wait`).
- Answer modes: default, `--tldr`, `--eli5`, `--steps`, `--code`, `--quotes`, `--ask "question"`, combinable; task continuation ("watch this and do X").
- `--save`: also save the link to a Deepmark library through its MCP connector.
- Per-video cache with LRU cap; cached reruns reuse the stored visual timeline.
- `doctor`, `setup` (model prefetch), `cache`, `cancel` subcommands.
- Packaging: Claude Code plugin + marketplace, Codex plugin manifest, `npx skills add`.
