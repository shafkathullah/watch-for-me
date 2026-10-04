# Troubleshooting

Load only when a run reports errors or warnings. Write **one line per affected video**: what happened, then the fix. Use the `hint` from the result when it has one. Other videos are answered normally.

## Per-video error codes (`videos[].error.code`, `videos[].warnings[].code`)

| Code | Line to the user | Fix to offer |
|---|---|---|
| `login_required` | `<site> needs a login for this video.` | Rerun with `--cookies chrome` (or firefox, safari, edge, brave, chromium, opera, vivaldi). Chrome on macOS shows a Keychain prompt; Safari needs Full Disk Access for the terminal; Chromium browsers on Windows usually fail |
| `private` | `This video is private.` | Only works with `--cookies <browser>` from an account that can see it |
| `geo_blocked` | `This video isn't available in your country.` | None from here |
| `live_stream` | `Live streams and upcoming premieres can't be watched.` | Retry after the stream ends and the recording is posted |
| `playlist_refused` | `That link is a playlist or a post with several videos.` | Rerun with `--playlist N` (1 to 10) to take the first N |
| `too_long` | `The video is longer than the limit (240 min by default).` | Use `--from`/`--to` for the part you need, or raise `--max-minutes` |
| `unsupported_url` | `This link isn't a supported video page.` | Check the link; any site yt-dlp supports works |
| `download_failed` | `The download failed after retries.` | Rerun later; if a whole site keeps failing, update the skill (new releases bump yt-dlp) |
| `no_audio` (warning) | `No audio track: visual-only answer.` | None |
| `no_video` (warning) | `No video track: transcript-only answer.` | None |
| `asr_failed` | `Transcription failed.` | Rerun; if it repeats, run `/watch-for-me --setup` and report the `log` path |
| `frames_failed` | `Keyframe extraction failed.` | Rerun with `--fresh`; check `doctor` for the ffmpeg version |
| `model_download_failed` | `The speech model download failed.` | Check the network and free disk, then `/watch-for-me --setup` (downloads resume) |
| `disk_full` | `The disk is full.` | Free space, or clear the cache: `watch.py cache clear --all` |

## Exit codes

| Exit | Meaning | Do |
|---|---|---|
| 0 | all inputs done | answer |
| 2 | usage error | show the stderr line; fix the flags |
| 3 | ffmpeg, ffprobe or uv missing | show `doctor` hints (e.g. `brew install ffmpeg`, the uv install docs link); the user installs them, never the agent |
| 4 | some videos failed | answer the rest, one line per failure |
| 5 | all videos failed | one line per failure, no answer |
| 6 | `wait` timed out, run still alive | not a failure: give one progress line and wait again |
| 7 | `wait` found the run dead | `cancel`, report the last WFM line and the `log` path |
| 130 | interrupted | say it was cancelled |

## Other symptoms

| Symptom | Cause | Fix |
|---|---|---|
| First run sits on `model start` for minutes | Speech model download (~1.3 GB, plus 1.6 GB for non-English) | Normal: keep waiting; `/watch-for-me --setup` prefetches |
| `meta done warn=no_js_runtime` | No JavaScript runtime for YouTube format extraction | Usually still works; `doctor` explains |
| `Unrecognized option 'fps_mode'` in the log | ffmpeg older than 5.1 | Upgrade ffmpeg (the tool falls back automatically; report if not) |
| `zsh: no matches found` | Link was not quoted | Quote every link in single quotes |
| Same wrong answer after the video changed | Cached result | Rerun with `--fresh` |
| Slow transcription on Intel, Linux or Windows | CPU backend | Expected: roughly 5 to 20x real time |
