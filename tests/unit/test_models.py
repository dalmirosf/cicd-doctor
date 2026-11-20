from uuid import uuid4

import pytest
from pydantic import ValidationError

from cicd_doctor.core.models import (
    Category,
    DiagnoseRunPayload,
    Diagnosis,
    Evidence,
    PublicationKey,
    PublicationKind,
    PublishedUpgrade,
    RepoConfig,
    SimilarFailure,
)
from tests.factories import make_diagnosis, make_rules_diagnosis, make_run_ref, make_usage


def test_category_has_exactly_the_eight_spec_values() -> None:
    assert {category.value for category in Category} == {
        "broken_test",
        "dependency",
        "network",
        "config_or_secrets",
        "timeout",
        "infra_or_deploy",
        "flaky",
        "unknown",
    }


def test_models_are_immutable() -> None:
    run = make_run_ref()
    with pytest.raises(ValidationError, match="frozen"):
        run.run_id = 99  # type: ignore[misc]


def test_branch_key_includes_head_repo_so_forks_do_not_collide() -> None:
    upstream = make_run_ref(head_repo_full_name="acme/widgets", head_branch="fix")
    fork = make_run_ref(head_repo_full_name="someone/widgets", head_branch="fix")

    assert upstream.branch_key == "acme/widgets:fix"
    assert fork.branch_key == "someone/widgets:fix"
    assert upstream.branch_key != fork.branch_key


@pytest.mark.parametrize("confidence", [-0.1, 1.1])
def test_diagnosis_confidence_must_be_between_zero_and_one(confidence: float) -> None:
    with pytest.raises(ValidationError):
        Diagnosis.model_validate({**make_diagnosis().model_dump(), "confidence": confidence})


def test_diagnosis_allows_at_most_eight_evidence_items() -> None:
    evidence = [{"line": n, "text": "x"} for n in range(9)]
    with pytest.raises(ValidationError):
        Diagnosis.model_validate({**make_diagnosis().model_dump(), "evidence": evidence})


def test_evidence_text_is_capped_at_300_characters() -> None:
    assert Evidence(line=1, text="x" * 300)
    with pytest.raises(ValidationError):
        Evidence(line=1, text="x" * 301)


def test_publication_key_factories_set_the_matching_scope() -> None:
    check = PublicationKey.check(repo_id=1, run_id=2)
    comment = PublicationKey.comment(repo_id=1, pr_number=3, workflow_id=4)

    assert (check.kind, check.run_id, check.pr_number) == (PublicationKind.CHECK, 2, None)
    assert (comment.kind, comment.pr_number, comment.workflow_id) == (
        PublicationKind.COMMENT,
        3,
        4,
    )


def test_publication_key_rejects_fields_that_do_not_match_its_kind() -> None:
    with pytest.raises(ValidationError):
        PublicationKey(kind=PublicationKind.CHECK, repo_id=1, run_id=2, pr_number=3)
    with pytest.raises(ValidationError):
        PublicationKey(kind=PublicationKind.COMMENT, repo_id=1, run_id=2)


def test_repo_config_has_defaults_and_rejects_unknown_keys() -> None:
    config = RepoConfig()
    assert (config.comment, config.llm, config.ignore_workflows) == (True, "auto", [])

    with pytest.raises(ValidationError):
        RepoConfig.model_validate({"unknown_key": True})


def _similar(**changes: object) -> SimilarFailure:
    fields: dict[str, object] = {
        "failure_id": uuid4(),
        "run_id": 5000,
        "run_attempt": 1,
        "score": 0.9,
        "category": Category.DEPENDENCY,
        "probable_cause": "requests is missing",
        "fix_commit_sha": None,
    }
    return SimilarFailure.model_validate(fields | changes)


def test_a_similar_failure_rounds_its_score_to_three_decimals() -> None:
    assert _similar(score=0.912_345_678).score == 0.912
    assert _similar(score=1.000_000_000_000_000_2).score == 1.0


@pytest.mark.parametrize("changes", [{"run_id": -1}, {"run_attempt": 0}])
def test_a_similar_failure_rejects_impossible_ids(changes: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        _similar(**changes)


# --- the explain trigger and the upgrade of a published diagnosis (D-95) -----------------------


def test_a_diagnose_run_payload_is_automatic_unless_it_says_otherwise() -> None:
    body = {
        "run": make_run_ref().model_dump(mode="json"),
        "conclusion": "failure",
        "completed_at": "2026-11-12T12:03:00+00:00",
        "default_branch": "main",
        "private": False,
    }
    old = DiagnoseRunPayload.model_validate(body)
    assert (old.trigger, old.pr_number, old.command_comment_id) == ("automatic", None, None)
    command = DiagnoseRunPayload.model_validate(
        body | {"trigger": "explain", "pr_number": 3, "command_comment_id": 9}
    )
    assert (command.trigger, command.pr_number, command.command_comment_id) == ("explain", 3, 9)
    with pytest.raises(ValidationError):
        DiagnoseRunPayload.model_validate(body | {"trigger": "manual"})


def test_an_upgrade_is_never_a_rules_diagnosis() -> None:
    with pytest.raises(ValidationError, match="llm or a cache"):
        PublishedUpgrade(failure_id=uuid4(), published=make_rules_diagnosis())


def test_an_upgrade_checks_the_same_invariants_as_a_stored_failure() -> None:
    answer = make_diagnosis(model="gpt-a", prompt_version="v1")
    with pytest.raises(ValidationError, match="names the model"):
        PublishedUpgrade(failure_id=uuid4(), published=answer, usage=make_usage(model="gpt-b"))
    with pytest.raises(ValidationError, match="only a cached"):
        PublishedUpgrade(failure_id=uuid4(), published=answer, cached_from=uuid4())
    cached = answer.model_copy(update={"source": "cache"})
    with pytest.raises(ValidationError, match="made no LLM call"):
        PublishedUpgrade(failure_id=uuid4(), published=cached, usage=make_usage(model="gpt-a"))
    assert PublishedUpgrade(failure_id=uuid4(), published=cached, cached_from=uuid4())
