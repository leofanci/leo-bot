#!/usr/bin/env python3
"""PostToolUse hook — rebuild os.db when an authored SOURCE file changes.

Files are the source of truth; os.db is the derived index. After any Edit/Write
to a file under ventures/ brands/ apps/ portfolio/, re-run index.py so the index
never drifts from the files. No-op for every other edit. Never blocks (exit 0).

Wired in .claude/settings.json as a PostToolUse hook on Edit|Write|MultiEdit.
"""

import json
import subprocess
import sys
from pathlib import Path

DATA_DIRS = {"ventures", "brands", "apps", "portfolio"}
SOURCE_EXTS = {".md", ".json"}
CODE_DIR = Path(__file__).resolve().parent.parent  # the checkout (one above hooks/)
sys.path.insert(0, str(CODE_DIR))

from core import paths  # noqa: E402

ROOT = paths.DATA  # the workspace holding the user data


def main():
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return  # nothing to do, never block

    fp = (event.get("tool_input") or {}).get("file_path")
    if not fp:
        return

    try:
        relp = Path(fp).resolve().relative_to(ROOT)
    except ValueError:
        return  # edit outside this workspace

    if not relp.parts or relp.parts[0] not in DATA_DIRS or relp.suffix not in SOURCE_EXTS:
        return  # not an indexed source file

    res = subprocess.run(
        [sys.executable, str(paths.CODE / "index.py"), str(ROOT)],
        capture_output=True, text=True,
    )
    if res.returncode == 0:
        print(f"[reindex] {relp} changed → os.db rebuilt")
    else:
        # surface the failure to Claude without blocking the edit
        print(f"[reindex] FAILED after {relp} changed:\n{res.stderr.strip()[:600]}", file=sys.stderr)


if __name__ == "__main__":
    main()
