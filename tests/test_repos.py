"""wfm.repos (repo links in a description, bounded archive fetch, safe extraction) and
codefiles.fill_gaps (anchor-checked gap filling) + the `repo-fill` subcommand. No network:
archives are built with tarfile and served by a local opener."""

from __future__ import annotations

import io
import sys
import tarfile
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"
if str(FIXTURES) not in sys.path:
    sys.path.insert(0, str(FIXTURES))

import make_code_scroll as fx
from wfm import cache, cli, plan, repos
from wfm import codefiles as cf

INV = fx.source_lines("inventory.py")
SHA = "0123456789abcdef0123456789abcdef01234567"
KEYFRAMES = [0.0, 7.0, 14.0, 21.0, 28.0, 35.0, 42.0, 49.05, 56.1]


# --------------------------------------------------------------------------
# link detection
# --------------------------------------------------------------------------
def _urls(text: str) -> list[str]:
    return [r.url for r in repos.find_repos(text)]


def test_find_repos_forms_and_punctuation() -> None:
    assert _urls("Code: https://github.com/acme/ledger") == ["https://github.com/acme/ledger"]
    assert _urls("Source (https://github.com/acme/ledger).") == ["https://github.com/acme/ledger"]
    assert _urls("see https://github.com/acme/ledger, thanks!") == ["https://github.com/acme/ledger"]
    assert _urls("git clone https://github.com/acme/ledger.git") == ["https://github.com/acme/ledger"]
    assert _urls("https://github.com/acme/ledger/") == ["https://github.com/acme/ledger"]
    assert _urls("http://www.github.com/acme/ledger?tab=readme#usage") == ["https://github.com/acme/ledger"]
    assert _urls("repo: github.com/acme/my.repo-2") == ["https://github.com/acme/my.repo-2"]
    (r,) = repos.find_repos("https://github.com/acme/ledger/tree/part-3/src")
    assert (r.path, r.ref, r.slug, r.label) == ("acme/ledger", "part-3", "github.com__acme__ledger",
                                                 "github.com/acme/ledger")
    (r,) = repos.find_repos("https://github.com/acme/ledger/blob/main/src/inventory.py#L10")
    assert (r.url, r.ref) == ("https://github.com/acme/ledger", "main")
    (r,) = repos.find_repos("https://gitlab.com/group/sub/project/-/tree/v2/app")
    assert (r.host, r.path, r.ref) == ("gitlab.com", "group/sub/project", "v2")
    assert _urls("https://gitlab.com/group/project.git") == ["https://gitlab.com/group/project"]


def test_find_repos_ignores_non_repo_links() -> None:
    for text in ("https://github.com/acme/ledger/issues/12", "https://github.com/acme/ledger/pull/3",
                 "https://github.com/acme/ledger/releases/tag/v1", "https://github.com/acme/ledger/wiki",
                 "https://gist.github.com/acme/0123456789abcdef", "https://github.com/acme",
                 "https://github.com/sponsors/acme", "https://github.com/orgs/acme/people",
                 "https://github.com/topics/python", "https://raw.githubusercontent.com/acme/ledger/main/a.py",
                 "https://acme.github.io/ledger", "git@github.com:acme/ledger.git",
                 "https://gitlab.com/group/project/-/issues/4", "https://gitlab.com/explore/projects",
                 "https://gitlab.com/group/project/-/merge_requests/2", "https://notgithub.com/acme/ledger",
                 "https://bitbucket.org/acme/ledger", "https://youtu.be/abc", "", None):
        assert repos.find_repos(text) == [], text


def test_find_repos_multiple_dedupe_and_cap() -> None:
    text = ("Code https://github.com/acme/ledger and the UI https://gitlab.com/acme/ui\n"
            "again HTTPS://GITHUB.COM/Acme/Ledger/tree/main, docs https://github.com/acme/docs "
            "extra https://github.com/acme/four https://github.com/acme/five")
    assert _urls(text) == ["https://github.com/acme/ledger", "https://gitlab.com/acme/ui",
                           "https://github.com/acme/docs"]  # capped at MAX_REPOS, first mention wins
    assert len(repos.find_repos(text, limit=10)) == 5


def test_context_md_lists_repos() -> None:
    meta = {"title": "t", "description": "Full code: https://github.com/acme/ledger\nMore: https://x.example/a"}
    text = plan.render_context_md(meta, "full-1080", None, None, {})
    assert "repos: https://github.com/acme/ledger\n" in text
    assert "github.com" not in text.split("description: ")[1].split("\n")[0]  # URLs still stripped there
    assert "repos:" not in plan.render_context_md({"title": "t", "description": "no links"}, "v", None, None, {})


# --------------------------------------------------------------------------
# fetch bounds (local archives, no network)
# --------------------------------------------------------------------------
def _archive(members: list[tuple[str, bytes | None, str]], sha: str | None = SHA) -> bytes:
    """members: (name, data, kind) with kind file | symlink | dir."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT,
                      pax_headers={"comment": sha} if sha else None) as tf:
        for name, data, kind in members:
            ti = tarfile.TarInfo(name)
            if kind == "symlink":
                ti.type, ti.linkname = tarfile.SYMTYPE, "/etc/passwd"
                tf.addfile(ti)
            elif kind == "dir":
                ti.type = tarfile.DIRTYPE
                tf.addfile(ti)
            else:
                ti.size, ti.mode = len(data or b""), 0o755
                tf.addfile(ti, io.BytesIO(data or b""))
    return buf.getvalue()


def _opener(payloads: dict[str, bytes], calls: list[str]):
    def open_(url: str) -> io.BytesIO:
        calls.append(url)
        if url not in payloads:
            import urllib.error

            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)  # type: ignore[arg-type]
        return io.BytesIO(payloads[url])
    return open_


LINK = repos.RepoLink("github.com", "acme/ledger")
HEAD_URL = "https://github.com/acme/ledger/archive/HEAD.tar.gz"


def test_archive_url_and_host_rules() -> None:
    assert repos.archive_url(LINK) == HEAD_URL
    assert repos.archive_url(LINK, "part-3") == "https://github.com/acme/ledger/archive/part-3.tar.gz"
    assert repos.archive_url(repos.RepoLink("gitlab.com", "g/sub/p")) == \
        "https://gitlab.com/g/sub/p/-/archive/HEAD/p-HEAD.tar.gz"
    assert repos.check_url(HEAD_URL) == HEAD_URL
    for bad in ("http://github.com/a/b/archive/HEAD.tar.gz", "https://evil.example/a/b.tar.gz",
                "https://user:pw@github.com/a/b", "https://github.com:8443/a/b", "file:///etc/passwd",
                "https://github.com.evil.example/a/b", "ssh://github.com/a/b"):
        with pytest.raises(repos.RepoError) as e:
            repos.check_url(bad)
        assert e.value.code == "repo_blocked"
    # redirects: only to the hosts' own download servers, https only
    h = repos._Redirects()
    for bad in ("https://evil.example/x.tar.gz", "http://codeload.github.com/a/b/tar.gz/HEAD"):
        with pytest.raises(repos.RepoError):
            h.redirect_request(None, None, 302, "Found", {}, bad)
    assert repos.check_url("https://codeload.github.com/a/b/tar.gz/HEAD", repos.REDIRECT_HOSTS)


def test_fetch_extracts_text_only_and_caches(tmp_path: Path) -> None:
    data = _archive([
        ("ledger-abc", None, "dir"),
        ("ledger-abc/src/inventory.py", "\n".join(INV).encode(), "file"),
        ("ledger-abc/README.md", b"# ledger\n", "file"),
        ("ledger-abc/logo.png", b"\x89PNG\x00\x00binary", "file"),
        ("ledger-abc/link.py", None, "symlink"),
        ("ledger-abc/../../escape.py", b"x = 1\n", "file"),
        ("/abs/escape.py", b"x = 1\n", "file"),
        ("ledger-abc/.git/hooks/pre-commit", b"#!/bin/sh\nrm -rf ~\n", "file"),
        ("ledger-abc/big.txt", b"a" * 50_000, "file"),
    ])
    calls: list[str] = []
    snap = repos.fetch(LINK, tmp_path, opener=_opener({HEAD_URL: data}, calls), file_max=20_000)
    tree = Path(snap.dir)
    assert tree == tmp_path / "repos" / "github.com__acme__ledger" / "tree" and calls == [HEAD_URL]
    assert sorted(p.relative_to(tree).as_posix() for p in tree.rglob("*") if p.is_file()) == \
        ["README.md", "src/inventory.py"]
    assert (snap.commit, snap.files, snap.skipped, snap.truncated, snap.cached) == (SHA, 2, 6, False, False)
    assert (tree / "src" / "inventory.py").stat().st_mode & 0o777 == 0o644  # never executable
    assert not (tmp_path.parent / "escape.py").exists() and not Path("/abs/escape.py").exists()
    assert repos.find_files(tree, "inventory.py") == ["src/inventory.py"]
    assert repos.find_files(tree, "Inventory.PY") == ["src/inventory.py"] and repos.find_files(tree, "x.py") == []
    assert repos.read_lines(tree, "src/inventory.py") == INV
    # second call: served from the cache, no request
    again = repos.fetch(LINK, tmp_path, opener=_opener({}, calls))
    assert again.cached and again.commit == SHA and calls == [HEAD_URL]
    # no leftovers
    assert sorted(p.name for p in tree.parent.iterdir()) == ["repo.json", "tree"]


def test_fetch_bounds(tmp_path: Path) -> None:
    many = _archive([(f"r/f{i}.py", b"x = 1\n", "file") for i in range(30)])
    snap = repos.fetch(LINK, tmp_path / "a", opener=_opener({HEAD_URL: many}, []), max_files=10)
    assert snap.files == 10 and snap.truncated
    snap = repos.fetch(LINK, tmp_path / "b", opener=_opener({HEAD_URL: many}, []), total_max=20)
    assert snap.files == 3 and snap.truncated  # 6 bytes each
    with pytest.raises(repos.RepoError) as e:  # compressed size cap, checked while streaming
        repos.fetch(LINK, tmp_path / "c", opener=_opener({HEAD_URL: many}, []), download_max=100)
    assert e.value.code == "repo_too_large"
    with pytest.raises(repos.RepoError) as e:
        repos.fetch(LINK, tmp_path / "d", opener=_opener({HEAD_URL: many}, []), deadline_s=-1.0)
    assert e.value.code == "repo_timeout"
    with pytest.raises(repos.RepoError) as e:
        repos.fetch(LINK, tmp_path / "e", opener=_opener({HEAD_URL: b"not a tarball"}, []))
    assert e.value.code == "repo_bad_archive"
    with pytest.raises(repos.RepoError) as e:
        repos.fetch(LINK, tmp_path / "f", opener=_opener({}, []))
    assert e.value.code == "repo_unreachable"
    for d in "cdef":  # failures leave nothing behind
        assert not (tmp_path / d).exists() or not any((tmp_path / d).iterdir())
    # a tree link's ref is tried first, then the default branch
    calls: list[str] = []
    ref = repos.RepoLink("github.com", "acme/ledger", "part-3")
    snap = repos.fetch(ref, tmp_path / "g", opener=_opener({HEAD_URL: many}, calls))
    assert calls == ["https://github.com/acme/ledger/archive/part-3.tar.gz", HEAD_URL] and snap.ref is None
    assert repos.fetch(LINK, tmp_path / "h", opener=_opener({HEAD_URL: _archive([("r/a.py", b"x\n", "file")], None)},
                                                            [])).commit == "unknown"


# --------------------------------------------------------------------------
# fill / refuse
# --------------------------------------------------------------------------
def _files(numbered: bool) -> list[cf.CodeFile]:
    blocks = []
    for i, t in enumerate(KEYFRAMES):
        name, a, b = fx.visible_lines(t)
        src = fx.source_lines(name)
        head = f"CODE#{i + 1} x 00:{int(t):02d} file={name}"
        blocks.append("\n".join([f"{head} lines={a}-{b}", *(f"{n}|{src[n - 1]}" for n in range(a, b + 1))])
                      if numbered else "\n".join([head, *src[a - 1:b]]))
    return cf.stitch(cf.split_visual("\n".join(blocks) + "\nEND\n")[1])


def _fill(files: list[cf.CodeFile], repo_files: dict[str, list[str]]) -> None:
    cf.fill_gaps(files, lambda name: [(p, x) for p, x in repo_files.items() if p.endswith(name)],
                 "github.com/acme/ledger@0123456789ab")


@pytest.mark.parametrize("numbered", [True, False])
def test_fill_from_matching_repo_file(numbered: bool) -> None:
    files = _files(numbered)
    inv = files[0]
    assert len(inv.gap_keys()) == 1
    _fill(files, {"src/inventory.py": INV})
    body = cf.file_body(inv)
    assert body[63] == ("# [lines 64-90 from repo github.com/acme/ledger@0123456789ab src/inventory.py, "
                        "not shown in the video]")
    assert body[91] == "# [end of lines from repo]"
    assert [*body[:63], *body[64:91], *body[92:]] == INV  # the 27 lines, nothing else changed
    assert inv.kept == {} and len(next(iter(inv.fills.values())).lines) == 27
    head = cf.section_header(1, inv)
    assert "filled from repo github.com/acme/ledger@0123456789ab src/inventory.py: lines" in head
    assert "gaps: none" in head


@pytest.mark.parametrize("numbered", [True, False])
def test_refuse_when_repo_differs_around_the_gap(numbered: bool) -> None:
    for changed in (62, 90):  # the line just before / just after the gap is different in the repo
        files = _files(numbered)
        other = list(INV)
        other[changed] = other[changed] + "  # changed upstream"
        _fill(files, {"src/inventory.py": other})
        inv = files[0]
        assert inv.fills == {} and list(inv.kept.values()) == ["the repo file differs from the video"]
        body = cf.file_body(inv)
        assert [x for x in body if "from repo" in x] == [] and len(body) == 177  # the gap comment stays
        assert "repo not used: the repo file differs from the video" in cf.section_header(1, inv)


def test_refuse_numbered_gap_of_another_length_and_other_cases() -> None:
    files = _files(True)
    longer = [*INV[:70], "        extra_line = 1", *INV[70:]]  # anchors match, 28 lines between them
    _fill(files, {"src/inventory.py": longer})
    assert files[0].fills == {} and "differs" in next(iter(files[0].kept.values()))
    # no such file in the repo
    files = _files(True)
    _fill(files, {"src/other.py": INV})
    assert files[0].kept == {64: "no inventory.py in the repo"}
    # two same-named repo files: filled when they agree, refused when both fit with other lines
    files = _files(True)
    _fill(files, {"a/inventory.py": INV, "b/inventory.py": INV})
    assert 64 in files[0].fills
    files = _files(True)
    alt = [*INV[:70], "        item.on_hand += units * 2", *INV[71:]]
    _fill(files, {"a/inventory.py": INV, "b/inventory.py": alt})
    assert files[0].fills == {} and files[0].kept == {64: "several repo files fit, with different lines"}
    # a nameless snippet is never looked up
    files = cf.stitch(cf.split_visual("CODE#1 python 00:01 lines=5-6\n5|a = 1\n6|b = 2\nEND\n")[1])
    _fill(files, {"x.py": ["a = 1"]})
    assert files[0].kept == {1: "no file name on screen"}
    # a file the video never showed is never copied
    files = _files(True)
    _fill(files, {"src/inventory.py": INV, "src/secrets.py": ["TOKEN = 'x'"]})
    assert [f.name for f in files] == ["inventory.py", "settings.toml"]


def test_fill_top_of_file_needs_a_long_anchor_at_its_line_numbers() -> None:
    def top(first: int, last: int) -> list[cf.CodeFile]:
        body = "\n".join(f"{n}|{INV[n - 1]}" for n in range(first, last + 1))
        return cf.stitch(cf.split_visual(f"CODE#1 python 00:01 file=inventory.py lines={first}-{last}\n{body}\nEND\n")[1])
    files = top(20, 60)
    _fill(files, {"inventory.py": INV})
    assert cf.file_body(files[0])[1:20] == INV[:19] and files[0].fills[1].first == 1
    files = top(20, 60)
    _fill(files, {"inventory.py": ["# licence header", *INV]})  # same code, one line lower
    assert files[0].fills == {} and files[0].kept == {1: "the repo file differs from the video"}
    files = top(20, 23)
    _fill(files, {"inventory.py": INV})
    assert files[0].kept == {1: "too little code after the gap to check against the repo"}


def test_plain_gap_with_nothing_missing_closes() -> None:
    a = "CODE#1 python 00:01 file=a.py\ndef alpha():\n    return compute_alpha(1)"
    b = "CODE#2 python 00:02 file=a.py\ndef beta():\n    return compute_beta(2)"
    files = cf.stitch(cf.split_visual(a + "\n" + b + "\nEND\n")[1])
    assert files[0].gaps() == 1
    _fill(files, {"a.py": ["def alpha():", "    return compute_alpha(1)", "def beta():", "    return compute_beta(2)"]})
    assert cf.file_body(files[0]) == ["def alpha():", "    return compute_alpha(1)", "def beta():",
                                      "    return compute_beta(2)"]


# --------------------------------------------------------------------------
# `repo-fill` subcommand
# --------------------------------------------------------------------------
def _run_with_gap(wfm_cache: Path, *, description: str | None, flags: dict, local: bool = False) -> cache.ViewPaths:
    kp = cache.key_paths("youtube-x")
    view = kp.view("full-1080")
    view.dir.mkdir(parents=True)
    meta: dict = {"title": "t", "description": description}
    if local:
        meta["local_path"] = "/v.mp4"
    cache.atomic_write_json(kp.meta, meta)
    files = _files(True)
    blocks = cf.split_visual("\n".join(
        "\n".join([f"CODE#{i + 1} python 00:0{i} file=inventory.py lines={a}-{b}",
                   *(f"{n}|{INV[n - 1]}" for n in range(a, b + 1))]) for i, (a, b) in enumerate([(1, 63), (91, 203)])
    ) + "\nEND\n")[1]
    assert files and blocks
    cache.atomic_write_text(view.code_blocks_md, cf.render_blocks_md(blocks))
    rdir = cache.run_dir("wfmrepo1")
    cache.atomic_write_json(rdir / "plan.json", {"views": {"youtube-x": str(view.dir)}})
    cache.atomic_write_json(rdir / "run.json", {"flags": flags})
    return view


def test_repo_fill_command(wfm_cache: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    view = _run_with_gap(wfm_cache, description="code: https://github.com/acme/ledger", flags={"code": True})
    data = _archive([("ledger-x/src/inventory.py", "\n".join(INV).encode(), "file")])
    calls: list[str] = []
    monkeypatch.setattr(repos, "open_archive", _opener({HEAD_URL: data}, calls))
    monkeypatch.setattr(repos.fetch, "__kwdefaults__", {**repos.fetch.__kwdefaults__, "opener": repos.open_archive})
    assert cli.main(["repo-fill", "--run", "wfmrepo1"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["repo youtube-x https://github.com/acme/ledger commit 0123456789ab files=1",
                   "filled youtube-x inventory.py lines 64-90 (27 lines) from src/inventory.py",
                   f"code {view.code_md}"]
    text = view.code_md.read_text()
    assert "| gaps: none | filled from repo github.com/acme/ledger@0123456789ab src/inventory.py: lines 64-90 |" in text
    assert text.count("not shown in the video]") == 1 and calls == [HEAD_URL]
    assert cli.main(["repo-fill", "--run", "nope0001"]) == 2


@pytest.mark.parametrize(("description", "flags", "local", "why"), [
    ("code: https://github.com/acme/ledger", {"code": True, "no_repo": True}, False, "--no-repo"),
    ("code: https://github.com/acme/ledger", {"code": False}, False, "not a --code run"),
    ("no links here", {"code": True}, False, "no repo linked"),
    ("code: https://github.com/acme/ledger", {"code": True}, True, "no repo linked"),  # local file
])
def test_repo_fill_never_fetches_when_off(wfm_cache: Path, capsys: pytest.CaptureFixture[str],
                                          monkeypatch: pytest.MonkeyPatch, description: str, flags: dict,
                                          local: bool, why: str) -> None:
    _run_with_gap(wfm_cache, description=description, flags=flags, local=local)

    def boom(url: str) -> None:
        raise AssertionError(f"network call: {url}")
    monkeypatch.setattr(repos.fetch, "__kwdefaults__", {**repos.fetch.__kwdefaults__, "opener": boom})
    assert cli.main(["repo-fill", "--run", "wfmrepo1"]) == 0
    assert capsys.readouterr().out.strip() == f"nothing youtube-x: {why}"


def test_repo_fill_keeps_gap_on_fetch_error(wfm_cache: Path, capsys: pytest.CaptureFixture[str],
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    view = _run_with_gap(wfm_cache, description="https://github.com/acme/ledger", flags={"code": True})
    monkeypatch.setattr(repos.fetch, "__kwdefaults__", {**repos.fetch.__kwdefaults__, "opener": _opener({}, [])})
    assert cli.main(["repo-fill", "--run", "wfmrepo1"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("repo youtube-x https://github.com/acme/ledger error repo_unreachable")
    assert out[1] == "kept youtube-x inventory.py lines 64-90: repo not reachable"
    assert "# [gap: lines 64-90 not shown in the video]" in view.code_md.read_text()


def test_anchor_compare_ignores_what_a_frame_cannot_show() -> None:
    video = ["- \U0001F5D1 **Delete Tasks**: remove a task", "\tindented with a tab", "caf\u00e9  "]
    repo = ["\ufeff- \U0001F5D1\ufe0f **Delete Tasks**: remove a task", "    indented with a tab", "cafe\u0301"]
    assert cf._same(video, repo)
    assert not cf._same(["placeholder=\"add your task\""], ["placeholder=\"Add your task\""])  # case is shown
    assert not cf._same(["  x = 1"], ["    x = 1"])  # so is indentation


def test_reused_view_drops_an_earlier_runs_repo_lines(wfm_cache: Path, capsys: pytest.CaptureFixture[str],
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    view = _run_with_gap(wfm_cache, description="https://github.com/acme/ledger", flags={"code": True})
    data = _archive([("ledger-x/inventory.py", "\n".join(INV).encode(), "file")])
    monkeypatch.setattr(repos.fetch, "__kwdefaults__", {**repos.fetch.__kwdefaults__, "opener": _opener({HEAD_URL: data}, [])})
    assert cli.main(["repo-fill", "--run", "wfmrepo1"]) == 0
    assert "from repo" in view.code_md.read_text()
    # the next run on this cached view (say with --no-repo) starts from the video's lines only
    assert plan.restitch_code_md(view, "youtube-x", "0.0.0") is True
    text = view.code_md.read_text()
    assert "from repo" not in text and "# [gap: lines 64-90 not shown in the video]" in text
    view.code_blocks_md.unlink()
    view.code_md.unlink()
    assert plan.restitch_code_md(view, "youtube-x", "0.0.0") is False
