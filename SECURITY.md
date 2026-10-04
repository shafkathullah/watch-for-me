# Security

## Reporting a vulnerability

Please report security issues privately through GitHub: **Security > Report a vulnerability** on this repository ([private vulnerability reporting](https://github.com/usedeepmark/watch-for-me/security/advisories/new)). Do not open a public issue for them.

You can expect a first reply within 3 days. Fixes ship as a new release, credited to you unless you prefer otherwise.

## Supported versions

Only the latest release gets security fixes.

## What the skill does on your machine

watch-for-me runs locally with your user's permissions. Knowing what it touches helps you judge reports and your own risk:

- **Network:** it downloads videos with yt-dlp and speech models from Hugging Face. With `--code`, when the video's description links a GitHub or GitLab repository and the transcribed code still has gaps, it also downloads one commit of that repository as an archive (https only, those two hosts only, size, file-count and time caps, no git, nothing from it is executed). `--no-repo` turns that off. Nothing else. No telemetry, no analytics.
- **Files:** it writes only to its cache directory (`~/Library/Caches/watch-for-me` on macOS, `~/.cache/watch-for-me` on Linux, or `$WFM_CACHE_DIR`), plus code files under `./watch-for-me/` in your project when you pass `--code`.
- **Commands:** the agent runs only `uv run --script .../watch.py` subcommands. watch.py starts yt-dlp, ffmpeg/ffprobe and the local transcription worker as child processes.
- **Untrusted content:** transcripts, on-screen text, titles and descriptions come from the video and are treated as data, and so are the files of a repository its description links: they are only read as text and compared with the code the video showed. SKILL.md and the reader prompts tell the agent never to follow instructions found in them. A video that tries prompt injection is in scope for reports.
- **Cookies:** `--cookies <browser>` passes `--cookies-from-browser` to yt-dlp so it can fetch login-gated videos. It is off by default; only use it with sites you trust.
- **Dependencies:** the transcription environments are pinned by `asr_mlx.py.lock` / `asr_cpu.py.lock`. yt-dlp is deliberately unpinned above a minimum version so site extractors stay current.
