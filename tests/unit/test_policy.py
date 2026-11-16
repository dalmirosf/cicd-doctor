"""LLM policy (SPEC §9.4, D-16, D-17): the order of the rules decides, and the first match wins."""

from itertools import product

import pytest

from cicd_doctor.core.models import FallbackReason
from cicd_doctor.core.policy import PolicyDecision, PolicyInput, decide


def _input(**changes: object) -> PolicyInput:
    fields: dict[str, object] = {"llm_enabled": True, "repo_llm": "auto", "is_fork": False}
    return PolicyInput(**(fields | changes))  # type: ignore[arg-type]


def test_a_normal_run_calls_the_llm() -> None:
    assert decide(_input()) == PolicyDecision(action="llm")


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"repo_llm": "off"}, "config_off"),
        ({"llm_enabled": False}, "disabled"),
        ({"is_fork": True}, "fork_policy"),
        ({"repo_llm": "on_demand"}, "config_off"),
    ],
)
def test_each_rule_falls_back_with_its_reason(changes: dict[str, object], reason: str) -> None:
    decision = decide(_input(**changes))

    assert decision.action == "rules"
    assert decision.fallback_reason == reason


def test_the_repository_switch_is_checked_before_the_global_kill_switch() -> None:
    assert decide(_input(repo_llm="off", llm_enabled=False)).fallback_reason == "config_off"


def test_the_kill_switch_is_checked_before_the_fork_policy() -> None:
    assert decide(_input(llm_enabled=False, is_fork=True)).fallback_reason == "disabled"


def test_a_fork_is_checked_before_on_demand() -> None:
    assert decide(_input(is_fork=True, repo_llm="on_demand")).fallback_reason == "fork_policy"


def test_an_explain_command_lifts_the_fork_and_on_demand_rules_only() -> None:
    assert decide(_input(is_fork=True, explain=True)).action == "llm"
    assert decide(_input(repo_llm="on_demand", explain=True)).action == "llm"
    assert (
        decide(_input(is_fork=True, explain=True, llm_enabled=False)).fallback_reason == "disabled"
    )
    assert (
        decide(_input(is_fork=True, explain=True, repo_llm="off")).fallback_reason == "config_off"
    )


def test_a_cache_hit_is_used_when_nothing_blocks_the_llm() -> None:
    assert decide(_input(cache_hit=True)) == PolicyDecision(action="cache")
    assert decide(_input(cache_hit=True, is_fork=True)).fallback_reason == "fork_policy"


@pytest.mark.parametrize(
    ("llm_enabled", "repo_llm", "is_fork", "explain", "cache_hit"),
    list(
        product(
            [True, False], ["auto", "on_demand", "off"], [True, False], [True, False], [True, False]
        )
    ),
)
def test_a_decision_is_consistent_for_every_input(
    llm_enabled: bool, repo_llm: str, is_fork: bool, explain: bool, cache_hit: bool
) -> None:
    decision = decide(
        _input(
            llm_enabled=llm_enabled,
            repo_llm=repo_llm,
            is_fork=is_fork,
            explain=explain,
            cache_hit=cache_hit,
        )
    )

    reasons: set[FallbackReason | None] = {"config_off", "disabled", "fork_policy"}
    assert (decision.action == "rules") == (decision.fallback_reason in reasons)
    if repo_llm == "off" or not llm_enabled:
        assert decision.action == "rules"  # off and the kill switch beat even an explain
    if decision.action != "rules":
        assert decision.fallback_reason is None
