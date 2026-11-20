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

from cicd_doctor.adapters.memory import CharTokenCounter
from cicd_doctor.core.diff import select_for_prompt
from cicd_doctor.core.models import Category, DiffFile
from cicd_doctor.core.repo_config import effective_llm, ignores, parse_repo_config
from evals.runner.capture import load_manifest
from tests.diffs import patch_of, pull_diff
from tests.llmcases import make_excerpt

LAB = Path(__file__).resolve().parents[2] / "lab"
WORKFLOWS = LAB / ".github" / "workflows"
MANIFEST = load_manifest(LAB / "cases.yaml")
CASES = {case.id: case for case in MANIFEST.cases}
# Workflows of the live verification (P1c): started by a pull request, not a case of the evals.
LIVE_WORKFLOWS = {"pr-py-modnf.yml": "py-modnf"}
# Live verification of P2: a flaky simulated by the run attempt, not a case of the evals.
FLAKY_LIVE_WORKFLOW = "pr-flaky.yml"
# Live verification of P3: the failure of a line that a pull request changes (suggestions).
SUGGEST_LIVE_WORKFLOW = "pr-suggest.yml"


def _workflow(name: str) -> dict[Any, Any]:
    data = yaml.safe_load((WORKFLOWS / name).read_text("utf-8"))
    assert isinstance(data, dict)
    return data


def test_case_ids_are_unique_and_every_workflow_has_a_case() -> None:
    assert len(CASES) == len(MANIFEST.cases)
    assert {case.workflow for case in MANIFEST.cases} | LIVE_WORKFLOWS.keys() | {
        FLAKY_LIVE_WORKFLOW,
        SUGGEST_LIVE_WORKFLOW,
    } == {p.name for p in WORKFLOWS.glob("*.yml")}


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


@pytest.mark.parametrize("name", sorted(LIVE_WORKFLOWS))
def test_live_workflow_runs_on_pull_requests_and_repeats_its_case(name: str) -> None:
    workflow = _workflow(name)
    case = CASES[LIVE_WORKFLOWS[name]]
    original = _workflow(case.workflow)
    triggers = workflow.get("on", workflow.get(True))

    assert triggers == {"pull_request": None}
    assert workflow["name"] == name.removesuffix(".yml")  # the check is "CICD Doctor: <name>"
    assert workflow["permissions"] == {"contents": "read"}
    assert "timeout-minutes" in workflow["jobs"][case.job]
    assert workflow["jobs"] == original["jobs"]  # the same failure, step by step


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


def test_simulated_flaky_workflow_fails_in_attempt_one_and_passes_after() -> None:
    workflow = _workflow(FLAKY_LIVE_WORKFLOW)
    triggers = workflow.get("on", workflow.get(True))
    script = "\n".join(
        step.get("run", "") for step in workflow["jobs"]["run"]["steps"] if isinstance(step, dict)
    )

    assert triggers == {"pull_request": None}
    assert workflow["name"] == "pr-flaky"
    assert workflow["permissions"] == {"contents": "read"}
    assert "timeout-minutes" in workflow["jobs"]["run"]
    assert 'if [ "$GITHUB_RUN_ATTEMPT" = 1 ]; then' in script
    assert "exit 1" in script


def test_suggest_workflow_fails_on_a_named_line_of_a_file_that_the_diff_can_hold() -> None:
    workflow = _workflow(SUGGEST_LIVE_WORKFLOW)
    triggers = workflow.get("on", workflow.get(True))
    job = workflow["jobs"]["run"]
    script = "\n".join(step.get("run", "") for step in job["steps"] if isinstance(step, dict))

    assert triggers == {"pull_request": {"paths": ["python/suggest/**"]}}
    assert workflow["name"] == "pr-suggest"
    assert workflow["permissions"] == {"contents": "read"}
    assert "timeout-minutes" in job
    assert "working-directory" not in str(job)  # the log must name the path as the diff does
    assert "python/suggest/legacy.py" in script
    assert "--output-format concise" in script  # path:line:col right after the path
    legacy = (LAB / "python" / "suggest" / "legacy.py").read_text("utf-8")
    assert len(re.findall(r"[=!]= None", legacy)) == 1  # one failure, on main
    assert "None" not in (LAB / "python" / "suggest" / "notes.py").read_text("utf-8")


@pytest.mark.parametrize(
    ("name", "state", "llm", "comment"),
    [
        ("p3-on-demand.yml", "file", "on_demand", True),
        ("p3-comment-off.yml", "file", "on_demand", False),
        ("p3-branch-only.yml", "file", "off", True),
        ("p3-invalid.yml", "invalid", "off", True),
    ],
)
def test_p3_config_samples_say_what_the_runbook_expects(
    name: str, state: str, llm: str, comment: bool
) -> None:
    loaded = parse_repo_config((LAB / "config" / name).read_text("utf-8"))

    assert loaded.state == state
    assert effective_llm(loaded) == llm
    assert loaded.config.comment is comment
    assert ignores(loaded, "pr-flaky") is (name in {"p3-on-demand.yml", "p3-comment-off.yml"})


def test_the_log_of_pr_suggest_makes_the_diff_reach_the_model_only_when_the_file_is_in_it() -> None:
    log = [
        "##[group]Run uvx ruff check --isolated --select E711 --output-format concise "
        "python/suggest/legacy.py",
        "python/suggest/legacy.py:8:12: E711 [*] Comparison to `None` should be `cond is None`",
        "Found 2 errors.",
    ]
    excerpt = make_excerpt(log)
    legacy = DiffFile(
        filename="python/suggest/legacy.py",
        status="modified",
        patch=patch_of(
            (5, 5, [" ", " ", " def is_missing(value: object) -> bool:", "-    old", "+    new"])
        ),
    )
    notes = DiffFile(
        filename="python/suggest/notes.py", status="modified", patch=patch_of((3, 3, ["+x"]))
    )

    inside = select_for_prompt(pull_diff(legacy, notes), excerpt, CharTokenCounter())
    outside = select_for_prompt(pull_diff(notes), excerpt, CharTokenCounter())

    assert inside is not None
    assert [file.path for file in inside.files] == ["python/suggest/legacy.py"]
    assert outside is None  # the model gets no diff, so no suggestion can be posted
