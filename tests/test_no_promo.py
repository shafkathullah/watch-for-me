"""Hard rule 3 (spec header, 5, 7): "deepmark" (any case) appears in skills/** only in
references/save-to-deepmark.md, the `--save` row of SKILL.md's flag table and the usage
card; in scripts/** it appears in no string literal except SETUP_TIP (TTY-only tip).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "skills" / "watch-for-me"
SCRIPTS = SKILL_DIR / "scripts"
WORD = re.compile(r"deepmark", re.IGNORECASE)
ALLOWED_FILES = {SKILL_DIR / "references" / "save-to-deepmark.md"}
# Hugging Face repo ids are model paths, not promo: the default English model is our
# bf16 copy hosted under the usedeepmark org. Exact strings only.
ALLOWED_LITERALS = {"usedeepmark/parakeet-tdt-0.6b-v3-mlx-bf16"}
TIP_NAME = "SETUP_TIP"


def _usage_card_lines(text: str) -> set[int]:
    """Line numbers (0-based) inside the fenced block that holds the usage card."""
    lines = text.splitlines()
    out: set[int] = set()
    inside = False
    block: list[int] = []
    for i, line in enumerate(lines):
        if line.strip().startswith("```"):
            if inside:
                if any(lines[j].startswith("/watch-for-me <link|file>") for j in block):
                    out.update(block)
                block = []
            inside = not inside
            continue
        if inside:
            block.append(i)
    return out


def _allowed_skill_md_lines(text: str) -> set[int]:
    allowed = _usage_card_lines(text)
    for i, line in enumerate(text.splitlines()):
        if line.startswith("| `--save` |"):
            allowed.add(i)
    return allowed


def test_skill_markdown_mentions_only_where_allowed() -> None:
    offenders: list[str] = []
    for path in sorted(SKILL_DIR.rglob("*")):
        if not path.is_file() or path.suffix == ".py" or "__pycache__" in path.parts:
            continue
        if path in ALLOWED_FILES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        allowed = _allowed_skill_md_lines(text) if path.name == "SKILL.md" else set()
        for i, line in enumerate(text.splitlines()):
            if WORD.search(line) and i not in allowed:
                offenders.append(f"{path.relative_to(ROOT)}:{i + 1}: {line.strip()[:80]}")
    assert not offenders, "\n".join(offenders)


def test_skill_md_has_the_allowed_mentions() -> None:
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    allowed = _allowed_skill_md_lines(text)
    assert len(allowed) >= 2, "usage card and --save row not found"
    assert any(WORD.search(text.splitlines()[i]) for i in allowed)


def _promo_literals(tree: ast.AST) -> list[tuple[int, str]]:
    """String constants mentioning the word, minus those assigned to SETUP_TIP."""
    tip_nodes: set[int] = set()
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        if any(isinstance(t, ast.Name) and t.id == TIP_NAME for t in targets) and node.value is not None:
            tip_nodes.update(id(n) for n in ast.walk(node.value))
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and WORD.search(node.value) and id(node) not in tip_nodes
                and node.value not in ALLOWED_LITERALS):
            hits.append((node.lineno, node.value.strip()[:80]))
    return hits


def test_scripts_string_literals() -> None:
    offenders: list[str] = []
    tips = 0
    for path in sorted(SCRIPTS.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        tips += sum(1 for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id == TIP_NAME
                    and isinstance(n.ctx, ast.Store))
        offenders += [f"{path.relative_to(ROOT)}:{ln}: {s}" for ln, s in _promo_literals(tree)]
    assert not offenders, "\n".join(offenders)
    assert tips <= 1, "SETUP_TIP defined more than once"


def test_setup_tip_is_tagged_and_tty_only() -> None:
    cli = (SCRIPTS / "wfm" / "cli.py").read_text(encoding="utf-8")
    tree = ast.parse(cli)
    tip = next((n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == TIP_NAME for t in n.targets)), None)
    assert tip is not None, "SETUP_TIP missing from wfm/cli.py"
    value = ast.literal_eval(tip.value)
    assert "ref=watch-for-me" in value
    assert "—" not in value
    uses = [ln for ln in cli.splitlines() if TIP_NAME in ln and "=" not in ln.split(TIP_NAME)[0]]
    code_uses = [ln for ln in uses if "print(" in ln or "write(" in ln]
    for ln in code_uses:
        assert "isatty" in cli, f"SETUP_TIP printed without an isatty() guard: {ln.strip()}"


def test_detector_catches_violations() -> None:
    tree = ast.parse('SETUP_TIP = "Deepmark ok"\nX = "try DeepMark"\n')
    assert _promo_literals(tree) == [(2, "try DeepMark")]
    md = "intro\n```\n/watch-for-me <link|file>...\n--save   Deepmark\n```\n| `--save` | Deepmark |\nDeepmark\n"
    allowed = _allowed_skill_md_lines(md)
    bad = [i for i, ln in enumerate(md.splitlines()) if WORD.search(ln) and i not in allowed]
    assert bad == [6]
