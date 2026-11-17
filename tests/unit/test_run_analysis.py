"""The models of the analysis checkpoint (D-69): ids, invariants and ordering."""

from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from cicd_doctor.core.models import (
    DiagnosisUsage,
    Publication,
    PublicationKey,
    RunAnalysisRecord,
    StoredFailure,
    failure_id_for,
)
from tests.factories import (
    make_diagnosis,
    make_failure,
    make_rules_diagnosis,
    make_run_analysis,
    make_run_attempt,
    make_run_ref,
    make_stored_failure,
    make_usage,
    make_workflow_run,
)

# --- failure_id_for -----------------------------------------------------------------------


def test_the_failure_id_is_deterministic() -> None:
    assert failure_id_for(5000, 1, 7) == failure_id_for(5000, 1, 7)
    assert isinstance(failure_id_for(5000, 1, 7), UUID)
    assert failure_id_for(5000, 1, 7).version == 5


def test_the_failure_id_changes_with_the_run_the_attempt_and_the_job() -> None:
    ids = {failure_id_for(5000, 1, 7), failure_id_for(5001, 1, 7)}
    ids |= {failure_id_for(5000, 2, 7), failure_id_for(5000, 1, 8)}
    assert len(ids) == 4


def test_the_failure_id_does_not_mix_up_neighbouring_numbers() -> None:
    assert failure_id_for(1, 23, 4) != failure_id_for(12, 3, 4)  # "1:23:4" vs "12:3:4"


def test_the_failure_id_is_pinned() -> None:
    """A change of the namespace or of the text would make every stored id unreachable."""
    assert str(failure_id_for(5000, 1, 7)) == "03c002fc-e3fc-5145-8e25-132ae5afe3c1"


# --- StoredFailure ------------------------------------------------------------------------


def test_the_diagnosis_rows_are_one_per_source_with_one_published() -> None:
    llm = make_stored_failure()
    fallback = make_stored_failure(
        published=make_rules_diagnosis().model_copy(update={"fallback_reason": "budget"})
    )
    cache = make_stored_failure(published=make_diagnosis(source="cache"))

    assert [(d.source, p) for d, p in llm.diagnosis_rows()] == [("rules", False), ("llm", True)]
    assert [(d.source, p) for d, p in fallback.diagnosis_rows()] == [("rules", True)]
    assert [(d.source, p) for d, p in cache.diagnosis_rows()] == [("rules", False), ("cache", True)]
    assert fallback.diagnosis_rows()[0][0].fallback_reason == "budget"


def test_a_rules_diagnosis_has_no_model_and_no_reason() -> None:
    failure = make_failure()
    for bad in (
        make_rules_diagnosis(model="gpt-test"),
        make_rules_diagnosis(fallback_reason="budget"),
        make_diagnosis(source="llm"),
    ):
        with pytest.raises(ValidationError, match="plain rules diagnosis"):
            StoredFailure(failure=failure, published=make_diagnosis(), rules=bad)


def test_a_published_rules_diagnosis_is_the_rules_one_plus_a_reason() -> None:
    rules = make_rules_diagnosis()
    other = rules.model_copy(update={"probable_cause": "Something else"})

    with pytest.raises(ValidationError, match="plus a reason"):
        StoredFailure(failure=make_failure(), published=other, rules=rules)
    with pytest.raises(ValidationError, match="plus a reason"):
        StoredFailure(
            failure=make_failure(),
            published=rules.model_copy(update={"model": "gpt-test"}),
            rules=rules,
        )


def test_a_cached_diagnosis_made_no_llm_call() -> None:
    with pytest.raises(ValidationError, match="no LLM call"):
        make_stored_failure(published=make_diagnosis(source="cache"), usage=make_usage())


def test_the_usage_of_an_llm_diagnosis_names_the_same_model() -> None:
    with pytest.raises(ValidationError, match="names the model"):
        make_stored_failure(
            published=make_diagnosis(source="llm", model="gpt-a"), usage=make_usage(model="gpt-b")
        )


def test_a_failed_job_can_cost_nothing_and_name_no_model() -> None:
    usage = DiagnosisUsage(failed_calls=3)

    assert (usage.model, usage.calls, usage.cost_usd) == (None, 0, Decimal(0))


# --- RunAnalysisRecord --------------------------------------------------------------------


def test_the_items_are_ordered_by_job_id_whatever_the_order_given() -> None:
    run = make_run_ref()
    items = [make_stored_failure(run=run, job_id=job_id) for job_id in (30, 10, 20)]

    record = make_run_analysis(run, items)

    assert [i.failure.job.job.job_id for i in record.items] == [10, 20, 30]
    assert record == make_run_analysis(run, list(reversed(items)))


def test_a_record_without_items_is_valid() -> None:
    assert make_run_analysis(items=[]).items == []


def test_the_attempt_must_belong_to_the_run() -> None:
    run = make_run_ref()
    other = make_run_attempt(make_run_ref(run_id=1))

    with pytest.raises(ValidationError, match="another run"):
        RunAnalysisRecord(run=make_workflow_run(run), attempt=other, items=[])


@pytest.mark.parametrize("changes", [{"run_id": 1}, {"run_attempt": 2}, {"repo_id": 1}])
def test_a_failure_of_another_run_attempt_or_repository_is_refused(
    changes: dict[str, int],
) -> None:
    run = make_run_ref()
    alien = make_stored_failure(run=make_run_ref(**changes))

    with pytest.raises(ValidationError, match="another run attempt"):
        make_run_analysis(run, [alien])


def test_the_failure_id_must_be_the_deterministic_one() -> None:
    run = make_run_ref()
    stored = make_stored_failure(run=run)
    random_id = stored.model_copy(update={"failure": make_failure(run=run, job_id=1)})

    with pytest.raises(ValidationError, match="failure_id_for"):
        make_run_analysis(run, [random_id])


def test_a_job_cannot_appear_twice() -> None:
    run = make_run_ref()

    with pytest.raises(ValidationError, match="twice"):
        make_run_analysis(run, [make_stored_failure(run=run), make_stored_failure(run=run)])


# --- Publication --------------------------------------------------------------------------


def test_a_check_publication_belongs_to_the_run_of_its_key() -> None:
    key = PublicationKey.check(repo_id=1, run_id=2)

    assert Publication(key=key, external_id=9, run_id=2, attempt=1).run_id == 2
    with pytest.raises(ValidationError, match="run of its key"):
        Publication(key=key, external_id=9, run_id=3, attempt=1)


def test_a_comment_publication_may_name_a_newer_run_than_its_key_does() -> None:
    key = PublicationKey.comment(repo_id=1, pr_number=2, workflow_id=3)

    assert Publication(key=key, external_id=9, run_id=5001, attempt=1).run_id == 5001


def test_only_a_cached_diagnosis_can_have_an_origin() -> None:
    origin = uuid4()
    cached = make_stored_failure(published=make_diagnosis(source="cache"))
    llm = make_stored_failure(published=make_diagnosis(source="llm"))

    assert StoredFailure.model_validate(cached.model_dump() | {"cached_from": origin}).cached_from
    with pytest.raises(ValidationError, match="only a cached diagnosis has an origin"):
        StoredFailure.model_validate(llm.model_dump() | {"cached_from": origin})
