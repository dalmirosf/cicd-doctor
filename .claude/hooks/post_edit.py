"""Claude Code PostToolUse hook: ruff + the fast tests after Claude edits a Python file.

Registered in .claude/settings.json for the Edit/Write tools. It reads the hook payload (JSON)
from stdin and ignores edits to files that cannot affect the checks (docs, YAML, files outside
the repository).

Exit codes: 0 = everything passed (silent); 2 = something failed. For PostToolUse hooks Claude
Code feeds stderr back to the model, so the failure shows up right after the edit that caused it.

The checks only read: nothing is auto-fixed, so files never change behind Claude's back.
Run by hand with:  uv run python .claude/hooks/post_edit.py < NUL
"""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RELEVANT_SUFFIXES = {".py", ".pyi"}
RELEVANT_NAMES = {"pyproject.toml"}
STEP_TIMEOUT_S = 150
MAX_OUTPUT_CHARS = 6000

PYTHON = sys.executable  # the project's venv, because this script is started with `uv run`
STEPS: tuple[tuple[str, list[str]], ...] = (
    ("ruff check", [PYTHON, "-m", "ruff", "check", "."]),
    ("ruff format --check", [PYTHON, "-m", "ruff", "format", "--check", "."]),
    (
        "pytest -m 'unit or contract'",
        [PYTHON, "-m", "pytest", "-m", "unit or contract", "-x", "-q"],
    ),
)


def edited_path(raw_payload: str) -> Path | None:
    """The file the tool edited, or None when the payload does not say."""
    try:
        # Windows pipes (PowerShell) may prepend a UTF-8 BOM, which json.loads rejects.
        file_path = json.loads(raw_payload.lstrip("﻿"))["tool_input"]["file_path"]
    except (ValueError, KeyError, TypeError):
        return None
    return Path(file_path).resolve() if isinstance(file_path, str) else None


def is_relevant(path: Path | None) -> bool:
    if path is None:  # unknown payload (for example a manual run): check everything
        return True
    if not path.is_relative_to(ROOT):
        return False
    return path.suffix in RELEVANT_SUFFIXES or path.name in RELEVANT_NAMES


def run_step(name: str, command: list[str]) -> str | None:
    """Return a failure report for the step, or None when it passed."""
    env = {**os.environ, "NO_COLOR": "1", "PYTHONIOENCODING": "utf-8"}
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=STEP_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"## {name}: timed out after {STEP_TIMEOUT_S}s"
    if result.returncode == 0:
        return None
    output = (result.stdout + result.stderr).strip()
    if len(output) > MAX_OUTPUT_CHARS:
        output = "[...truncated...]\n" + output[-MAX_OUTPUT_CHARS:]
    return f"## {name}: failed (exit {result.returncode})\n{output}"


def main() -> int:
    if not is_relevant(edited_path(sys.stdin.read())):
        return 0
    failures = [report for name, command in STEPS if (report := run_step(name, command))]
    if not failures:
        return 0
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.write("Checks failed after this edit. Fix them before continuing.\n\n")
    sys.stderr.write("\n\n".join(failures) + "\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
