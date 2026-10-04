# Changelog

All notable changes to watch-for-me. Versions follow [Semantic Versioning](https://semver.org).

## [0.2.0] - 2026-10-04

### Added
- `--code` now handles code that scrolls or grows across several keyframes. Readers record the file name and visible line numbers of each code frame, fetch a few extra frames when two frames of the same file don't connect, and a stitcher in the CLI merges the blocks into files. Lines the video never showed become a visible `[gap: lines N-M not shown in the video]` comment instead of being guessed.
- When the video's description links a GitHub or GitLab repository, `--code` can fill those gaps from it. A gap is filled only when the lines on both sides match the repo's file exactly, and filled lines are marked with their repo, commit and path. If the repo has changed since the video, the gap stays. `--no-repo` turns this off. The fetch is one read-only archive download with size and time limits; nothing from the repo is run.

### Changed
- Readers zoom to the exact frame time of each tile (the old "end of span" rule could show a different screen).
- With `--code`, short videos always use a reader subagent.

## [0.1.4] - 2026-10-04

### Changed
- The repository moved to [usedeepmark/watch-for-me](https://github.com/usedeepmark/watch-for-me). The old `shafkathullah/watch-for-me` path redirects; install commands now use the new one.

## [0.1.3] - 2026-10-04

### Changed
- The skill now pre-approves only its own script (`uv run --script '<skill dir>/scripts/watch.py' ...`) instead of any `uv run --script ... watch.py` command.

### Added
- Plugin icon.

## [0.1.2] - 2026-10-04

### Changed
- The missing-uv hint points at `brew install uv` / the uv install docs instead of a piped shell installer, and the skill tells the agent never to run installers.
- `asr_mlx.py.lock` is limited to macOS arm64 (283 KB to 86 KB).

### Added
- `SECURITY.md`, GitHub Actions pinned to commit SHAs, demo GIF in the README (a real, sped-up Claude Code run).

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
