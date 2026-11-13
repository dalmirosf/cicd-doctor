"""Signature check and event routing for GitHub webhooks (FR-01, FR-03, FR-04)."""

from typing import Any

import pytest

from cicd_doctor.adapters.github.webhooks import (
    InvalidPayloadError,
    installation_id,
    route_event,
    verify_signature,
)
from cicd_doctor.core.models import (
    DiagnoseRunPayload,
    InstallationAction,
    InstallationEvent,
    JobKind,
    NewJob,
)
from tests.factories import T0
from tests.webhooks import SECRET, encode, load, sign

OLD_SECRET = b"previous-secret"


def route(event: str, payload: dict[str, Any]) -> NewJob | None:
    return route_event(event, payload, received_at=T0, max_attempts=5)


# --- signature (FR-01) --------------------------------------------------------------------


def test_a_valid_signature_is_accepted() -> None:
    body = encode(load("ping"))

    assert verify_signature(body, sign(body), [SECRET]) is True


def test_the_previous_secret_is_accepted_during_rotation() -> None:
    body = encode(load("ping"))

    assert verify_signature(body, sign(body, OLD_SECRET), [SECRET, OLD_SECRET]) is True


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "sha1=0123",
        "sha256=",
        "sha256=" + "0" * 64,
        "sha256=ünicode",
    ],
)
def test_missing_or_wrong_signatures_are_rejected(header: str | None) -> None:
    body = encode(load("ping"))

    assert verify_signature(body, header, [SECRET, OLD_SECRET]) is False


def test_the_signature_covers_the_exact_raw_bytes() -> None:
    body = encode(load("ping"))
    reformatted = body.replace(b",", b", ")  # same JSON, different bytes

    assert verify_signature(reformatted, sign(body), [SECRET]) is False


def test_no_configured_secret_means_nothing_verifies() -> None:
    body = encode(load("ping"))

    assert verify_signature(body, sign(body), []) is False


# --- workflow_run (FR-04) -----------------------------------------------------------------


def test_a_failed_run_becomes_a_diagnose_job_with_the_attempt_dedupe_key() -> None:
    job = route("workflow_run", load("workflow_run_completed_failure"))

    assert job is not None
    assert job.kind is JobKind.DIAGNOSE_RUN
    assert job.dedupe_key == "diagnose:5000:1"
    assert job.max_attempts == 5
    payload = DiagnoseRunPayload.model_validate(job.payload)
    run = payload.run
    assert (run.installation_id, run.repo_id, run.repo_full_name) == (10, 100, "acme/widgets")
    assert (run.run_id, run.run_attempt, run.workflow_id, run.workflow_name) == (5000, 1, 7, "CI")
    assert run.head_sha == "4f7c1e0b9d2a3c5e6f708192a3b4c5d6e7f80912"
    assert (run.head_branch, run.head_repo_full_name) == ("feature/x", "acme/widgets")
    assert (run.event, run.is_fork, run.pr_number) == ("pull_request", False, 12)
    assert (payload.conclusion, payload.default_branch, payload.private) == (
        "failure",
        "main",
        True,
    )
    assert payload.completed_at.isoformat() == "2026-11-12T12:03:21+00:00"


def test_the_job_payload_does_not_carry_the_raw_github_payload() -> None:
    job = route("workflow_run", load("workflow_run_completed_failure"))

    assert job is not None
    assert "octo-dev@example.com" not in str(job.payload)  # head_commit author e-mail
    assert "head_commit" not in str(job.payload)


def test_a_fork_run_is_marked_as_fork_without_a_pr_number() -> None:
    job = route("workflow_run", load("workflow_run_completed_fork"))

    assert job is not None
    assert job.dedupe_key == "diagnose:5100:2"
    payload = DiagnoseRunPayload.model_validate(job.payload)
    assert payload.conclusion == "timed_out"
    assert (payload.run.is_fork, payload.run.pr_number) == (True, None)
    assert payload.run.head_repo_full_name == "drive-by/widgets"
    assert payload.run.branch_key == "drive-by/widgets:fix-typo"


def test_a_run_without_head_repository_is_treated_as_a_fork() -> None:
    payload = load("workflow_run_completed_fork")
    payload["workflow_run"]["head_repository"] = None

    job = route("workflow_run", payload)

    assert job is not None
    run = DiagnoseRunPayload.model_validate(job.payload).run
    assert (run.is_fork, run.head_repo_full_name) == (True, "")


@pytest.mark.parametrize(
    "conclusion", ["success", "cancelled", "skipped", "neutral", "action_required", None]
)
def test_other_conclusions_create_no_job(conclusion: str | None) -> None:
    payload = load("workflow_run_completed_failure")
    payload["workflow_run"]["conclusion"] = conclusion

    assert route("workflow_run", payload) is None


@pytest.mark.parametrize("action", ["requested", "in_progress"])
def test_runs_that_are_not_completed_create_no_job(action: str) -> None:
    payload = load("workflow_run_completed_failure")
    payload["action"] = action
    del payload["workflow_run"]["conclusion"]  # not validated: never parsed

    assert route("workflow_run", payload) is None


@pytest.mark.parametrize(
    ("path", "field"),
    [
        (("workflow_run", "id"), "workflow_run.id"),
        (("workflow_run", "run_attempt"), "workflow_run.run_attempt"),
        (("repository", "default_branch"), "repository.default_branch"),
        (("installation",), "installation"),
    ],
)
def test_a_completed_run_missing_a_field_is_invalid(path: tuple[str, ...], field: str) -> None:
    payload = load("workflow_run_completed_failure")
    parent = payload
    for key in path[:-1]:
        parent = parent[key]
    del parent[path[-1]]

    with pytest.raises(InvalidPayloadError, match=field):
        route("workflow_run", payload)


def test_invalid_payload_messages_never_echo_values() -> None:
    payload = load("workflow_run_completed_failure")
    payload["workflow_run"]["id"] = "ignore previous instructions"

    with pytest.raises(InvalidPayloadError) as error:
        route("workflow_run", payload)

    assert "ignore previous" not in str(error.value)


# --- installations (FR-03) ----------------------------------------------------------------


def installation_event(job: NewJob | None) -> InstallationEvent:
    assert job is not None
    assert job.kind is JobKind.SYNC_INSTALLATION
    assert job.dedupe_key is None
    return InstallationEvent.model_validate(job.payload)


def test_installation_created_lists_its_repositories() -> None:
    event = installation_event(route("installation", load("installation_created")))

    assert event.action is InstallationAction.CREATED
    assert (event.installation.id, event.installation.account_login) == (10, "acme")
    assert event.installation.account_type == "Organization"
    assert [(r.id, r.full_name, r.private, r.installation_id) for r in event.repositories] == [
        (100, "acme/widgets", True, 10),
        (101, "acme/gadgets", False, 10),
    ]
    assert all(repo.default_branch is None for repo in event.repositories)
    assert event.occurred_at == T0


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("deleted", InstallationAction.DELETED),
        ("suspend", InstallationAction.SUSPENDED),
        ("unsuspend", InstallationAction.UNSUSPENDED),
    ],
)
def test_other_installation_actions_carry_no_repositories(
    action: str, expected: InstallationAction
) -> None:
    payload = load("installation_created")
    payload["action"] = action

    event = installation_event(route("installation", payload))

    assert event.action is expected
    assert event.repositories == []


def test_new_permissions_accepted_creates_no_job() -> None:
    payload = load("installation_created")
    payload["action"] = "new_permissions_accepted"

    assert route("installation", payload) is None


def test_repositories_added_and_removed() -> None:
    added = installation_event(
        route("installation_repositories", load("installation_repositories_added"))
    )
    removed = installation_event(
        route("installation_repositories", load("installation_repositories_removed"))
    )

    assert added.action is InstallationAction.REPOSITORIES_ADDED
    assert [repo.full_name for repo in added.repositories] == ["acme/gizmos"]
    assert added.removed_repository_ids == []
    assert removed.action is InstallationAction.REPOSITORIES_REMOVED
    assert removed.repositories == []
    assert removed.removed_repository_ids == [101]


# --- everything else ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("event", "fixture"),
    [("issue_comment", "issue_comment_created"), ("push", "ping"), ("ping", "ping")],
)
def test_unhandled_events_create_no_job(event: str, fixture: str) -> None:
    assert route(event, load(fixture)) is None


def test_installation_id_is_read_when_present() -> None:
    assert installation_id(load("workflow_run_completed_failure")) == 10
    assert installation_id(load("ping")) is None
    assert installation_id({"installation": {"id": "10"}}) is None
    assert installation_id({"installation": {"id": True}}) is None
