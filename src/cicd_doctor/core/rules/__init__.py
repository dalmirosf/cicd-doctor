"""Rule-based diagnoser: engine and per-ecosystem rules (SPEC §10, D-60)."""

import re

from cicd_doctor.core.rules.docker import DOCKER_RULES
from cicd_doctor.core.rules.engine import (
    Rule,
    RuleContext,
    match_rule,
    match_rules,
    rules_diagnosis,
)
from cicd_doctor.core.rules.generic import GENERIC_RULES
from cicd_doctor.core.rules.python import PYTHON_RULES
from cicd_doctor.core.rules.terraform import TERRAFORM_RULES

RULES: tuple[Rule, ...] = (*GENERIC_RULES, *PYTHON_RULES, *DOCKER_RULES, *TERRAFORM_RULES)

# Every rule pattern also flags error lines in the excerpt (SPEC §7.3 step 6), so the lines a
# rule can match are kept around it.
RULE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    pattern for rule in RULES for pattern in rule.all_patterns()
)

__all__ = [
    "RULES",
    "RULE_PATTERNS",
    "Rule",
    "RuleContext",
    "match_rule",
    "match_rules",
    "rules_diagnosis",
]
