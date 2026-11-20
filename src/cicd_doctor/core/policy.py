"""When the LLM may be called (SPEC §9.4, D-16, D-17).

``decide`` is pure and covers the rules that need no state, in the order of the spec; the first
match wins. The last rule, the budget, needs the ``UsageLedger`` and is applied by the diagnosis
pipeline right before the call: a failed reservation falls back with ``budget``.
"""

from typing import Literal

from cicd_doctor.core.models import DomainModel, FallbackReason


class PolicyInput(DomainModel):
    llm_enabled: bool  # LLM_ENABLED, the global kill switch
    repo_llm: Literal["auto", "on_demand", "off"]  # RepoConfig.llm
    is_fork: bool  # the run's head repository differs from the base repository
    explain: bool = False  # triggered by `/cicd-doctor explain` (P3)
    cache_hit: bool = False  # an LLM diagnosis of the same signature within CACHE_TTL_DAYS (P2)


class PolicyDecision(DomainModel):
    action: Literal["llm", "cache", "rules"]
    fallback_reason: FallbackReason | None = None  # set exactly when action is "rules"


def decide(policy: PolicyInput) -> PolicyDecision:
    """Evaluate §9.4 in order; the first match decides."""
    if policy.repo_llm == "off":
        return _rules("config_off")
    if not policy.llm_enabled:
        return _rules("disabled")
    if policy.is_fork and not policy.explain:
        return _rules("fork_policy")
    if policy.repo_llm == "on_demand" and not policy.explain:
        return _rules("on_demand")
    if policy.cache_hit:
        return PolicyDecision(action="cache")
    return PolicyDecision(action="llm")


def _rules(reason: FallbackReason) -> PolicyDecision:
    return PolicyDecision(action="rules", fallback_reason=reason)
