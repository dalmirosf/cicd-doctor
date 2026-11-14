"""Python rules: imports, dependency resolution, pytest, ruff and mypy (SPEC §10)."""

import re

from cicd_doctor.core.models import Category, RuleMatch
from cicd_doctor.core.rules.engine import Rule, RuleContext, compile_all

_MODULE_NOT_FOUND = re.compile(r"ModuleNotFoundError: No module named '(?P<module>[^']+)'")


def _collection_error_category(context: RuleContext, match: RuleMatch) -> RuleMatch:
    """A collection error caused by a missing module is a dependency problem (SPEC §10)."""
    if any(_MODULE_NOT_FOUND.search(line.text) for line in context.excerpt.lines):
        return match.model_copy(update={"category": Category.DEPENDENCY})
    return match


PYTHON_RULES: tuple[Rule, ...] = (
    Rule(
        id="PY-MODNF",
        ecosystem="python",
        patterns=(_MODULE_NOT_FOUND,),
        category=Category.DEPENDENCY,
        confidence=0.90,
        priority=2,
        cause="The module `{module}` is imported but not installed in the environment.",
        fix=(
            "Declare the package that provides `{module}` in the project's dependencies "
            "(for example `pyproject.toml`) and reinstall; check the module name for typos."
        ),
    ),
    Rule(
        id="PY-IMPORT",
        ecosystem="python",
        patterns=compile_all(r"ImportError: cannot import name '(?P<name>[^']+)'"),
        category=Category.DEPENDENCY,
        confidence=0.70,
        priority=2,
        cause="`{name}` cannot be imported: it does not exist in the installed version.",
        fix=(
            "Pin the dependency to a version that still provides `{name}`, or update the import "
            "to the new name or location."
        ),
    ),
    Rule(
        id="PY-RESOLVE",
        ecosystem="python",
        patterns=compile_all(
            r"ResolutionImpossible",
            r"No solution found when resolving dependencies",
            r"Because .* depends on .* we can conclude",
        ),
        category=Category.DEPENDENCY,
        confidence=0.90,
        priority=2,
        cause="The dependency requirements conflict: no set of versions satisfies all of them.",
        fix=(
            "Read the resolver's explanation, then relax or align the conflicting version "
            "constraints (and refresh the lock file)."
        ),
    ),
    Rule(
        id="PY-NO-DIST",
        ecosystem="python",
        patterns=compile_all(
            r"No matching distribution found for (?P<pkg>\S+)",
            r"Because (?P<pkg>\S+) was not found in the package registry",
        ),
        category=Category.DEPENDENCY,
        confidence=0.85,
        priority=3,  # a package that does not exist explains any resolver error around it
        cause="The package `{pkg}` (or the requested version) does not exist in the index.",
        fix=(
            "Check the package name and version; if it is private, configure the index URL and "
            "its credentials; if the version is new, check its Python version support."
        ),
    ),
    Rule(
        id="PY-PYTEST-FAIL",
        ecosystem="python",
        patterns=compile_all(r"^FAILED (?P<test>\S+::\S+)", r"=+ \d+ failed"),
        category=Category.BROKEN_TEST,
        confidence=0.85,
        priority=1,
        cause="The test `{test}` failed.",
        fix="Run the failing test locally, then fix the code or the test's expectation.",
        defaults={"test": "suite"},
    ),
    Rule(
        id="PY-PYTEST-COLLECT",
        ecosystem="python",
        patterns=compile_all(r"\bERROR collecting\b", r"ImportError while importing test module"),
        category=Category.BROKEN_TEST,
        confidence=0.75,
        priority=2,
        cause="pytest could not collect a test module: it fails to import.",
        fix=(
            "Fix the import error shown under the collection error (a missing dependency, a "
            "renamed module or a syntax error)."
        ),
        refine=_collection_error_category,
    ),
    Rule(
        id="PY-RUFF",
        ecosystem="python",
        patterns=(
            re.compile(r"Found \d+ errors?\."),
            re.compile(r"would (be )?reformat", re.IGNORECASE),
        ),
        when=lambda context: context.mentions("ruff"),
        category=Category.BROKEN_TEST,
        confidence=0.80,
        priority=2,
        cause="The lint gate failed: ruff reported errors or unformatted files.",
        fix="Run `ruff check --fix` and `ruff format` locally and commit the result.",
    ),
    Rule(
        id="PY-MYPY",
        ecosystem="python",
        patterns=compile_all(r"Found \d+ errors? in \d+ files?", r"error: .* \[[a-z-]+\]$"),
        category=Category.BROKEN_TEST,
        confidence=0.80,
        priority=2,
        cause="The type-check gate failed: mypy reported type errors.",
        fix="Run mypy locally and fix the reported type errors (or the annotations).",
    ),
)
