"""Shared pytest setup: puts skills/watch-for-me/scripts on sys.path so tests can
`import wfm.<module>` and `import asr_common` exactly like watch.py does.

Run: cd tools/watch-for-me && uv run --with pytest --with pillow --with numpy pytest tests
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "skills" / "watch-for-me"
SCRIPTS_DIR = SKILL_DIR / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


@pytest.fixture
def wfm_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated cache root: sets WFM_CACHE_DIR to <tmp>/cache (created) for the test."""
    root = tmp_path / "cache"
    root.mkdir()
    monkeypatch.setenv("WFM_CACHE_DIR", str(root))
    return root
