"""``python -m evals discover`` (D-101): pairing a failed run with its fix, with a fake ``gh``.

No network and no GitHub. Every case builds a few runs and checks which become candidates and why
the others are discarded.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from evals.runner import cli, public_cli
from evals.runner.capture import CaptureError
from evals.runner.public_common import BudgetedGh
from evals.runner.public_discover import discover, render_candidates
from tests.public_fakes import (
    FIX_SHA,
    INJECTION,
    REPO,
    FakePublicGh,
    compare_json,
    job,
    jobs_payload,
    pull_json,
    run_json,
)

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
SHA = {n: f"{n:040x}" for n in range(1, 8)}  # distinct commit ids


def run(run_id: int, sha_no: int, conclusion: str, day: int, **kw: Any) -> dict[str, Any]:
    return run_json(
        run_id,
        conclusion=conclusion,
        sha=SHA[sha_no],
        created=f"2026-11-{day:02d}T10:00:00Z",
        **kw,
    )


def gh_with(*runs: dict[str, Any], greens_by_attempt: bool = False) -> FakePublicGh:
    """A fake where ``runs`` are every run of workflow 7 on the branch, and the failed ones are
    also answered as the listing of failures."""
    gh = FakePublicGh()
    gh.branch_runs = list(runs)
    gh.failed = [r for r in runs if r["conclusion"] == "failure"]
    for item in runs:
        gh.jobs[f"{item['id']}/{item['run_attempt']}"] = jobs_payload(job(item["id"] * 10, "test"))
    return gh


def find(gh: FakePublicGh, **kwargs: Any):  # type: ignore[no-untyped-def]
    return discover(REPO, gh=gh, now=lambda: NOW, calls=lambda: len(gh.calls), **kwargs)


def reasons(found) -> dict[Any, str]:  # type: ignore[no-untyped-def]
    return {d["run_id"]: d["reason"] for d in found.discarded}


def with_compare(gh: FakePublicGh, failed: int, green: int, **kwargs: int) -> None:
    gh.compares[f"{SHA[failed]}...{SHA[green]}"] = compare_json(**kwargs)


# --- H1 ------------------------------


def test_three_failures_and_a_green_leave_one_candidate_the_last_failure() -> None:
    gh = gh_with(
        run(1, 1, "failure", 10),
        run(2, 2, "failure", 11),
        run(3, 3, "failure", 12),
        run(4, 4, "success", 13),
    )
    with_compare(gh, 3, 4)

    found = find(gh)

    [candidate] = found.candidates
    assert (candidate["run_id"], candidate["pairing"], candidate["fix_run_id"]) == (3, "H1", 4)
    assert candidate["failed_sha"] == SHA[3]
    assert candidate["fix_sha"] == SHA[4]
    assert (candidate["commits"], candidate["files"], candidate["days"]) == (1, 2, 1.0)
    assert (candidate["additions"], candidate["deletions"]) == (6, 2)
    assert reasons(found) == {
        1: "superseded by a later failure",
        2: "superseded by a later failure",
    }


def test_a_green_run_with_the_same_commit_is_not_an_h1_pair() -> None:
    gh = gh_with(run(1, 1, "failure", 10), run(2, 1, "success", 11))

    found = find(gh)

    assert found.candidates == []
    assert reasons(found) == {1: "the green run has the same commit (see H2)"}


def test_a_green_run_of_another_branch_is_no_answer() -> None:
    gh = gh_with(run(1, 1, "failure", 10), run(2, 2, "success", 11, branch="other"))

    found = find(gh)

    assert found.candidates == []
    assert reasons(found) == {1: "no green run after it"}


def test_a_cancelled_run_in_between_is_ignored() -> None:
    gh = gh_with(run(1, 1, "failure", 10), run(2, 2, "cancelled", 11), run(3, 3, "success", 12))
    with_compare(gh, 1, 3)

    [candidate] = find(gh).candidates

    assert (candidate["run_id"], candidate["fix_run_id"]) == (1, 3)


def test_a_green_run_30_days_later_is_too_late() -> None:
    gh = gh_with(run(1, 1, "failure", 1), run(2, 2, "success", 31 - 1))
    gh.branch_runs[1]["created_at"] = "2026-11-30T10:00:00Z"
    with_compare(gh, 1, 2)

    found = find(gh)

    assert found.candidates == []
    assert reasons(found) == {1: "the green run is 29 days later"}
    assert not any("compare" in endpoint for endpoint in gh.endpoints())  # cheap filters first


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"files": 0}, "no files changed"),
        ({"files": 11}, "11 files changed"),
        ({"commits": 4}, "4 commits between the runs"),
    ],
)
def test_the_pair_must_be_small(kwargs: dict[str, int], reason: str) -> None:
    gh = gh_with(run(1, 1, "failure", 10), run(2, 2, "success", 11))
    with_compare(gh, 1, 2, **kwargs)

    found = find(gh)

    assert found.candidates == []
    assert reasons(found) == {1: reason}


# --- H1-pr: a fork PR, paired by pull request ------------------------------


def fork_gh(*runs: dict[str, Any], commits: list[int] | None = None, **pull: Any) -> FakePublicGh:
    """Fork runs on branch names that match nothing; PR 5 has the commits ``commits``."""
    gh = gh_with(*runs)
    shas = commits or [1, 2]
    for item in runs:
        gh.commit_pulls[str(item["head_sha"])] = [pull_json(5, SHA[shas[-1]], **pull)]
    gh.pull_commits[5] = [SHA[n] for n in shas]
    return gh


def fork_run(run_id: int, sha_no: int, conclusion: str, day: int, **kw: Any) -> dict[str, Any]:
    kw.setdefault("branch", f"main-{run_id}")  # a fork's branch name: never matches by branch
    return run(run_id, sha_no, conclusion, day, fork=True, **kw)


def test_a_fork_pr_with_a_later_green_commit_is_paired_by_pull_request() -> None:
    gh = fork_gh(fork_run(1, 1, "failure", 10), fork_run(2, 2, "success", 11))
    with_compare(gh, 1, 2)

    found = find(gh)

    [candidate] = found.candidates
    assert (candidate["pairing"], candidate["pr"], candidate["fix_run_id"]) == ("H1-pr", 5, 2)
    assert (candidate["failed_sha"], candidate["fix_sha"]) == (SHA[1], SHA[2])
    assert found.discarded == []
    # extra calls of one fork candidate: PRs of the commit, commits of the PR, and the runs of the
    # one later commit (on top of the compare and the jobs that H1 also makes)
    endpoints = gh.endpoints()
    assert sum("/pulls" in e for e in endpoints) == 2
    assert sum("head_sha=" in e for e in endpoints) == 1
    assert not any("branch=" in e for e in endpoints)


def test_a_fork_pr_never_fixed_is_discarded() -> None:
    gh = fork_gh(fork_run(1, 1, "failure", 10), commits=[1])

    found = find(gh)

    assert found.candidates == []
    assert reasons(found) == {1: "fork run: no later commit in the pull request"}
    assert not any("head_sha=" in e for e in gh.endpoints())  # stopped after two calls


def test_a_fork_pr_whose_later_commit_has_no_green_run_is_discarded() -> None:
    gh = fork_gh(fork_run(1, 1, "failure", 10))

    assert reasons(find(gh)) == {1: "no green run after it"}


def test_a_force_push_that_drops_the_failed_commit_is_discarded() -> None:
    gone = "fork run: the commit is no longer in the pull request"
    gh = fork_gh(fork_run(1, 1, "failure", 10), commits=[3, 4])
    assert reasons(find(gh)) == {1: gone}

    gh.pull_commits[5] = CaptureError("gh api failed: gh: Not Found (HTTP 404)")
    assert reasons(find(gh)) == {1: gone}

    gh.pull_commits[5] = []
    assert reasons(find(gh)) == {1: gone}


def test_a_fork_commit_without_a_pull_request_is_discarded() -> None:
    gh = fork_gh(fork_run(1, 1, "failure", 10))
    gh.commit_pulls.clear()
    assert reasons(find(gh)) == {1: "fork run: no pull request for the commit"}

    gh.commit_pulls[SHA[1]] = CaptureError("gh api failed: gh: Not Found (HTTP 404)")
    assert reasons(find(gh)) == {1: "fork run: no pull request for the commit"}


def test_a_merged_and_a_closed_unmerged_pr_are_paired_alike() -> None:
    pulls: list[dict[str, Any]] = [
        {"state": "closed", "merged": True},
        {"state": "closed", "merged": False},
    ]
    for pull in pulls:
        gh = fork_gh(fork_run(1, 1, "failure", 10), fork_run(2, 2, "success", 11), **pull)
        with_compare(gh, 1, 2)

        [candidate] = find(gh).candidates

        assert (candidate["pairing"], candidate["pr"]) == ("H1-pr", 5)


# --- H1-pr by owner:branch, when the commit has no pull request ------------------------------


def branch_gh(*runs: dict[str, Any], commits: list[int] | None = None) -> FakePublicGh:
    """Fork runs whose commits have no PR; ``someone:main-1`` has PR 5 with ``commits``."""
    gh = fork_gh(*runs, commits=commits)
    gh.commit_pulls.clear()
    gh.branch_pulls["someone:main-1"] = [pull_json(5, SHA[(commits or [1, 2])[-1]])]
    return gh


def test_a_fork_pr_is_found_by_owner_and_branch_when_the_commit_has_none() -> None:
    gh = branch_gh(fork_run(1, 1, "failure", 10, branch="main-1"), fork_run(2, 2, "success", 11))
    with_compare(gh, 1, 2)

    found = find(gh)

    [candidate] = found.candidates
    assert (candidate["pairing"], candidate["pr"], candidate["pr_lookup"]) == (
        "H1-pr",
        5,
        "owner_branch",
    )
    endpoints = gh.endpoints()
    # extra calls over the commit lookup: the PR list of owner:branch (the commits of the PR are
    # read anyway)
    assert sum("/pulls?state=all&head=someone:main-1" in e for e in endpoints) == 1
    assert sum(e.endswith("/pulls/5/commits?per_page=100") for e in endpoints) == 1


def test_a_candidate_found_by_the_commit_records_that_lookup() -> None:
    gh = fork_gh(fork_run(1, 1, "failure", 10), fork_run(2, 2, "success", 11))
    with_compare(gh, 1, 2)

    [candidate] = find(gh).candidates

    assert candidate["pr_lookup"] == "commit"
    assert not any("state=all" in e for e in gh.endpoints())  # the second lookup was not needed


def test_two_pull_requests_of_one_branch_name_are_told_apart_by_their_commits() -> None:
    gh = branch_gh(fork_run(1, 1, "failure", 10, branch="main-1"), fork_run(2, 2, "success", 11))
    gh.branch_pulls["someone:main-1"] = [pull_json(9, SHA[7]), pull_json(5, SHA[2])]
    gh.pull_commits[9] = [SHA[6], SHA[7]]  # another history under the same name
    with_compare(gh, 1, 2)

    [candidate] = find(gh).candidates

    assert candidate["pr"] == 5
    other_owner = branch_gh(fork_run(1, 1, "failure", 10, branch="main-1", owner="other"))
    other_owner.branch_pulls = {"someone:main-1": [pull_json(5, SHA[2])]}  # not this owner's
    assert reasons(find(other_owner)) == {1: "fork run: no pull request for the commit"}


def test_a_pull_request_whose_commits_lack_the_failed_sha_is_rejected() -> None:
    gh = branch_gh(fork_run(1, 1, "failure", 10, branch="main-1"), commits=[3, 4])

    found = find(gh)

    assert found.candidates == []
    assert reasons(found) == {1: "fork run: no pull request for the commit"}
    assert not any("head_sha=" in e for e in gh.endpoints())


def test_a_deleted_fork_is_discarded_with_its_reason() -> None:
    gh = branch_gh(fork_run(1, 1, "failure", 10, branch="main-1", deleted_fork=True))

    found = find(gh)

    assert found.candidates == []
    assert reasons(found) == {
        1: "fork run: the head repository is gone and no pull request has the commit"
    }
    assert not any("state=all" in e for e in gh.endpoints())  # no owner to ask for


def test_a_closed_unmerged_pr_found_by_branch_is_paired() -> None:
    gh = branch_gh(fork_run(1, 1, "failure", 10, branch="main-1"), fork_run(2, 2, "success", 11))
    gh.branch_pulls["someone:main-1"] = [pull_json(5, SHA[2], state="closed", merged=False)]
    with_compare(gh, 1, 2)

    [candidate] = find(gh).candidates

    assert (candidate["pr"], candidate["pr_lookup"]) == (5, "owner_branch")


def test_a_force_push_found_by_branch_is_discarded() -> None:
    gh = branch_gh(fork_run(1, 1, "failure", 10, branch="main-1"), commits=[3, 4])
    gh.pull_commits[5] = CaptureError("gh api failed: gh: Not Found (HTTP 404)")

    assert reasons(find(gh)) == {1: "fork run: no pull request for the commit"}


def test_the_pull_request_list_is_paginated() -> None:
    gh = branch_gh(fork_run(1, 1, "failure", 10, branch="main-1"), fork_run(2, 2, "success", 11))
    filler = [pull_json(100 + n, SHA[7]) for n in range(100)]
    for pull in filler:
        gh.pull_commits[pull["number"]] = [SHA[7]]
    gh.branch_pulls["someone:main-1"] = [*filler, pull_json(5, SHA[1])]  # page 2
    with_compare(gh, 1, 2)

    found = find(gh)

    assert [c["pr"] for c in found.candidates] == [5]
    pages = [e for e in gh.endpoints() if "state=all" in e]
    assert [e.rsplit("page=", 1)[1] for e in pages] == ["1", "2"]


def test_the_branch_lookup_is_read_once_per_commit_and_counted_in_the_budget() -> None:
    gh = branch_gh(
        fork_run(1, 1, "failure", 10, branch="main-1"),
        fork_run(4, 1, "failure", 10, branch="main-1", workflow_id=8),
        fork_run(2, 2, "success", 11),
        fork_run(5, 2, "success", 11, workflow_id=8),
    )
    gh.jobs["4/1"] = gh.jobs["1/1"]
    with_compare(gh, 1, 2)
    budget = BudgetedGh(gh, max_calls=400)

    found = discover(REPO, gh=budget, now=lambda: NOW, calls=lambda: budget.calls)

    assert len(found.candidates) == 2
    assert sum("state=all" in e for e in gh.endpoints()) == 1
    assert found.api_calls == len(gh.calls) - sum("rate_limit" in e for e in gh.endpoints())


def test_a_later_run_of_another_workflow_is_no_answer() -> None:
    gh = fork_gh(fork_run(1, 1, "failure", 10), fork_run(2, 2, "success", 11, workflow_id=8))
    with_compare(gh, 1, 2)

    found = find(gh)

    assert found.candidates == []
    assert reasons(found) == {1: "no green run after it"}


def test_a_fork_failure_followed_by_another_failure_is_superseded() -> None:
    gh = fork_gh(
        fork_run(1, 1, "failure", 10),
        fork_run(2, 2, "failure", 11),
        fork_run(3, 3, "success", 12),
        commits=[1, 2, 3],
    )
    with_compare(gh, 2, 3)

    found = find(gh)

    assert [c["run_id"] for c in found.candidates] == [2]
    assert reasons(found) == {1: "superseded by a later failure"}


def test_fork_pairing_applies_the_same_filters() -> None:
    gh = fork_gh(fork_run(1, 1, "failure", 10), fork_run(2, 2, "success", 11))
    with_compare(gh, 1, 2, files=11)
    assert reasons(find(gh)) == {1: "11 files changed"}

    gh = fork_gh(fork_run(1, 1, "failure", 10), fork_run(2, 2, "success", 25))
    assert reasons(find(gh)) == {1: "the green run is 15 days later"}

    gh = fork_gh(
        fork_run(1, 1, "failure", 10), fork_run(2, 5, "success", 11), commits=[1, 2, 3, 4, 5]
    )
    assert reasons(find(gh)) == {1: "no green run after it"}  # only the next 3 commits count


def test_fork_pairing_reads_the_pull_request_once_for_two_failures() -> None:
    gh = fork_gh(
        fork_run(1, 1, "failure", 10),
        fork_run(4, 1, "failure", 10, workflow_id=8),
        fork_run(2, 2, "success", 11),
    )
    gh.jobs["4/1"] = gh.jobs["1/1"]
    with_compare(gh, 1, 2)
    find(gh)

    assert sum(e.endswith("/pulls/5/commits?per_page=100") for e in gh.endpoints()) == 1
    assert sum("/commits/" in e and e.endswith("/pulls?per_page=30") for e in gh.endpoints()) == 1


def test_fork_pairing_respects_the_call_budget() -> None:
    gh = fork_gh(fork_run(1, 1, "failure", 10), fork_run(2, 2, "success", 11))
    with_compare(gh, 1, 2)
    budget = BudgetedGh(gh, max_calls=3)  # licence, one failed listing, one more

    found = discover(REPO, gh=budget, now=lambda: NOW, calls=lambda: budget.calls)

    assert found.stopped == "reached --max-api-calls (3)"
    assert found.api_calls == 3


def test_workflows_on_the_exclusion_list_are_discarded() -> None:
    gh = gh_with(
        run(1, 1, "failure", 10, name="PR Checklist"), run(2, 2, "failure", 11, name="Lint gate")
    )
    gh.failed = list(gh.branch_runs)

    found = find(gh, exclude=["gate"])

    assert reasons(found) == {
        1: "workflow is on the exclusion list",
        2: "workflow is on the exclusion list",
    }


def test_the_limit_stops_after_enough_candidates() -> None:
    gh = FakePublicGh()
    for number, (branch, workflow) in enumerate([("a", 7), ("b", 8), ("c", 9)]):
        base = number * 10
        failed = run(base + 1, 1, "failure", 10, branch=branch, workflow_id=workflow)
        green = run(base + 2, 2, "success", 11, branch=branch, workflow_id=workflow)
        gh.branch_runs += [failed, green]
        gh.failed.append(failed)
        gh.jobs[f"{base + 1}/1"] = jobs_payload(job(base + 10, "t"))
    with_compare(gh, 1, 2)

    assert len(find(gh, limit=2).candidates) == 2


# --- the matrix rule ------------------------------


def test_a_matrix_failing_at_the_same_step_takes_the_lowest_job_id() -> None:
    gh = gh_with(run(1, 1, "failure", 10), run(2, 2, "success", 11))
    gh.jobs["1/1"] = jobs_payload(job(30, "t (3.13)"), job(20, "t (3.12)"), job(40, "t (3.11)"))
    with_compare(gh, 1, 2)

    [candidate] = find(gh).candidates

    assert (candidate["job_id"], candidate["failed_jobs"]) == (20, 3)
    assert candidate["step"] == "Run pytest"


def test_a_matrix_failing_at_different_steps_and_a_job_that_never_started_are_discarded() -> None:
    gh = gh_with(run(1, 1, "failure", 10), run(2, 2, "success", 11))
    gh.jobs["1/1"] = jobs_payload(job(20, "a"), job(30, "b", step="Run mypy"))
    with_compare(gh, 1, 2)
    assert reasons(find(gh)) == {1: "failed jobs fail at different steps"}

    gh.jobs["1/1"] = jobs_payload(job(20, "a", step="Set up job"))
    assert reasons(find(gh)) == {1: "the failed job never started its steps"}


# --- H2 ------------------------------


def test_a_later_green_attempt_of_the_same_commit_is_an_h2_candidate() -> None:
    gh = FakePublicGh()
    gh.green = [run(5, 1, "success", 12, attempt=2, name="Tests"), run(6, 2, "success", 12)]
    gh.green[0]["updated_at"] = "2026-11-12T11:00:00Z"
    gh.runs["5/1"] = run(5, 1, "failure", 12, attempt=1)
    gh.jobs["5/1"] = jobs_payload(job(50, "t"))

    found = find(gh)

    [candidate] = found.candidates
    assert (candidate["pairing"], candidate["run_id"], candidate["attempt"]) == ("H2", 5, 1)
    assert (candidate["fix_attempt"], candidate["fix_sha"], candidate["files"]) == (2, SHA[1], 0)
    assert any(endpoint.endswith("/runs/5/attempts/1") for endpoint in gh.endpoints())
    assert not any("compare" in endpoint for endpoint in gh.endpoints())


def test_an_h2_whose_previous_attempt_did_not_fail_is_discarded() -> None:
    gh = FakePublicGh()
    gh.green = [run(5, 1, "success", 12, attempt=2)]
    gh.runs["5/1"] = run(5, 1, "cancelled", 12, attempt=1)

    found = find(gh)

    assert found.candidates == []
    assert reasons(found) == {5: "the previous attempt did not fail"}


# --- licence, budget, output ------------------------------


def test_a_repository_without_an_allowed_licence_reads_nothing_else() -> None:
    gh = gh_with(run(1, 1, "failure", 10))
    gh.repos[REPO] = {"license": {"spdx_id": "AGPL-3.0"}, "private": False}

    found = find(gh)

    assert found.candidates == []
    assert [d["reason"] for d in found.discarded] == ["licence not allowed"]
    assert gh.endpoints() == [f"repos/{REPO}"]


def test_the_budget_stops_discovery_and_says_so() -> None:
    gh = gh_with(run(1, 1, "failure", 10), run(2, 2, "success", 11))
    with_compare(gh, 1, 2)
    budget = BudgetedGh(gh, max_calls=2)

    found = discover(REPO, gh=budget, now=lambda: NOW, calls=lambda: budget.calls)

    assert found.stopped == "reached --max-api-calls (2)"
    assert found.api_calls == 2


def test_candidates_yaml_holds_cleaned_third_party_names_and_no_log_text() -> None:
    gh = gh_with(
        run(1, 1, "failure", 10, name=f"CI\n{INJECTION}‮", branch="feat/x​"),
        run(2, 2, "success", 11, branch="feat/x\u200b"),
    )
    with_compare(gh, 1, 2)

    text = render_candidates([find(gh)])

    document = yaml.safe_load(text)
    [candidate] = document["repos"][0]["candidates"]
    assert "\n" not in candidate["workflow"]
    assert "‮" not in text
    assert "​" not in text
    assert candidate["branch"] == "feat/x"
    assert document["discard_reasons"] == {}
    assert document["repos"][0]["license"] == "MIT"
    assert FIX_SHA not in text  # only the commits of this scenario


def test_discover_is_read_only_and_its_cli_prints_counts_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    gh = gh_with(run(1, 1, "failure", 10, name=INJECTION), run(2, 2, "success", 11))
    with_compare(gh, 1, 2)
    monkeypatch.setattr(public_cli, "run_gh", gh)
    out = tmp_path / "candidates.yaml"

    code = cli.main(["discover", "--repo", REPO, "--since", "30d", "--out", str(out)])

    assert code == 0
    shown = capsys.readouterr().out
    assert f"{REPO}: 1 candidates, 0 discarded" in shown
    assert INJECTION not in shown
    assert yaml.safe_load(out.read_text("utf-8"))["repos"][0]["candidates"][0]["run_id"] == 1
    assert all(call[0] == "api" and "-X" not in call for call in gh.calls)


def test_discover_cli_rejects_a_bad_window(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        cli.main(["discover", "--repo", REPO, "--since", "soon", "--out", str(tmp_path / "c.yaml")])
        == 1
    )
    assert "--since must be a number of days" in capsys.readouterr().err
