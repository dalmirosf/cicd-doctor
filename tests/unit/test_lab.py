"""The lab repository sources (``lab/``, SPEC §11.1, D-61): manifest and workflows agree.

These checks run on the files only. Whether each workflow fails the expected way is checked when
it runs in the published lab repository and its log is captured.
"""

import re
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml

from cicd_doctor.core.models import Category
from evals.runner.capture import load_manifest

LAB = Path(__file__).resolve().parents[2] / "lab"
WORKFLOWS = LAB / ".github" / "workflows"
MANIFEST = load_manifest(LAB / "cases.yaml")
CASES = {case.id: case for case in MANIFEST.cases}


def _workflow(name: str) -> dict[Any, Any]:
    data = yaml.safe_load((WORKFLOWS / name).read_text("utf-8"))
    assert isinstance(data, dict)
    return data


def test_case_ids_are_unique_and_every_workflow_has_a_case() -> None:
    assert len(CASES) == len(MANIFEST.cases)
    assert {case.workflow for case in MANIFEST.cases} == {p.name for p in WORKFLOWS.glob("*.yml")}


def test_at_least_twenty_cases_and_two_per_category() -> None:
    per_category = Counter(case.category for case in MANIFEST.cases)

    assert len(MANIFEST.cases) >= 20
    assert {
        category: per_category[category] for category in Category if per_category[category] < 2
    } == {}


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_workflow_is_manual_only_least_privilege_and_has_the_job(case_id: str) -> None:
    case = CASES[case_id]
    workflow = _workflow(case.workflow)
    triggers = workflow.get("on", workflow.get(True))  # YAML 1.1 reads a bare `on` as True

    assert triggers == {"workflow_dispatch": None}  # publishing the lab runs nothing
    assert workflow["permissions"] == {"contents": "read"}
    assert case.job in workflow["jobs"]
    assert "timeout-minutes" in workflow["jobs"][case.job]


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_evidence_patterns_compile_and_flaky_cases_are_marked(case_id: str) -> None:
    case = CASES[case_id]

    assert case.evidence, "every case names at least one evidence line"
    for pattern in case.evidence:
        re.compile(pattern)
    assert case.flaky is (case.category is Category.FLAKY)


def test_no_workflow_uses_a_real_secret_or_cloud_credential() -> None:
    for path in WORKFLOWS.glob("*.yml"):
        text = path.read_text("utf-8")
        secrets = re.findall(r"secrets\.([A-Z0-9_]+)", text)
        assert secrets in ([], ["LAB_DEPLOY_TOKNE"]), path.name  # the misspelled one is the case
        assert "id-token: write" not in text


def test_third_party_actions_are_pinned_or_official() -> None:
    for path in WORKFLOWS.glob("*.yml"):
        for action in re.findall(r"uses: (\S+)", path.read_text("utf-8")):
            owner = action.split("/")[0]
            pinned = re.search(r"@[0-9a-f]{40}$", action) is not None
            assert pinned or owner in {"actions", "hashicorp"}, f"{path.name}: {action}"
