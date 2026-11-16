"""Rules baseline versus LLM (SPEC §9.3 point 4, D-14, D-35).

The rules diagnosis is computed without the LLM. When a confident rule disagrees with the LLM,
both are shown, so a successful prompt injection cannot hide what the independent baseline saw.
"""

from cicd_doctor.core.models import Diagnosis

CONFIDENT_RULE = 0.85  # SPEC §9.3: a rule with at least this confidence


def disagrees(rules: Diagnosis, published: Diagnosis) -> bool:
    """True when a confident rule and the published LLM diagnosis name different categories."""
    return (
        published.source == "llm"
        and rules.source == "rules"
        and rules.confidence >= CONFIDENT_RULE
        and rules.category != published.category
    )


def baseline_to_show(rules: Diagnosis, published: Diagnosis) -> Diagnosis | None:
    """The rules diagnosis to show next to the published one, or None when it adds nothing."""
    return rules if disagrees(rules, published) else None
