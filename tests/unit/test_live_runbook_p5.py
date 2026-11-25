"""The P5 live runbook (infra/terraform/README.md, "Live verification (P5)"), checked as text.

It is documentation the owner pastes into PowerShell with real money behind it, so the rules that
protect that moment are pinned: no secret value anywhere, no read of a secret, every step tagged,
and the shapes PowerShell 5.1 cannot parse kept out. The blocks themselves are parsed with the
PowerShell parser by hand (Linux CI has none).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
README = (ROOT / "infra" / "terraform" / "README.md").read_text(encoding="utf-8")

pytestmark = pytest.mark.unit

HEADING = "## Live verification (P5)"


def _section() -> str:
    assert README.count(HEADING) == 1
    return README[README.index(HEADING) :]


def _powershell_blocks(text: str) -> list[str]:
    return re.findall(r"```powershell\n(.*?)\n```", text, flags=re.DOTALL)


def test_every_step_names_what_it_does_to_the_account() -> None:
    steps = re.findall(r"^### (\d+)\. (.+)$", _section(), flags=re.MULTILINE)
    assert [int(n) for n, _ in steps] == list(range(0, 17))
    tags = ("READ-ONLY", "WRITES", "SPENDS", "BY HAND", "LOCAL")
    for number, title in steps:
        if number in {"3", "4"}:
            continue  # they carry the tag on their plan and apply lines instead
        assert any(tag in title for tag in tags), f"step {number} has no tag: {title}"
    body = _section()
    assert "**Plan (READ-ONLY).**" in body
    assert "**Apply (WRITES)." in body


def test_no_block_reads_a_secret_or_holds_a_value() -> None:
    section = _section()
    blocks = "\n".join(_powershell_blocks(section))
    assert "get-secret-value" not in blocks.lower()
    ids = [n for n in re.findall(r"\b\d{12}\b", section) if set(n) != {"0"}]
    assert not ids, "a 12-digit id (an account?) is in the runbook"
    assert not re.search(r"AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{36}|-----BEGIN", section)
    assert "$PROFILE" not in blocks  # a PowerShell automatic variable
    assert "--secret-string" in blocks
    assert "Read-Host" in blocks
    assert "-AsSecureString" in blocks


def test_blocks_avoid_what_powershell_5_1_rejects() -> None:
    for block in _powershell_blocks(_section()):
        assert "&&" not in block
        assert "2>&1" not in block
        assert not re.search(r"(?m)^\s*(terraform|aws|gh)\b[^'\"\n]*<[A-Za-z]", block), block


def test_the_five_secrets_and_the_stop_rule_are_there() -> None:
    section = " ".join(_section().split())
    for name in (
        "github-app-private-key",
        "github-webhook-secret",
        "openai-api-key",
        "langfuse-public-key",
        "langfuse-secret-key",
    ):
        assert f"cicd-doctor/{name}" in section
    assert "explicit deny in a service control policy" in section
    assert "Nobody here edits, detaches or works around an SCP" in section
