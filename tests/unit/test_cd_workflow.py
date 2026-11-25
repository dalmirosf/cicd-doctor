"""Offline checks of `.github/workflows/cd.yml` (P5 f, D-128).

The deploy role trusts every run on `main` (D-123), so the workflow's shape is part of the
security boundary: these tests parse the YAML and pin that shape.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
CD = WORKFLOWS / "cd.yml"

pytestmark = pytest.mark.unit


def _load() -> dict[Any, Any]:
    data = yaml.safe_load(CD.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _triggers(data: dict[Any, Any]) -> dict[Any, Any]:
    # PyYAML reads the bare key `on` as the boolean True.
    raw = data.get("on", data.get(True))
    assert isinstance(raw, dict)
    return raw


def test_triggers_are_push_to_main_and_manual_only() -> None:
    triggers = _triggers(_load())
    assert set(triggers) == {"push", "workflow_dispatch"}
    assert triggers["push"] == {"branches": ["main"]}


def test_no_pull_request_trigger_and_no_pull_request_target_anywhere() -> None:
    lines = CD.read_text(encoding="utf-8").splitlines()
    code = chr(10).join(x for x in lines if not x.lstrip().startswith("#"))
    assert "pull_request" not in code


def test_permissions_are_read_only_except_the_token_of_the_deploy_job() -> None:
    data = _load()
    assert data["permissions"] == {"contents": "read"}
    jobs = data["jobs"]
    assert list(jobs) == ["deploy"]
    assert jobs["deploy"]["permissions"] == {"contents": "read", "id-token": "write"}


def test_concurrency_serialises_deploys_without_cancelling() -> None:
    assert _load()["concurrency"] == {"group": "cd", "cancel-in-progress": False}


def _steps(data: dict[Any, Any]) -> list[dict[Any, Any]]:
    return [step for job in data["jobs"].values() for step in job["steps"]]


def _run_script(data: dict[Any, Any]) -> str:
    return "\n".join(step.get("run", "") for step in _steps(data))


def test_every_action_is_pinned_to_a_full_commit_sha() -> None:
    uses = [step["uses"] for step in _steps(_load()) if "uses" in step]
    assert uses
    for ref in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref), ref


def test_the_aws_action_assumes_a_role_and_gets_no_key() -> None:
    steps = [
        s
        for s in _steps(_load())
        if str(s.get("uses", "")).startswith("aws-actions/configure-aws-credentials@")
    ]
    assert len(steps) == 1
    with_ = steps[0]["with"]
    assert "vars.AWS_DEPLOY_ROLE_ARN" in with_["role-to-assume"]
    assert set(with_) <= {"role-to-assume", "aws-region", "role-session-name"}


def test_no_aws_key_and_no_secret_in_the_workflow() -> None:
    text = CD.read_text(encoding="utf-8")
    assert "secrets." not in text
    assert not re.search(r"AWS_(ACCESS_KEY_ID|SECRET_ACCESS_KEY|SESSION_TOKEN)", text)
    assert "aws-access-key-id" not in text.lower()
    assert not re.search(r"AKIA[0-9A-Z]{16}", text)


def test_the_role_and_the_region_come_from_variables() -> None:
    text = CD.read_text(encoding="utf-8")
    assert "vars.AWS_DEPLOY_ROLE_ARN" in text
    assert "vars.AWS_REGION || 'us-east-2'" in text


def test_no_untrusted_context_is_interpolated_into_a_script() -> None:
    # Only ids and variables the owner controls may appear, and always through `env:`.
    for step in (s for job in _load()["jobs"].values() for s in job["steps"]):
        assert "${{" not in step.get("run", ""), step.get("name")


def test_the_image_is_tagged_with_the_commit_and_never_latest() -> None:
    # The ECR repository is IMMUTABLE (persistent/variables.tf): a moving tag would fail.
    script = _run_script(_load())
    assert "${GITHUB_SHA}" in script
    assert ":latest" not in script


def test_the_migration_runs_before_the_services_are_updated() -> None:
    names = [s.get("name", "") for s in _load()["jobs"]["deploy"]["steps"]]
    assert names.index("Run the migration") < names.index("Roll out the services")
    assert names.index("Push the image") < names.index("Find the services")
    migration = next(
        s for s in _load()["jobs"]["deploy"]["steps"] if s.get("name") == "Run the migration"
    )
    # A step stops at the first failing command (`set -e`) and checks the exit code of the task.
    assert "set -euo pipefail" in migration["run"]
    assert '"$code" != "0"' in migration["run"]


def test_a_down_stack_is_a_notice_and_skips_the_rollout() -> None:
    steps = _load()["jobs"]["deploy"]["steps"]
    script = _run_script(_load())
    assert "ClusterNotFoundException" in script
    assert "::notice::" in script
    # GitHub runs `run:` steps with `bash -e`: a bare `out="$(aws ...)"` that fails ends the step
    # before the branch above can run, so the failure must be caught on the assignment (D-130).
    find = next(s for s in steps if s.get("name") == "Find the services")
    assert re.search(
        r'out="\$\(aws ecs describe-services[^)]*\)"\s*\|\|\s*status=\$\?', find["run"]
    )
    assert not re.search(r"^\s*status=\$\?\s*$", find["run"], re.MULTILINE)
    gated = {
        "Register task definitions with the new image",
        "Run the migration",
        "Roll out the services",
    }
    for step in steps:
        if step.get("name") in gated:
            assert step["if"] == "steps.stack.outputs.up == 'true'"


def test_the_workflow_only_uses_calls_the_deploy_policy_allows() -> None:
    # persistent/oidc.tf is the allow list; a new call needs a decision, not a silent edit.
    allowed = {
        "ecs describe-services",
        "ecs describe-task-definition",
        "ecs register-task-definition",
        "ecs run-task",
        "ecs describe-tasks",
        "ecs update-service",
        "ecs wait tasks-stopped",
        "ecs wait services-stable",
        "ecr get-login-password",
        "logs get-log-events",
        "sts get-caller-identity",
    }
    services = "ecs|ecr|logs|sts|iam|s3|ec2|rds|secretsmanager"
    pattern = r"\baws ((?:" + services + r") [\w-]+(?: [\w-]+)?)"
    used = set(re.findall(pattern, _run_script(_load())))
    normalised = set()
    for call in used:
        parts = call.split()
        wait = parts[1] == "wait"
        normalised.add(" ".join(parts[:3]) if wait else " ".join(parts[:2]))
    assert normalised <= allowed, normalised - allowed


def test_every_workflow_that_asks_for_a_token_triggers_on_push_to_main_only() -> None:
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        text = path.read_text(encoding="utf-8")
        if re.search(r"^\s*id-token\s*:\s*write", text, re.M):
            assert path.name == "cd.yml", path.name
            assert set(_triggers(data)) == {"push", "workflow_dispatch"}
