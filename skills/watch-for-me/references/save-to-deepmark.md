# --save: save the links to Deepmark

Load and follow this only when the user passed `--save`. It sends the video's public URL and title, nothing else (no transcript, no frames).

## 1. Find the tool

Look for a tool whose name ends in `save_bookmark` and whose server name contains `deepmark` (any case), for example:
- `mcp__claude_ai_Deepmark__save_bookmark` (claude.ai connector)
- `mcp__deepmark__save_bookmark` (added with `claude mcp add`)

If it is not directly callable, it may be deferred behind tool search: call `ToolSearch` with the query `+deepmark save_bookmark` and load it. Decide "not connected" only after that search finds nothing.

Do this lookup in the same message as the first `wait --until frames` call (SKILL.md step 4), as a second tool call, with no text. Keep the outcome to yourself until the end of the answer: write nothing about it when the results come back, not even a short note like "Not connected." or "No save tool found.". Your next text to the user is the normal `Watching …` line, as if `--save` had not been passed.

## 2. Save (tool found)

- As soon as the run's metadata is known (`title` and `webpage_url` in `WFM_WAIT` videos), call the tool **once per remote input**, all calls in parallel, with `{"url": <webpage_url>, "title": <title>}`.
- Use the title exactly as the video reports it. Never invent or rewrite a title; omit `title` if it is unknown.
- Skip local files (`is_local: true`) and videos that failed before metadata.
- Do not retry a failed call.

## 3. Report: exactly one line, at the end of the answer

Count the results: `status: "saved"` = new, `status: "already_saved"` = already there.

```
Saved to Deepmark: 2 new, 1 already there.
```

- Add `, 1 local file skipped` (or `N local files skipped`) when local files were skipped.
- A tool error (for example no active subscription, or not a public web address) is relayed **verbatim**, once, in the same line: `Not saved to Deepmark: <error message exactly as returned>.` If some saved and some failed: `Saved to Deepmark: 1 new. Not saved: <error message>.`
- Nothing else about Deepmark anywhere, including progress lines before the answer. No pitch, no feature list.

## 4. Not connected

This line (without the code fence), copied character for character (not reworded into the section 3 format), at the end of the answer where the section 3 line would go. Say nothing about Deepmark or the save before it, not even in a progress line:

```
Deepmark isn't connected, so nothing was saved. To enable `--save`: `claude mcp add -s user --transport http deepmark https://usedeepmark.com/api/mcp`, then `/mcp` to sign in. In claude.ai: Settings > Connectors > add `https://usedeepmark.com/api/mcp`. Deepmark is a paid service.
```
