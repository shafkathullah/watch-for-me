"""`--code`: stitch the V readers' CODE blocks into files (views/<vtag>/code.md).

A V reader writes one block per code tile, plus `CODE#n.k` blocks for the extra frames it
fetched between tile n and n+1 (references/visual-reader.md):

    CODE#5 python 00:28 zoom=<jpg> file=inventory.py lines=100-131
    100|        return self._get(sku).available / usage
    101|

`file=` is the name in the editor tab / title bar (`?` or absent: none visible). With a
gutter of absolute line numbers the header has `lines=a-b` and every body line is
`<number>|<line as shown>`; otherwise the body is the plain lines. Blocks stored by older
skill versions have neither and are stitched as plain, nameless blocks.

Stitching (pure, deterministic; nothing is ever invented):
- blocks in tile order (n, k); grouped by file name
- numbered blocks merge by line number, the later block wins; lines typed or deleted inside
  the visible window shift the numbers of the lines below (detected by aligning the old and
  the new text); the same numbers with mostly different text start a new version of the file
- plain blocks merge where they share lines with what is already there (the later block
  overwrites the window it covers); no shared line: appended after a gap
- nameless blocks only merge on evidence (shared lines / matching numbers), else stay apart
- a hole becomes one comment line: `[gap: lines 41-57 not shown in the video]`

fill_gaps (the `repo-fill` subcommand): a hole is filled from the same-named file of the repo
the video's description links, only when the lines around the hole (anchors) are in that file
exactly, and for numbered code only when the hole has exactly that many lines there. Filled
lines sit between two comment lines naming their origin. Anything else keeps the gap.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath

CODE_HEADER_PREFIX = "# code "
HEADER_RE = re.compile(r"^CODE#(\d+)(?:\.(\d+))?[ \t]+(\S+)[ \t]+(\S+)(.*)$")
ATTR_RE = re.compile(r"(\w+)=(.*?)(?=\s+\w+=|\s*$)")  # a value may hold spaces (file=My File.py)
PART_HEADER_RE = re.compile(r"^V \S+ \S+ \S+$")
NUM_LINE_RE = re.compile(r"^[ \t]*(\d+)\|(.*)$")
RANGE_RE = re.compile(r"^(\d+)-(\d+)$")
MARKER_ONLY_RE = re.compile(r"^\W*\[(?:cut off|gap\b[^\]]*)\]\W*$")
NUMBERED_MIN_SHARE = 0.8  # share of non-blank body lines that must carry `n|` to trust the numbers
SAME_VERSION_MIN = 0.5  # more than this share of the comparable lines must agree: "same file, edited"
SHIFT_LOOKAHEAD = 15  # lines below a block that are compared to find typed / deleted lines
STRONG_LINE_CHARS = 8  # shorter lines (`}`, `else:`) are no evidence of an overlap
GAP = "\x00gap"  # sentinel entry in a plain version's line list
ANCHOR_LINES = 3  # lines compared on each side of a gap ...
ANCHOR_REACH = 8  # ... extended up to this many until one of them is a strong line
HEAD_ANCHOR_LINES = 8  # a gap at the top of a file has one side only: a longer anchor, at its line number
FILL_MAX_LINES = 400  # a plain gap wider than this in the repo file is not filled

_HASH = {"py", "python", "rb", "ruby", "sh", "bash", "zsh", "shell", "fish", "yaml", "yml", "toml", "r",
         "pl", "perl", "ex", "exs", "elixir", "nim", "ps1", "powershell", "dockerfile", "makefile", "mk",
         "cmake", "tf", "hcl", "terraform", "nix", "jl", "julia", "conf", "cfg", "env", "gitignore",
         "graphql", "gql", "awk", "tcl", "cr", "crystal", "text", "txt"}
_DASH = {"sql", "lua", "hs", "haskell", "elm", "ada", "adb", "vhdl", "applescript"}
_XML = {"html", "htm", "xml", "svg", "vue", "svelte", "md", "markdown", "xaml", "astro"}
_BLOCK = {"css"}
_SEMI = {"lisp", "clj", "clojure", "scm", "scheme", "asm", "s", "ini", "el"}
_PCT = {"tex", "latex", "m", "matlab", "erl", "erlang", "pro", "prolog"}


@dataclass
class CodeBlock:
    n: int
    k: int
    lang: str
    ts: str
    attrs: dict[str, str]
    body: list[str]
    numbered: list[tuple[int, str]] | None = None  # set by parse when the gutter numbers hold

    @property
    def ref(self) -> str:
        return f"#{self.n}" + (f".{self.k}" if self.k else "")

    @property
    def file(self) -> str | None:
        name = (self.attrs.get("file") or "").strip()
        return None if name in ("", "?", "-") else name

    @property
    def header(self) -> str:
        extra = "".join(f" {k}={v}" for k, v in self.attrs.items())
        return f"CODE{self.ref} {self.lang} {self.ts}{extra}"


@dataclass
class Fill:
    lines: list[str]
    origin: str  # "<host>/<owner>/<repo>@<sha12>"
    path: str  # file in the repo
    first: int  # 1-based line of lines[0] in that file


@dataclass
class CodeFile:
    """One reconstructed file (or one version of it)."""

    name: str | None
    lang: str
    first_ts: str
    last_ts: str
    num: dict[int, str] | None = None  # numbered version
    lines: list[str] | None = None  # plain version; GAP entries mark holes
    refs: list[str] = field(default_factory=list)
    edited: int = 0  # lines whose text changed between frames (latest kept)
    version: int = 1
    fills: dict[int, Fill] = field(default_factory=dict)  # gap key -> lines taken from the repo
    kept: dict[int, str] = field(default_factory=dict)  # gap key -> why the repo did not fill it

    def gaps(self) -> list[tuple[int, int]] | int:
        """Numbered: missing line ranges from line 1 to the last line seen. Plain: hole count."""
        if self.num is not None:
            out: list[tuple[int, int]] = []
            prev = 0
            for n in sorted(self.num):
                if n > prev + 1:
                    out.append((prev + 1, n - 1))
                prev = n
            return out
        return sum(1 for x in self.lines or [] if x == GAP)

    def gap_keys(self) -> list[int]:
        """One key per hole: its first missing line number (numbered), or its ordinal (plain)."""
        g = self.gaps()
        return [a for a, _ in g] if isinstance(g, list) else list(range(g))


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------
def split_visual(text: str) -> tuple[str, list[CodeBlock]]:
    """-> (text with the block bodies removed, headers kept; blocks in file order).
    A block runs from its `CODE#n ...` header to the next header, or to the `END` line that
    closes its V part (an `END` followed by more code is code)."""
    lines = text.splitlines()
    kept: list[str] = []
    blocks: list[CodeBlock] = []
    cur: CodeBlock | None = None
    for i, line in enumerate(lines):
        m = HEADER_RE.match(line)
        if m:
            attrs = {a.group(1): a.group(2).strip() for a in ATTR_RE.finditer(m.group(5))}
            cur = CodeBlock(int(m.group(1)), int(m.group(2) or 0), m.group(3), m.group(4), attrs, [])
            blocks.append(cur)
            kept.append(line)
            continue
        if cur is not None and line.strip() == "END" and _closes_part(lines, i):
            cur = None
        if cur is None:
            kept.append(line)
        else:
            cur.body.append(line)
    for b in blocks:
        _finish(b)
    return "\n".join(kept) + ("\n" if text.endswith("\n") else ""), blocks


def _closes_part(lines: list[str], i: int) -> bool:
    for nxt in lines[i + 1:]:
        if nxt.strip():
            return bool(PART_HEADER_RE.match(nxt))
    return True


def _finish(b: CodeBlock) -> None:
    """Trim blank edges; resolve the gutter numbers (CodeBlock.numbered) when they hold."""
    body = [x.rstrip() for x in b.body]
    while body and not body[0]:
        body.pop(0)
    while body and not body[-1]:
        body.pop()
    b.body = body
    rng = RANGE_RE.match(b.attrs.get("lines", ""))
    nonblank = [x for x in body if x.strip() and not MARKER_ONLY_RE.match(x)]
    hits = [x for x in nonblank if NUM_LINE_RE.match(x)]
    if nonblank and len(hits) >= NUMBERED_MIN_SHARE * len(nonblank) and (rng or len(hits) == len(nonblank)):
        entries: list[tuple[int, str]] = []
        for x in body:
            m = NUM_LINE_RE.match(x)
            if m:
                entries.append((int(m.group(1)), m.group(2)))
            elif entries and x.strip() and not MARKER_ONLY_RE.match(x):
                entries[-1] = (entries[-1][0], entries[-1][1] + "\n" + x)  # wrapped continuation
        texts = _unpad([t for _, t in entries])
        b.numbered = _dedupe([(n, t.rstrip()) for (n, _), t in zip(entries, texts)])
    elif rng and int(rng.group(2)) - int(rng.group(1)) + 1 == len(body):
        first = int(rng.group(1))  # range given, numbers left off the lines: count them
        b.numbered = [(first + i, x) for i, x in enumerate(body)]


def _unpad(texts: list[str]) -> list[str]:
    """`64| code` instead of `64|code`: one pad space on every line. Indentation is even in
    practice, so an odd minimum indent across the block is that pad."""
    lead = [len(t) - len(t.lstrip(" ")) for t in texts if t.strip() and not t.startswith("\t")]
    if lead and len(lead) == sum(1 for t in texts if t.strip()) and min(lead) % 2 == 1:
        return [t.removeprefix(" ") for t in texts]
    return texts


def _dedupe(entries: list[tuple[int, str]]) -> list[tuple[int, str]]:
    seen: dict[int, str] = {}
    for n, t in entries:
        seen[n] = t
    return sorted(seen.items())


# --------------------------------------------------------------------------
# Stitching
# --------------------------------------------------------------------------
def _norm(s: str) -> str:
    return " ".join(s.split())


def _strong(s: str) -> bool:
    return len(_norm(s)) >= STRONG_LINE_CHARS


def _align(old: list[str], new: list[str]) -> list[tuple[int, int]]:
    """Index pairs (old i, new j) of lines that are the same text, in order (blank lines and
    GAP entries never pair)."""
    a = [f"\x00a{i}" if (not x.strip() or x == GAP) else _norm(x) for i, x in enumerate(old)]
    b = [f"\x00b{j}" if not x.strip() else _norm(x) for j, x in enumerate(new)]
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return [(m.a + d, m.b + d) for m in sm.get_matching_blocks() for d in range(m.size)]


def _solid(pairs: list[tuple[int, int]], new: list[str]) -> list[tuple[int, int]]:
    """The pairs that can anchor an offset: part of a run of 2+ consecutive pairs, else the
    pairs on a line long enough not to be a look-alike (`}`, `return x`)."""
    have = set(pairs)
    runs = [(i, j) for i, j in pairs if (i + 1, j + 1) in have or (i - 1, j - 1) in have]
    return runs or [(i, j) for i, j in pairs if _strong(new[j])]


def _merge_numbered(f: CodeFile, entries: list[tuple[int, str]]) -> bool:
    """Merge a numbered block into a numbered version; False when the block is other code
    under the same line numbers (a new version)."""
    assert f.num is not None
    acc = f.num
    lo, hi = entries[0][0], entries[-1][0]
    window = [(n, acc[n]) for n in sorted(acc) if lo <= n <= hi + SHIFT_LOOKAHEAD]
    common = [n for n, _ in entries if n in acc]
    d_first = d_last = 0
    if common:
        texts = [t for _, t in entries]
        pairs = _align([t for _, t in window], texts)
        # compare the lines that say something (`}` and `else:` agree in any two programs)
        both = min(sum(1 for n, t in window if n <= hi and _strong(t)), sum(1 for t in texts if _strong(t)))
        if both >= 2 and sum(1 for _, j in pairs if _strong(texts[j])) <= SAME_VERSION_MIN * both:
            return False
        solid = _solid(pairs, [t for _, t in entries])
        if solid:
            d_first = entries[solid[0][1]][0] - window[solid[0][0]][0]
            d_last = entries[solid[-1][1]][0] - window[solid[-1][0]][0]
    new = dict(entries)
    f.edited += sum(1 for n in common if d_last == 0 and _norm(acc[n]) != _norm(new[n]))
    out: dict[int, str] = {}
    for n, t in acc.items():
        if n < lo - max(d_first, 0):
            out[n] = t
        elif n > hi - d_last:
            out[n + d_last] = t  # below the window: renumbered by the lines typed / deleted in it
        elif lo <= n <= hi and d_last == 0 and n not in new:
            out[n] = t  # a line this block skipped: keep what an earlier frame showed
    out.update(new)
    f.num = out
    return True


def _merge_plain(f: CodeFile, body: list[str], *, append: bool) -> bool:
    """Merge a plain block into a plain version where they share lines: the block overwrites
    the window it covers (later frame wins). No shared line: appended after a GAP when
    `append`, else False."""
    assert f.lines is not None
    acc = f.lines
    pairs = _align(acc, body)
    strong = [(i, j) for i, j in pairs if _strong(body[j])]
    small = min(sum(1 for x in acc if x.strip() and x != GAP), sum(1 for x in body if x.strip())) <= 3
    ok = len(strong) >= 2 or (len(strong) == 1 and small)
    if ok:
        (i0, j0), (i1, j1) = strong[0], strong[-1]
        start, end = max(0, i0 - j0), min(len(acc), i1 + 1 + (len(body) - 1 - j1))
        # never write across a hole the block shares no line with
        start = max([start] + [g + 1 for g, x in enumerate(acc[:i0]) if x == GAP])
        end = min([end] + [g for g, x in enumerate(acc) if x == GAP and g > i1])
        ok = end - start <= 2 * len(body) + 5  # scattered look-alike lines, not an overlap
        if ok:
            paired = {j for _, j in pairs}
            f.edited += sum(1 for j in range(j0, j1 + 1) if body[j].strip() and j not in paired)
            f.lines = acc[:start] + body + acc[end:]
            return True
    if not append:
        return False
    f.lines = [*acc, GAP, *body]
    return True


def stitch(blocks: list[CodeBlock]) -> list[CodeFile]:
    """Blocks -> reconstructed files, in order of first appearance."""
    files: list[CodeFile] = []
    for b in sorted(blocks, key=lambda x: (x.n, x.k)):
        if not b.body:
            continue
        name = b.file
        numbered = b.numbered is not None
        same = [f for f in files if f.name == name and (name is not None or f.lang == b.lang)
                and (f.num is not None) == numbered]
        target: CodeFile | None = None
        for f in reversed(same):  # newest version first
            if numbered:
                assert b.numbered is not None
                done = _merge_numbered(f, b.numbered)
            else:
                # a nameless block only joins on shared lines; a named one may follow a gap,
                # but only in the newest version of that file
                done = _merge_plain(f, b.body, append=name is not None and f is same[-1])
            if done:
                target = f
                break
        if target is None:
            target = CodeFile(name=name, lang=b.lang, first_ts=b.ts, last_ts=b.ts,
                              num=dict(b.numbered or []) if numbered else None,
                              lines=None if numbered else list(b.body),
                              version=1 + sum(1 for f in files if f.name == name) if name else 1)
            files.append(target)
        target.refs.append(b.ref)
        target.last_ts = b.ts
    return files


# --------------------------------------------------------------------------
# Filling gaps from the linked repo
# --------------------------------------------------------------------------
_INVISIBLE = dict.fromkeys([0xFE0E, 0xFE0F, 0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF])


def _shown(line: str) -> str:
    """A line as a frame can show it: no trailing space, tabs expanded, NFC, and without the
    characters that draw nothing (emoji variation selectors, zero-width marks, BOM)."""
    return unicodedata.normalize("NFC", line.translate(_INVISIBLE)).rstrip().expandtabs(4)


def _same(a: list[str], b: list[str]) -> bool:
    return len(a) == len(b) and all(_shown(x) == _shown(y) for x, y in zip(a, b))


def _anchor(lines: list[str], *, tail: bool) -> list[str]:
    """Up to ANCHOR_LINES lines next to the gap (the tail of what precedes it, or the head of
    what follows), extended up to ANCHOR_REACH until a strong line is in; [] when none is."""
    for size in range(min(ANCHOR_LINES, len(lines)), min(ANCHOR_REACH, len(lines)) + 1):
        part = lines[-size:] if tail else lines[:size]
        if size and any(_strong(x) for x in part):
            return part
    return []


def _sides(f: CodeFile, key: int) -> tuple[list[str], list[str], int | None]:
    """(lines shown before the gap, lines shown after it, missing line count or None)."""
    if f.num is not None:
        nums = sorted(f.num)
        end = next(n for n in nums if n > key)
        before: list[str] = []
        n = key - 1
        while n in f.num and len(before) < ANCHOR_REACH:
            before.insert(0, f.num[n])
            n -= 1
        after: list[str] = []
        n = end
        while n in f.num and len(after) < ANCHOR_REACH:
            after.append(f.num[n])
            n += 1
        return before, after, end - key
    lines = f.lines or []
    at = [i for i, x in enumerate(lines) if x == GAP][key]
    before = []
    for x in reversed(lines[:at]):
        if x == GAP or len(before) >= ANCHOR_REACH:
            break
        before.insert(0, x)
    after = []
    for x in lines[at + 1:]:
        if x == GAP or len(after) >= ANCHOR_REACH:
            break
        after.append(x)
    return before, after, None


def match_gap(f: CodeFile, key: int, repo: list[str]) -> tuple[int, int] | str:
    """Where the gap's lines are in the repo file: (start index, end index), or the reason
    they cannot be taken from it."""
    before, after, count = _sides(f, key)
    if f.num is not None and key == 1:  # top of the file: only the lines after it, at their line numbers
        assert count is not None
        head = after[:HEAD_ANCHOR_LINES]
        if len(head) < HEAD_ANCHOR_LINES or sum(1 for x in head if _strong(x)) < HEAD_ANCHOR_LINES // 2:
            return "too little code after the gap to check against the repo"
        return (0, count) if _same(repo[count:count + len(head)], head) else "the repo file differs from the video"
    a, b = _anchor(before, tail=True), _anchor(after, tail=False)
    if not a or not b:
        return "too little code around the gap to check against the repo"
    found: list[tuple[int, int]] = []
    for i in range(len(a), len(repo) + 1):
        if not _same(repo[i - len(a):i], a):
            continue
        ends = [i + count] if count is not None else range(i, min(len(repo), i + FILL_MAX_LINES) + 1)
        j = next((j for j in ends if _same(repo[j:j + len(b)], b)), None)
        if j is not None:
            found.append((i, j))
    if len(found) == 1:
        return found[0]
    return "the repo file differs from the video" if not found else "the gap fits several places in the repo file"


def fill_gaps(files: list[CodeFile], lookup: Callable[[str], list[tuple[str, list[str]]]], origin: str) -> None:
    """Fill each remaining gap of each named file from the repo file of the same name.
    `lookup(name)` -> [(repo path, lines)]. A gap is filled only when exactly one text fits
    (several same-named repo files must agree). Records f.fills / f.kept; never touches a
    line the video showed."""
    for f in files:
        keys = [k for k in f.gap_keys() if k not in f.fills]
        if not keys:
            continue
        if f.name is None:
            f.kept.update(dict.fromkeys(keys, "no file name on screen"))
            continue
        cands = lookup(f.name)
        for key in keys:
            if not cands:
                f.kept[key] = f"no {PurePosixPath(f.name).name} in the repo"
                continue
            hits: list[Fill] = []
            why = ""
            for path, lines in cands:
                m = match_gap(f, key, lines)
                if isinstance(m, str):
                    why = why or m
                else:
                    hits.append(Fill(lines[m[0]:m[1]], origin, path, m[0] + 1))
            if hits and all(_same(h.lines, hits[0].lines) for h in hits):
                f.fills[key] = hits[0]
                f.kept.pop(key, None)
            else:
                f.kept[key] = "several repo files fit, with different lines" if hits else why


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
def comment_line(name: str | None, lang: str, text: str) -> str:
    """`text` as one comment line in the file's syntax (extension first, then language)."""
    ext = PurePosixPath(name).suffix.lstrip(".").lower() if name else ""
    stem = PurePosixPath(name).name.lower() if name else ""
    for key in (ext, stem, lang.lower()):
        if key in _HASH:
            return f"# {text}"
        if key in _DASH:
            return f"-- {text}"
        if key in _XML:
            return f"<!-- {text} -->"
        if key in _BLOCK:
            return f"/* {text} */"
        if key in _SEMI:
            return f"; {text}"
        if key in _PCT:
            return f"% {text}"
        if key:
            return f"// {text}"
    return f"# {text}"


def _ranges(rs: list[tuple[int, int]]) -> str:
    return ", ".join(f"{a}-{b}" if b > a else str(a) for a, b in rs)


def _span(a: int, b: int) -> str:
    return f"lines {a}-{b}" if b > a else f"line {a}"


def _fill_lines(f: CodeFile, fill: Fill, what: str) -> list[str]:
    if not fill.lines:
        return []  # the repo file has nothing between the two sides: no hole
    src = f"{fill.origin} {fill.path}"
    return [comment_line(f.name, f.lang, f"[{what} from repo {src}, not shown in the video]"), *fill.lines,
            comment_line(f.name, f.lang, "[end of lines from repo]")]


def file_body(f: CodeFile) -> list[str]:
    """The file's lines; per hole one gap comment, or the repo's lines between two comments."""
    out: list[str] = []
    if f.num is not None:
        prev = 0
        for n in sorted(f.num):
            if n > prev + 1:
                fill = f.fills.get(prev + 1)
                out += (_fill_lines(f, fill, _span(prev + 1, n - 1)) if fill else
                        [comment_line(f.name, f.lang, f"[gap: {_span(prev + 1, n - 1)} not shown in the video]")])
            out.extend(f.num[n].split("\n"))
            prev = n
        return out
    g = 0
    for x in f.lines or []:
        if x != GAP:
            out.append(x)
            continue
        fill = f.fills.get(g)
        out += (_fill_lines(f, fill, _span(fill.first, fill.first + len(fill.lines) - 1)) if fill else
                [comment_line(f.name, f.lang, "[gap: lines not shown in the video]")])
        g += 1
    return out


def section_header(i: int, f: CodeFile) -> str:
    name = f.name or "?"
    if f.version > 1:
        name += f" (version {f.version})"
    gaps = f.gaps()
    if f.num is not None:
        assert isinstance(gaps, list)
        left = [(a, b) for a, b in gaps if a not in f.fills]
        where = f"lines {min(f.num)}-{max(f.num)}"
        gap_txt = f"gaps: lines {_ranges(left)}" if left else "gaps: none"
        filled = _ranges([(a, b) for a, b in gaps if a in f.fills])
    else:
        assert isinstance(gaps, int)
        left_n = gaps - len(f.fills)
        where = f"{sum(1 for x in f.lines or [] if x != GAP)} lines, no line numbers on screen"
        gap_txt = f"gaps: {left_n} (no shared line between frames)" if left_n else "gaps: none found"
        filled = ", ".join(str(len(x.lines)) for x in f.fills.values())
    shown = f.first_ts if f.first_ts == f.last_ts else f"{f.first_ts}-{f.last_ts}"
    parts = [f"{i:02d} {name}", f.lang, shown, where, gap_txt]
    if f.fills:
        one = next(iter(f.fills.values()))
        parts.append(f"filled from repo {one.origin} {one.path}: lines {filled}")
    if f.kept:
        parts.append("repo not used: " + "; ".join(dict.fromkeys(f.kept.values())))
    if f.edited:
        parts.append(f"{f.edited} lines changed between frames (latest kept)")
    parts.append("from CODE" + ",".join(f.refs))
    return "=== " + " | ".join(parts) + " ==="


def render_code_md(key: str, vtag: str, version: str, files: list[CodeFile]) -> str:
    out = [f"{CODE_HEADER_PREFIX}{key} {vtag} skill={version} files={len(files)}"]
    for i, f in enumerate(files, start=1):
        out.append(section_header(i, f))
        out.extend(file_body(f))
    return "\n".join(out) + "\n"


def render_blocks_md(blocks: list[CodeBlock]) -> str:
    """The raw blocks as the readers wrote them (code-blocks.md: earlier states of edited code)."""
    out: list[str] = []
    for b in sorted(blocks, key=lambda x: (x.n, x.k)):
        out.append(b.header)
        out.extend(b.body)
    return "\n".join(out) + "\n"
