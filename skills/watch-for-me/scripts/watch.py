# /// script
# requires-python = ">=3.10,<3.14"
# dependencies = ["yt-dlp[default,deno]>=2026.8.19", "pillow>=12.3"]
# ///
"""watch-for-me CLI entry (spec 3).

    uv run --script <SKILL_DIR>/scripts/watch.py <run|wait|visual-put|frame|doctor|setup|cache|cancel> [args]

Python puts this file's directory first on sys.path, so `wfm/` and
`asr_common.py` import without packaging. All logic lives in wfm.cli.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from wfm.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
