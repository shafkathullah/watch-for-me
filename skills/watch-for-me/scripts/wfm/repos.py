"""`--code`: the source repo a video's description links (GitHub / GitLab), fetched read-only
into the cache so gaps in the stitched code can be filled from it (codefiles.fill_gaps).

The video is the map of which files matter; the repo only fills lines the video never showed,
and only where the lines around the gap match (anchors). Bounds, all enforced here:
- links come from the video's own description (meta.json), never from the agent: the
  `repo-fill` subcommand takes a run id, nothing else
- https only, hosts github.com / gitlab.com, redirects only to those hosts' download servers
- ONE commit as a .tar.gz archive (no git, so no hooks, no submodules, no history, no LFS)
- download size cap, per-file cap, file-count cap, total-bytes cap, wall-clock deadline
- only regular text files are written, mode 0644, under <key>/repos/<slug>/tree/; symlinks,
  devices, absolute and `..` paths, `.git/` and binaries are skipped
- nothing from the repo is ever executed or imported
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import IO, Any

MAX_REPOS = 3  # links kept per description
REPO_HOSTS = ("github.com", "gitlab.com")
REDIRECT_HOSTS = ("github.com", "codeload.github.com", "gitlab.com")
DOWNLOAD_MAX_BYTES = 60_000_000  # compressed archive
EXTRACT_MAX_BYTES = 120_000_000  # text written to disk
FILE_MAX_BYTES = 1_000_000
MAX_FILES = 20_000
DEADLINE_S = 60.0
SOCKET_TIMEOUT_S = 15.0
CHUNK = 1 << 16
REPOS_DIR = "repos"
TREE_DIR = "tree"
USER_AGENT = "watch-for-me"

_LINK_RE = re.compile(r"(?<![\w.@/-])(?:(https?)://)?(?:www\.)?(github\.com|gitlab\.com)/([^\s<>\"'`\\|]+)", re.IGNORECASE)
_TRAIL = ".,;:!?)]}>'\"*"
_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_REF_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# first path segments that are site pages, not owners
_GITHUB_RESERVED = {
    "about", "account", "apps", "blog", "collections", "contact", "customer-stories", "enterprise", "events",
    "explore", "features", "issues", "join", "login", "logout", "marketplace", "new", "notifications", "orgs",
    "organizations", "pricing", "pulls", "readme", "search", "security", "settings", "site", "sponsors",
    "stars", "topics", "trending", "users", "watching", "codespaces", "discussions", "copilot", "resources",
}
_GITLAB_RESERVED = {"-", "admin", "api", "dashboard", "explore", "groups", "help", "projects", "search",
                    "snippets", "users", "public", "profile", "oauth", "import"}
_GITLAB_NOT_PROJECT = {"issues", "merge_requests", "snippets", "wikis", "pipelines", "commit", "commits", "blob",
                       "tree", "tags", "branches", "releases", "jobs"}


class RepoError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RepoLink:
    host: str  # github.com | gitlab.com
    path: str  # owner/repo, or group/sub/project
    ref: str | None = None  # from a /tree/<ref> or /blob/<ref> link

    @property
    def url(self) -> str:
        return f"https://{self.host}/{self.path}"

    @property
    def slug(self) -> str:
        return re.sub(r"[^A-Za-z0-9._-]", "_", f"{self.host}__{self.path.replace('/', '__')}")

    @property
    def label(self) -> str:
        return f"{self.host}/{self.path}"


@dataclass
class Snapshot:
    url: str
    commit: str
    ref: str | None
    dir: str  # .../repos/<slug>/tree
    files: int = 0
    skipped: int = 0
    bytes: int = 0
    truncated: bool = False
    fetched: str = ""
    cached: bool = field(default=False, compare=False)


# --------------------------------------------------------------------------
# Link detection (pure)
# --------------------------------------------------------------------------
def find_repos(text: str | None, limit: int = MAX_REPOS) -> list[RepoLink]:
    """Repository links in a video description, in order, deduplicated (case-insensitive), at
    most `limit`. A link counts when it is the repo itself (`/owner/repo`, `.git`, trailing
    slash) or a tree / blob page in it; issue, pull, release, wiki, gist and profile links do
    not name a source to read and are ignored. `http://` and scheme-less links are kept as
    https (the only scheme ever fetched)."""
    out: list[RepoLink] = []
    seen: set[str] = set()
    for m in _LINK_RE.finditer(text or ""):
        host = m.group(2).lower()
        raw = re.split(r"[?#]", m.group(3), maxsplit=1)[0].rstrip(_TRAIL)
        while raw.endswith(("/", *_TRAIL)):
            raw = raw.rstrip("/").rstrip(_TRAIL)
        segs = [s for s in raw.split("/") if s]
        link = _github(segs) if host == "github.com" else _gitlab(segs)
        if link is None or link.url.lower() in seen:
            continue
        seen.add(link.url.lower())
        out.append(link)
        if len(out) >= limit:
            break
    return out


def _ref(segs: list[str]) -> str | None:
    return segs[0] if segs and _REF_RE.match(segs[0]) and not segs[0].startswith(".") else None


def _github(segs: list[str]) -> RepoLink | None:
    if len(segs) < 2:
        return None
    owner, name, rest = segs[0], segs[1].removesuffix(".git"), segs[2:]
    if owner.lower() in _GITHUB_RESERVED or not _OWNER_RE.match(owner) or not _NAME_RE.match(name):
        return None
    if name in (".", "..") or (rest and rest[0] not in ("tree", "blob")):
        return None
    return RepoLink("github.com", f"{owner}/{name}", _ref(rest[1:]) if rest else None)


def _gitlab(segs: list[str]) -> RepoLink | None:
    rest: list[str] = []
    if "-" in segs:
        i = segs.index("-")
        segs, rest = segs[:i], segs[i + 1:]
    if not 2 <= len(segs) <= 6 or segs[0].lower() in _GITLAB_RESERVED:
        return None
    segs = [*segs[:-1], segs[-1].removesuffix(".git")]
    if any(not _NAME_RE.match(s) or s in (".", "..") for s in segs) or _GITLAB_NOT_PROJECT & set(segs[1:]):
        return None
    if rest and rest[0] not in ("tree", "blob"):
        return None
    return RepoLink("gitlab.com", "/".join(segs), _ref(rest[1:]) if rest else None)


def archive_url(link: RepoLink, ref: str | None = None) -> str:
    """The one URL fetched for a link: that commit's .tar.gz on the link's own host."""
    r = urllib.parse.quote(ref or "HEAD", safe="")
    if link.host == "github.com":
        return f"https://github.com/{link.path}/archive/{r}.tar.gz"
    name = link.path.rsplit("/", 1)[-1]
    return f"https://gitlab.com/{link.path}/-/archive/{r}/{name}-{r}.tar.gz"


def check_url(url: str, hosts: tuple[str, ...] = REPO_HOSTS) -> str:
    """url unchanged when it is https on an allowed host (no userinfo, no port), else RepoError."""
    p = urllib.parse.urlsplit(url)
    if p.scheme != "https" or (p.hostname or "").lower() not in hosts or p.username or p.password or p.port:
        raise RepoError("repo_blocked", f"refusing {url}: only https on {', '.join(hosts)}")
    return url


class _Redirects(urllib.request.HTTPRedirectHandler):
    max_redirections = 4

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        check_url(newurl, REDIRECT_HOSTS)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_archive(url: str) -> IO[bytes]:
    """GET `url` (https, allowed host, checked redirects, no credentials, no proxy auth)."""
    import ssl

    check_url(url)
    try:
        import certifi

        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    opener = urllib.request.build_opener(_Redirects(), urllib.request.HTTPSHandler(context=ctx))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/gzip"})
    return opener.open(req, timeout=SOCKET_TIMEOUT_S)


# --------------------------------------------------------------------------
# Fetch (bounded) + extract (safe)
# --------------------------------------------------------------------------
def repo_root(key_dir: Path, link: RepoLink) -> Path:
    return Path(key_dir) / REPOS_DIR / link.slug


def load_snapshot(key_dir: Path, link: RepoLink) -> Snapshot | None:
    root = repo_root(key_dir, link)
    try:
        d = json.loads((root / "repo.json").read_text(encoding="utf-8"))
        snap = Snapshot(**{k: d[k] for k in ("url", "commit", "ref", "dir", "files", "skipped", "bytes",
                                             "truncated", "fetched")})
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if snap.dir != str(root / TREE_DIR) or not Path(snap.dir).is_dir():
        return None  # cache moved or half-written: fetch again
    snap.cached = True
    return snap


def fetch(link: RepoLink, key_dir: Path, *, fresh: bool = False,
          opener: Callable[[str], IO[bytes]] = open_archive,
          download_max: int = DOWNLOAD_MAX_BYTES, deadline_s: float = DEADLINE_S, **limits: int) -> Snapshot:
    """One commit of `link` under <key_dir>/repos/<slug>/tree (cached: fetched once per video).
    The ref of a tree / blob link is tried first, then the default branch. RepoError codes:
    repo_blocked, repo_unreachable, repo_too_large, repo_timeout, repo_bad_archive."""
    if not fresh:
        snap = load_snapshot(key_dir, link)
        if snap is not None:
            return snap
    root = repo_root(key_dir, link)
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / f".archive.{os.getpid()}.tar.gz"
    stage = root / f".tree.{os.getpid()}"
    try:
        err: RepoError | None = None
        for ref in ([link.ref, None] if link.ref else [None]):
            try:
                _download(archive_url(link, ref), tmp, opener, download_max, time.monotonic() + deadline_s)
                err = None
                break
            except RepoError as e:
                err = e
                if e.code != "repo_unreachable":
                    break
        if err is not None:
            raise err
        shutil.rmtree(stage, ignore_errors=True)
        commit, stats = extract(tmp, stage, **limits)
        tree = root / TREE_DIR
        shutil.rmtree(tree, ignore_errors=True)
        os.replace(stage, tree)
        snap = Snapshot(url=link.url, commit=commit, ref=ref, dir=str(tree), fetched=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **stats)
        (root / "repo.json").write_text(json.dumps(asdict(snap), indent=1) + "\n", encoding="utf-8")
        return snap
    finally:
        tmp.unlink(missing_ok=True)
        shutil.rmtree(stage, ignore_errors=True)
        for d in (root, root.parent):  # a failed fetch leaves no empty directories
            try:
                d.rmdir()
            except OSError:
                break


def _download(url: str, dest: Path, opener: Callable[[str], IO[bytes]], cap: int, deadline: float) -> None:
    try:
        with opener(url) as resp, open(dest, "wb") as out:
            total = 0
            while True:
                if time.monotonic() > deadline:
                    raise RepoError("repo_timeout", f"{url}: download took too long")
                chunk = resp.read(CHUNK)
                if not chunk:
                    return
                total += len(chunk)
                if total > cap:
                    raise RepoError("repo_too_large", f"{url}: archive over {cap // 1_000_000} MB")
                out.write(chunk)
    except RepoError:
        raise
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise RepoError("repo_unreachable", f"{url}: {type(e).__name__}: {e}") from None


def extract(archive: Path, dest: Path, *, file_max: int = FILE_MAX_BYTES, max_files: int = MAX_FILES,
            total_max: int = EXTRACT_MAX_BYTES) -> tuple[str, dict[str, Any]]:
    """Write the archive's regular text files under dest (top directory stripped), mode 0644.
    Skipped: links, devices, absolute / `..` / `.git` paths, files over file_max, binaries.
    Stops (truncated) at max_files or total_max. -> (commit sha or "unknown", stats)."""
    files = skipped = total = 0
    truncated = False
    commit = "unknown"
    try:
        with tarfile.open(archive, "r:gz") as tf:
            sha = str(tf.pax_headers.get("comment") or "")
            if _SHA_RE.match(sha):
                commit = sha
            base = dest.resolve()
            for m in tf:
                parts = PurePosixPath(m.name).parts[1:]  # strip <repo>-<ref>/
                if not m.isfile() or not parts or m.name.startswith("/") or any(
                        p in ("..", ".", ".git") or "\\" in p or "\x00" in p for p in parts):
                    skipped += not m.isdir()
                    continue
                if m.size > file_max:
                    skipped += 1
                    continue
                if files >= max_files or total + m.size > total_max:
                    truncated = True
                    break
                src = tf.extractfile(m)
                data = src.read(file_max + 1) if src is not None else b""
                if len(data) > file_max or b"\x00" in data[:8192]:
                    skipped += 1
                    continue
                out = base.joinpath(*parts)
                if base not in out.resolve().parents:
                    skipped += 1
                    continue
                out.parent.mkdir(parents=True, exist_ok=True)
                with open(out, "wb") as fh:
                    fh.write(data)
                os.chmod(out, 0o644)
                files += 1
                total += len(data)
    except (tarfile.TarError, EOFError, OSError) as e:
        raise RepoError("repo_bad_archive", f"{archive.name}: {type(e).__name__}: {e}") from None
    dest.mkdir(parents=True, exist_ok=True)
    return commit, {"files": files, "skipped": int(skipped), "bytes": total, "truncated": truncated}


def find_files(tree: str | Path, name: str, limit: int = 20) -> list[str]:
    """Paths (relative, posix, shortest first) of the files called `name` in a fetched tree:
    exact name first, else case-insensitive."""
    base = Path(tree)
    want = PurePosixPath(name.replace("\\", "/")).name
    exact: list[str] = []
    loose: list[str] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames.sort()
        for fn in sorted(filenames):
            rel = (Path(dirpath) / fn).relative_to(base).as_posix()
            if fn == want:
                exact.append(rel)
            elif fn.lower() == want.lower():
                loose.append(rel)
    hits = exact or loose
    return sorted(hits, key=lambda p: (p.count("/"), p))[:limit]


def read_lines(tree: str | Path, rel: str) -> list[str]:
    return (Path(tree) / rel).read_text(encoding="utf-8", errors="replace").splitlines()
