"""The ``issue_comment`` webhook: who can give the command and how it is checked (FR-19, D-92).

The decision uses only the signed payload. A comment that is not an authorized command is a 204
whatever else it lacks; only an accepted one can be a 400, and its job never carries the body.
"""

from typing import Any

import pytest

from cicd_doctor.adapters.github.webhooks import InvalidPayloadError, route_event
from cicd_doctor.core.models import ExplainPayload, JobKind, NewJob, explain_dedupe_key
from tests.factories import T0
from tests.webhooks import load

COMMAND = "/cicd-doctor explain"


def route(payload: dict[str, Any]) -> NewJob | None:
    return route_event("issue_comment", payload, received_at=T0, max_attempts=5)


def comment(body: str = COMMAND, **changes: Any) -> dict[str, Any]:
    payload = load("issue_comment_created")
    payload["comment"]["body"] = body
    for path, value in changes.items():
        target = payload
        *parents, last = path.split("__")
        for part in parents:
            target = target[part]
        target[last] = value
    return payload


def test_an_authorized_command_becomes_an_explain_job_with_no_body() -> None:
    job = route(comment())

    assert job is not None
    assert job.kind is JobKind.EXPLAIN
    assert job.dedupe_key == explain_dedupe_key(777) == "explain:777"
    assert job.max_attempts == 5
    payload = ExplainPayload.model_validate(job.payload)
    assert payload.model_dump() == {
        "installation_id": 10,
        "repo_id": 100,
        "repo_full_name": "acme/widgets",
        "private": True,
        "default_branch": "main",
        "account_login": "acme",
        "account_type": "Organization",
        "pr_number": 12,
        "comment_id": 777,
        "actor_login": "acme-maintainer",
        "received_at": T0,
    }
    assert "cicd-doctor" not in str(job.payload).replace("acme-maintainer", "")  # no body


@pytest.mark.parametrize(
    "body", [COMMAND + "\r\n", COMMAND + "\nthanks!", COMMAND + "\r\nmore\r\n"]
)
def test_only_the_first_line_counts_and_a_carriage_return_is_ignored(body: str) -> None:
    assert route(comment(body)) is not None


@pytest.mark.parametrize(
    "body",
    [
        "",
        "/cicd-doctor",
        "/cicd-doctor explain ",  # trailing space
        " /cicd-doctor explain",  # leading space
        "\t/cicd-doctor explain",
        "/cicd-doctor  explain",
        "/cicd-doctor explain now",
        "/cicd-doctor explain --all",
        "/cicd-doctor explain\u200b",  # zero width space
        "\u200b/cicd-doctor explain",
        "\ufeff/cicd-doctor explain",  # byte order mark
        "/cicd-doctor\u00a0explain",  # no-break space
        "/CICD-DOCTOR explain",
        "/cicd-doctor EXPLAIN",
        "/cicd-d\u043ector explain",  # Cyrillic small o: a homoglyph
        "/cicd\u2011doctor explain",  # non-breaking hyphen
        "please run\n/cicd-doctor explain",  # second line
        "\n/cicd-doctor explain",  # first line empty
        "```\n/cicd-doctor explain\n```",  # in a code block
        "> /cicd-doctor explain",  # quoted
        "`/cicd-doctor explain`",
        "- /cicd-doctor explain",
        "/cicd-doctor explain\u2028x",  # not a line break for us: the line is not exact
        "@cicd-doctor explain",
    ],
)
def test_anything_but_the_exact_first_line_is_ignored(body: str) -> None:
    assert route(comment(body)) is None


@pytest.mark.parametrize(
    ("association", "accepted"),
    [
        ("OWNER", True),
        ("MEMBER", True),
        ("COLLABORATOR", True),
        ("CONTRIBUTOR", False),
        ("FIRST_TIMER", False),
        ("FIRST_TIME_CONTRIBUTOR", False),
        ("MANNEQUIN", False),
        ("NONE", False),
        ("owner", False),
        ("", False),
        (None, False),
    ],
)
def test_the_six_associations_and_the_unknown_ones(association: str | None, accepted: bool) -> None:
    job = route(comment(comment__author_association=association))

    assert (job is not None) is accepted


def test_a_missing_association_is_ignored() -> None:
    payload = comment()
    del payload["comment"]["author_association"]

    assert route(payload) is None


@pytest.mark.parametrize("kind", ["Bot", "Organization", "Mannequin", "bot", ""])
def test_a_comment_by_anything_but_a_user_is_ignored(kind: str) -> None:
    assert route(comment(comment__user={"login": "dependabot[bot]", "id": 9, "type": kind})) is None


@pytest.mark.parametrize("action", ["edited", "deleted", "pinned", "unpinned", "Created", "", None])
def test_only_created_counts(action: str | None) -> None:
    assert route(comment(action=action)) is None


def test_a_comment_without_an_action_is_ignored() -> None:
    payload = comment()
    del payload["action"]

    assert route(payload) is None


def test_a_comment_on_an_issue_that_is_not_a_pull_request_is_ignored() -> None:
    payload = comment()
    del payload["issue"]["pull_request"]

    assert route(payload) is None


@pytest.mark.parametrize("value", [None, "https://x", [], 1])
def test_a_pull_request_marker_that_is_not_an_object_is_ignored(value: object) -> None:
    assert route(comment(issue__pull_request=value)) is None


@pytest.mark.parametrize("body", [None, 7, ["/cicd-doctor explain"], {"x": 1}])
def test_a_body_that_is_not_text_is_ignored(body: object) -> None:
    assert route(comment(comment__body=body)) is None


@pytest.mark.parametrize("field", ["issue", "comment", "user"])
def test_missing_or_malformed_parts_are_ignored_not_errors(field: str) -> None:
    payload = comment()
    if field == "user":
        del payload["comment"]["user"]
    else:
        payload[field] = "nope"

    assert route(payload) is None


def test_an_accepted_command_without_an_installation_is_a_400_naming_the_field() -> None:
    payload = comment()
    del payload["installation"]

    with pytest.raises(InvalidPayloadError, match="installation"):
        route(payload)


@pytest.mark.parametrize("field", ["id", "full_name", "private", "default_branch", "owner"])
def test_an_accepted_command_without_a_repository_field_is_a_400(field: str) -> None:
    payload = comment()
    del payload["repository"][field]

    with pytest.raises(InvalidPayloadError, match=field):
        route(payload)


def test_a_400_never_echoes_values() -> None:
    payload = comment(comment__id="secret-looking-value")

    with pytest.raises(InvalidPayloadError) as caught:
        route(payload)

    assert "secret-looking-value" not in str(caught.value)


def test_an_ignored_comment_with_a_broken_repository_is_still_a_204() -> None:
    payload = comment("just a comment", repository=None)

    assert route(payload) is None


def test_the_job_payload_has_no_field_that_could_hold_the_body() -> None:
    assert "body" not in ExplainPayload.model_fields
    assert not any("body" in name or "text" in name for name in ExplainPayload.model_fields)
