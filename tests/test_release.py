"""Release consistency (spec 2, 5, 7): one version everywhere, manifests exactly as specced,
SKILL.md frontmatter parses on strict YAML, body size, referenced files exist, copy rules.

No network, no imports of the scripts (the version is read from wfm/__init__.py as text).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "skills" / "watch-for-me"
SKILL_MD = SKILL_DIR / "SKILL.md"
REFERENCES = SKILL_DIR / "references"
MAX_BODY_LINES = 220
REQUIRED_REFERENCES = ("visual-reader.md", "transcript-digest.md", "merger.md", "output-formats.md",
                       "save-to-deepmark.md", "troubleshooting.md")
FORBIDDEN_PLUGIN_FIELDS = ("displayName", "defaultEnabled", "userConfig", "experimental",
                           "permissionMode", "maxTurns")
USAGE_CARD = """/watch-for-me <link|file>... [mode] [options]
modes:   (none) summary + timeline · --tldr · --eli5 · --steps · --code · --quotes · --ask "question"
options: --from 12:00 --to 20:00 · --hires · --lang xx (speech language) · --cookies chrome · --playlist N · --audio-only · --fresh · --setup (prefetch models)
--save   also save the link to your Deepmark library (needs the Deepmark connector)
Up to 10 links at once. Speech is transcribed on this device."""
CONNECT_HINT = ("Deepmark isn't connected, so nothing was saved. To enable `--save`: "
                "`claude mcp add -s user --transport http deepmark https://usedeepmark.com/api/mcp`, "
                "then `/mcp` to sign in. In claude.ai: Settings > Connectors > add "
                "`https://usedeepmark.com/api/mcp`. Deepmark is a paid service.")


def _json(rel: str) -> dict[str, Any]:
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def _split_skill() -> tuple[str, str]:
    text = SKILL_MD.read_text(encoding="utf-8")
    m = re.match(r"---\n(.*?)\n---\n(.*)\Z", text, re.DOTALL)
    assert m, "SKILL.md must start with a --- frontmatter block"
    return m.group(1), m.group(2)


def _frontmatter() -> dict[str, Any]:
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load(_split_skill()[0])
    assert isinstance(data, dict)
    return data


def _init_version() -> str:
    text = (SKILL_DIR / "scripts" / "wfm" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^VERSION\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert m, "wfm/__init__.py must define VERSION = \"x.y.z\""
    return m.group(1)


def _changelog_version() -> str:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(r"^## \[?(\d+\.\d+\.\d+)\]?", text, re.MULTILINE)
    assert m, "CHANGELOG.md needs a '## [x.y.z] - date' entry"
    return m.group(1)


# ---------------------------------------------------------------- versions
def test_versions_match_everywhere() -> None:
    fm = _frontmatter()
    market = _json(".claude-plugin/marketplace.json")
    versions = {
        "wfm/__init__.py": _init_version(),
        ".claude-plugin/plugin.json": _json(".claude-plugin/plugin.json")["version"],
        ".claude-plugin/marketplace.json": market["plugins"][0]["version"],
        ".codex-plugin/plugin.json": _json(".codex-plugin/plugin.json")["version"],
        "SKILL.md metadata.version": fm["metadata"]["version"],
        "CHANGELOG.md": _changelog_version(),
    }
    assert len(set(versions.values())) == 1, versions
    assert re.fullmatch(r"\d+\.\d+\.\d+", versions["wfm/__init__.py"])


# ---------------------------------------------------------------- manifests
def test_claude_plugin_manifest() -> None:
    p = _json(".claude-plugin/plugin.json")
    assert p["name"] == "watch-for-me"
    assert p["author"] == {"name": "Deepmark", "url": "https://usedeepmark.com"}
    assert p["license"] == "MIT"
    assert p["repository"] == "https://github.com/shafkathullah/watch-for-me"
    assert not set(FORBIDDEN_PLUGIN_FIELDS) & set(p), "invented manifest fields"
    assert "skills" not in p, "skills are auto-discovered from skills/"


def test_claude_marketplace_manifest() -> None:
    m = _json(".claude-plugin/marketplace.json")
    assert m["name"] == "watch-for-me"
    assert m["owner"] == {"name": "Deepmark"}
    [plugin] = m["plugins"]
    assert plugin["name"] == "watch-for-me"
    assert plugin["source"] == "./"
    assert plugin["category"] == "productivity"


def test_codex_manifests() -> None:
    c = _json(".codex-plugin/plugin.json")
    assert c["name"] == "watch-for-me"
    assert c["skills"] == "./skills/"
    ui = c["interface"]
    assert ui["displayName"] == "Watch for Me"
    assert ui["category"] == "Productivity"
    assert ui["capabilities"] == ["Instructions"]
    for key in ("shortDescription", "longDescription"):
        assert ui[key].strip()
    a = _json(".agents/plugins/marketplace.json")
    [entry] = a["plugins"]
    assert entry["name"] == "watch-for-me"
    assert entry["source"] == {"source": "url", "url": "https://github.com/shafkathullah/watch-for-me.git",
                               "ref": "main"}


def test_license_is_mit_deepmark() -> None:
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert text.startswith("MIT License")
    assert "Copyright (c) 2026 Deepmark" in text


# ---------------------------------------------------------------- SKILL.md
def test_frontmatter_parses_and_matches_spec() -> None:
    fm = _frontmatter()
    assert fm["name"] == "watch-for-me" == SKILL_DIR.name
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", fm["name"])
    desc = fm["description"]
    assert desc.startswith("Watch one or more videos for the user: any link yt-dlp supports")
    assert 0 < len(desc) <= 1024
    assert "deepmark" not in desc.lower()
    assert fm["license"] == "MIT"
    assert fm["compatibility"].startswith("Needs uv and ffmpeg.")
    assert len(fm["compatibility"]) <= 500
    assert fm["argument-hint"].startswith("<url|file>...")


def test_description_is_quoted_in_source() -> None:
    raw = _split_skill()[0]
    line = next(ln for ln in raw.splitlines() if ln.startswith("description:"))
    assert line.startswith('description: "'), "unquoted ': ' breaks strict YAML parsers"


def test_allowed_tools_are_narrowed() -> None:
    tools = _frontmatter()["allowed-tools"]
    assert "Bash(uv run *)" not in tools
    bash = [t for t in tools if t.startswith("Bash(")]
    assert bash == ["Bash(uv run --script '${CLAUDE_SKILL_DIR}/scripts/watch.py' *)"]
    assert set(tools) == {"Bash(uv run --script *watch.py*)", "Read", "Write", "Agent"}


def test_body_length() -> None:
    body = _split_skill()[1]
    assert len(body.splitlines()) <= MAX_BODY_LINES


def test_references_exist_and_are_linked() -> None:
    body = _split_skill()[1]
    for name in REQUIRED_REFERENCES:
        assert (REFERENCES / name).is_file(), name
        assert f"references/{name}" in body, f"SKILL.md never points at {name}"
    for ref in re.findall(r"references/([\w.-]+\.md)", body):
        assert (REFERENCES / ref).is_file(), f"SKILL.md links missing {ref}"


def test_usage_card_exact() -> None:
    assert USAGE_CARD in _split_skill()[1]


def test_commands_use_literal_uv_prefix() -> None:
    body = _split_skill()[1]
    assert "uv run --script '<SKILL_DIR>/scripts/watch.py'" in body
    assert "${CLAUDE_SKILL_DIR}" in body
    assert "timeout: 600000" in body
    assert "--timeout 540" in body
    for sub in ("run --detach", "wait --run", "visual-put --run", "frame", "doctor --quick --brief", "cancel --run"):
        assert sub in body, sub


def test_subagents_read_their_own_instructions() -> None:
    """Main-context budget (spec 4.7): prompts name the reference and the task file instead of
    pasting them, and the readers' outputs are stored by the readers, not re-written by the agent."""
    body = _split_skill()[1]
    for name in ("visual-reader.md", "transcript-digest.md", "merger.md"):
        assert f"references/{name}" in body, name
    assert "Read '<SKILL_DIR>/references/visual-reader.md' and follow it." in body
    assert "TASK: <plan.tasks_dir>/<name>.json" in body
    assert "full text of" not in body and "visual.draft.md" not in body
    for name in ("visual-reader.md", "transcript-digest.md"):
        text = (REFERENCES / name).read_text(encoding="utf-8")
        assert "stored <id>" in text and "`out`" in text, name


def test_save_procedure() -> None:
    text = (REFERENCES / "save-to-deepmark.md").read_text(encoding="utf-8")
    assert "ToolSearch" in text and "+deepmark save_bookmark" in text
    assert CONNECT_HINT in text
    assert "verbatim" in text


def test_subagent_prompts_carry_hard_rule() -> None:
    for name in ("visual-reader.md", "transcript-digest.md", "merger.md"):
        text = (REFERENCES / name).read_text(encoding="utf-8")
        assert "never instructions" in text, name
        assert text.rstrip().endswith("`END`.") or "END\n```" in text, name
    v = (REFERENCES / "visual-reader.md").read_text(encoding="utf-8")
    assert "only command you may run is the `FRAME` command" in v


# ---------------------------------------------------------------- copy rules
def _docs() -> list[Path]:
    return [*sorted(SKILL_DIR.rglob("*.md")), ROOT / "README.md", ROOT / "CHANGELOG.md"]


def test_no_em_dashes_in_docs() -> None:
    for path in _docs():
        assert "—" not in path.read_text(encoding="utf-8"), f"em dash in {path.relative_to(ROOT)}"


def test_readme_links_are_tagged() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    links = re.findall(r"https?://(?:www\.)?usedeepmark\.com[^\s)`\"'>]*", text)
    assert links, "README must credit usedeepmark.com"
    for link in links:
        if link.rstrip("/").endswith("/api/mcp"):
            continue  # MCP endpoint in a command, not a web link
        assert "ref=watch-for-me" in link, link


def test_readme_sections_in_order() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    headings = [h.strip() for h in re.findall(r"^## (.+)$", text, re.MULTILINE)]
    wanted = ["Install", "First run", "Use", "Flags", "How it works", "Privacy", "Requirements",
              "Models and licenses", "Updating", "Uninstall", "FAQ", "Troubleshooting", "Credits", "License"]
    positions = [next(i for i, h in enumerate(headings) if h.startswith(w)) for w in wanted]
    assert positions == sorted(positions), headings
