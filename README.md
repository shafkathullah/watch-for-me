# watch-for-me

**Give your agent any video. It watches it for you, on your machine.**

A free agent skill for Claude Code, Codex and any Agent Skills host. Paste a YouTube, Instagram Reel, TikTok, X, Vimeo or Loom link (or a local file): speech is transcribed on your device, keyframes are pulled into contact sheets your agent reads, and you get a timestamped timeline of what is said and shown. Several videos at once.

Guide: [usedeepmark.com/can-claude-watch-videos](https://usedeepmark.com/can-claude-watch-videos?ref=watch-for-me)

<!-- demo GIF goes here: docs/demo.gif, 20 s, `/watch-for-me <public talk> --tldr` recorded with vhs or a real screen capture; no personal names, handles or accounts visible -->

## Install

| Host | Command |
|---|---|
| Claude Code | `/plugin marketplace add shafkathullah/watch-for-me` then `/plugin install watch-for-me@watch-for-me`. Invoke as `/watch-for-me` (autocomplete may show `/watch-for-me:watch-for-me`) |
| Any Agent Skills host | `npx skills add shafkathullah/watch-for-me -g --skill watch-for-me` |
| Codex | `codex plugin marketplace add shafkathullah/watch-for-me` then `codex plugin add watch-for-me@watch-for-me` |
| Manual | clone this repo, copy `skills/watch-for-me/` to `~/.claude/skills/watch-for-me/` |

You also need [uv](https://docs.astral.sh/uv/) and ffmpeg (see [Requirements](#requirements)).

## First run

The first run downloads the speech models and a Python runtime once: about **3 GB** for English (Parakeet 2.5 GB + a 74 MB language detector + runtime), about **5 GB** if you also watch non-English videos (Whisper turbo, 1.6 GB, fetched on the first non-English speech). Later runs start in seconds.

Prefetch everything up front instead of waiting on your first video:

```
/watch-for-me --setup
```

## Use

```
/watch-for-me https://www.youtube.com/watch?v=zjkBMFhNj_g
/watch-for-me https://youtu.be/aaa https://www.instagram.com/reel/bbb/ ~/Movies/demo.mp4
/watch-for-me https://youtu.be/xyz --steps
/watch-for-me https://youtu.be/xyz --ask "which GPU does he recommend?"
/watch-for-me https://youtu.be/xyz --from 12:00 --to 20:00 --tldr
```

Or just talk to your agent: "watch this and tell me the steps", "what does she say about pricing in these three videos?".

**Watch and do.** Ask for work, not a summary, and the agent watches first and then does it: "watch this tutorial and add the same feature to my app", "use this video to set up the database". You get one line (`Watched <title> (<duration>).`) and then the work.

## Flags

| Flag | Effect |
|---|---|
| (none) | Summary, timeline of what is said and shown, key on-screen text |
| `--tldr` | 1 to 3 sentences + 3 key timestamps (light visuals, fastest) |
| `--eli5` | Plain-words explanation, 150 words or fewer |
| `--steps` | "You'll need" list + numbered checklist with timestamps |
| `--code` | Transcribes on-screen code from full-resolution frames into `./watch-for-me/<video>/` files |
| `--quotes` | 5 to 15 verbatim quotes with timestamps (transcript only, skips frames) |
| `--ask "question"` | Answers the question with timestamp evidence |
| `--save` | also save the link to your [Deepmark](https://usedeepmark.com/?ref=watch-for-me) library (needs the Deepmark MCP connection and a Deepmark plan) |
| `--from T` / `--to T` | Only this part (`SS`, `MM:SS` or `HH:MM:SS`); timestamps stay absolute |
| `--hires` | 1080p video, bigger tiles (small text, dense slides) |
| `--lang xx` | Speech language (ISO 639-1, e.g. `fr`), skips language detection. Does not change the answer language |
| `--cookies BROWSER` | Use your browser's login (chrome, firefox, safari, edge, brave, chromium, opera, vivaldi) for sites that need one |
| `--playlist N` | Accept a playlist or multi-video post, take the first N (up to 10) |
| `--audio-only` / `--video-only` | Skip frames / skip transcription |
| `--fresh` | Ignore the cache |
| `--setup` | Prefetch the models, then stop |

Modes combine: `--tldr --quotes`, `--steps --ask "..."`. Up to 10 links per call. YouTube timestamps come back as clickable deep links.

## How it works

<!-- Numbers below: v0.1 integration pass 2026-09-29 (M1 Pro 16 GB, ~5 MB/s), see evals/cases.md. -->

1. **Download**: [yt-dlp](https://github.com/yt-dlp/yt-dlp) fetches audio and a 720p video stream in parallel (original-language audio, never an auto-dub). A 1 hour talk is about 50 MB.
2. **Transcribe on your device**: English goes to NVIDIA Parakeet TDT 0.6B v3, other languages to Whisper large-v3-turbo, chosen per minute of audio by a small language detector. Apple Silicon runs on the GPU through MLX (about 40x real time for English, about 20x for mixed languages); other machines use a CPU build (about 7 to 19x on an M1 Pro).
3. **Keyframes**: ffmpeg scene detection plus perceptual-hash dedupe keeps one frame per slide, scene or code change (a 1 hour slide talk: 523 candidates down to 73 frames, ready about 23 s after you ask), tiled into labelled contact sheets.
4. **Your agent reads**: subagents read the sheets in parallel while transcription is still running, zoom into full-resolution frames for small text and code, and the main agent merges everything into one timeline. Long transcripts are digested by subagents too, so a 1 hour video costs the main conversation about 25k tokens.

Everything is cached per video: asking a second question about the same video starts in under a second.

## Privacy

Audio and transcription never leave your device. The frames your agent reads go to your agent's model provider, like anything else you show it. No telemetry.

The only network calls are yt-dlp downloading the video and the one-time model downloads from Hugging Face.

## Requirements

- [uv](https://docs.astral.sh/uv/getting-started/installation/): `curl -LsSf https://astral.sh/uv/install.sh | sh`
- ffmpeg 5.1 or newer (older works with a fallback): `brew install ffmpeg`, `sudo apt install ffmpeg`, or `winget install ffmpeg`
- macOS on Apple Silicon (macOS 14+) for the fast path. Intel Macs and Linux use the CPU path. Windows may work on the CPU path but is untested.
- Free disk: 5 GB for the first run.

Nothing else: Python dependencies install themselves through uv, and the JavaScript runtime yt-dlp needs for YouTube (deno) ships with yt-dlp's `deno` extra.

## Models and licenses

| Model | Used for | Size | License |
|---|---|---|---|
| [nvidia/parakeet-tdt-0.6b-v3](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3) via [mlx-community/parakeet-tdt-0.6b-v3](https://huggingface.co/mlx-community/parakeet-tdt-0.6b-v3) | English speech (Apple Silicon) | 2.51 GB | CC BY 4.0, NVIDIA |
| [istupakov/parakeet-tdt-0.6b-v3-onnx](https://huggingface.co/istupakov/parakeet-tdt-0.6b-v3-onnx) (int8) | English speech (CPU) | 670 MB | CC BY 4.0, NVIDIA |
| [mlx-community/whisper-large-v3-turbo](https://huggingface.co/mlx-community/whisper-large-v3-turbo) | Other languages (Apple Silicon) | 1.61 GB | MIT (OpenAI Whisper) |
| [dropbox-dash/faster-whisper-large-v3-turbo](https://huggingface.co/dropbox-dash/faster-whisper-large-v3-turbo) | Other languages (CPU) | 1.6 GB | MIT |
| [mlx-community/whisper-tiny-mlx](https://huggingface.co/mlx-community/whisper-tiny-mlx) / [Systran/faster-whisper-tiny](https://huggingface.co/Systran/faster-whisper-tiny) | Language detection | 74 / 76 MB | MIT (OpenAI Whisper) |
| [istupakov/silero-vad-onnx](https://huggingface.co/istupakov/silero-vad-onnx) | Voice activity (CPU) | 6 MB | MIT |

Parakeet TDT 0.6B v3 is by NVIDIA and licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Whisper is by OpenAI (MIT). Runtimes: [parakeet-mlx](https://github.com/senstella/parakeet-mlx), [mlx-whisper](https://github.com/ml-explore/mlx-examples), [onnx-asr](https://github.com/istupakov/onnx-asr), [faster-whisper](https://github.com/SYSTRAN/faster-whisper), [yt-dlp](https://github.com/yt-dlp/yt-dlp), [ffmpeg](https://ffmpeg.org), [Pillow](https://python-pillow.org).

Models live in the Hugging Face cache (`~/.cache/huggingface/hub`, or `$HF_HOME`). Override them with `WFM_PARAKEET_MODEL`, `WFM_WHISPER_MODEL`, `WFM_LID_MODEL` (a Hugging Face id or a local directory).

## Updating

- Claude Code: `/plugin marketplace update watch-for-me`, then restart the session.
- Agent Skills hosts: `npx skills update`, or run the `npx skills add` command again.
- Codex: re-run the install commands.

**When YouTube (or another site) breaks**: sites change often and yt-dlp follows within days. watch-for-me already retries a failed download once with the newest yt-dlp release. If downloads still fail, update the skill: each release bumps the yt-dlp pin.

## Uninstall and cache cleanup

1. Remove the skill: `/plugin uninstall watch-for-me@watch-for-me` (Claude Code), or delete `~/.claude/skills/watch-for-me/`.
2. Delete the video cache (downloads, frames, transcripts; capped at 10 GB by default, `WFM_CACHE_MAX_GB`):
   - with the skill still installed: `uv run --script <skill dir>/scripts/watch.py cache clear --all`
   - or delete the folder: `~/Library/Caches/watch-for-me` (macOS), `~/.cache/watch-for-me` (Linux), `%LOCALAPPDATA%\watch-for-me\Cache` (Windows), or your `WFM_CACHE_DIR`.
3. Delete the models from `~/.cache/huggingface/hub` (or `$HF_HOME/hub`):
   - `models--mlx-community--parakeet-tdt-0.6b-v3` (2.5 GB)
   - `models--mlx-community--whisper-large-v3-turbo` (1.6 GB)
   - `models--mlx-community--whisper-tiny-mlx` (74 MB)
   - CPU path: `models--istupakov--parakeet-tdt-0.6b-v3-onnx` (670 MB), `models--dropbox-dash--faster-whisper-large-v3-turbo` (1.6 GB), `models--Systran--faster-whisper-tiny` (76 MB), `models--istupakov--silero-vad-onnx` (6 MB)
4. Python environments (about 0.65 GB) live in uv's cache: `uv cache prune` removes the unused ones.

## FAQ

**Does it see motion?** watch-for-me sees keyframes, not motion. It catches every slide, scene change and line of code on screen, but it won't judge a golf swing.

**How many tokens does it use?** Visuals cost about 200 tokens per minute for a slide talk and about 3,000 per minute for fast-cut videos, and most of that is spent in subagents, not your main conversation. A transcript is about 13k tokens per hour of speech. Default mode keeps a 1 hour talk at about 25k tokens in the main conversation.

**Instagram (or another site) says login required.** Add `--cookies chrome` (or your browser). Chrome on macOS asks for Keychain access; Safari needs Full Disk Access for your terminal.

**How much disk does it use?** About 55 MB per hour of video kept in the cache (audio, 720p video, frames, sheets), capped at 10 GB with least-recently-used eviction. Plus the models (see above).

**Does it need captions?** No. It transcribes the audio itself, so Reels, TikToks and X videos without captions work.

**What does `--save` do?** It also saves the video's link and title to your [Deepmark](https://usedeepmark.com/?ref=watch-for-me) library, where it becomes searchable by what was said and shown. Only the URL and title are sent, never the transcript or frames. It needs the Deepmark MCP connection (`claude mcp add -s user --transport http deepmark https://usedeepmark.com/api/mcp`) and a Deepmark plan. There is no free tier: without a plan the save is refused with a message saying so. Deepmark indexes YouTube and Instagram as video (TikTok video indexing is limited for now); other sites are saved as a page.

**Is it safe to run on untrusted videos?** Everything that comes out of a video (speech, on-screen text, title, description) is treated as data: the skill tells your agent and its subagents never to follow instructions found in it, and the only commands it runs are its own `watch.py` subcommands. The skill pre-approves only `uv run --script …watch.py…` commands, not arbitrary shell. No defense against prompt injection is perfect: review what your agent does when you ask it to act on a video ("watch this and do X").

**Why does Claude Code ask for permission when I just say "watch this"?** Typing `/watch-for-me …` gives the skill its pre-approved commands. When Claude picks the skill on its own (plain-language requests, "watch this and do X"), Claude Code does not apply the skill's pre-approvals, so you get prompts for its commands and its cache reads and writes. To skip them, add this to `~/.claude/settings.json` (on Linux the cache is `~/.cache/watch-for-me`):

```json
{ "permissions": { "allow": ["Bash(uv run --script *watch.py*)", "Read(~/Library/Caches/watch-for-me/**)", "Edit(~/Library/Caches/watch-for-me/**)"] } }
```

**Which agents work?** Claude Code (tested), Codex and any host that supports Agent Skills and can run shell commands. Hosts without subagents read the sheets in the main conversation; hosts without image input get transcript-only answers.

**Can it do live streams?** No. Finished recordings of past streams work.

## Troubleshooting

| Problem | Fix |
|---|---|
| `ffmpeg` or `uv` not found | Install them (see [Requirements](#requirements)), then retry |
| First run looks stuck | It is downloading ~3 GB of models; run `/watch-for-me --setup` once to see progress |
| "needs a login" / private video | `--cookies chrome` (or your browser) |
| Playlist or multi-video post refused | Add `--playlist N` |
| Video longer than 4 hours | `--from` / `--to`, or `--max-minutes` |
| Stale or wrong result | `--fresh` |
| `zsh: no matches found` running the CLI by hand | Quote the link: `'https://…?v=x&t=30'` |
| Anything else | Run `uv run --script <skill dir>/scripts/watch.py doctor` and open an issue with its output |

## Credits

**Made by [Deepmark](https://usedeepmark.com/?ref=watch-for-me).** Deepmark saves everything you bookmark (links, X posts, Reels, YouTube) and lets you search it in plain English, down to what was said in a video. Add `--save` and watch-for-me also keeps the video in your Deepmark library.

## License

MIT, see [LICENSE](LICENSE). Model licenses: see [Models and licenses](#models-and-licenses).
