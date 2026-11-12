import pytest
from pydantic import ValidationError

from cicd_doctor.core.models import (
    Category,
    Diagnosis,
    Evidence,
    PublicationKey,
    PublicationKind,
    RepoConfig,
)
from tests.factories import make_diagnosis, make_run_ref


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
