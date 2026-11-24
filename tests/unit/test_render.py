"""Safe rendering (SPEC §7.5, §9.5, D-25, D-64): the check run, the sticky comment, and the
guarantee that untrusted text becomes no link, no mention and no markup."""

import re
import unicodedata
from typing import Any
from uuid import uuid4

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cicd_doctor.core.models import (
    Category,
    Diagnosis,
    DiagnosisReport,
    Evidence,
    Failure,
    FailureStatus,
    FallbackReason,
    SimilarFailure,
)
from cicd_doctor.core.render import (
    ALLOWED_HOSTS,
    CHECK_NAME_PREFIX,
    COULD_NOT_DIAGNOSE,
    FALLBACK_TEXT,
    FOOTER,
    MAX_CHECK_NAME_CHARS,
    MAX_COMMENT_CHARS,
    MAX_SUMMARY_CHARS,
    ZWJ,
    allowed_url,
    check_name,
    code,
    confidence_label,
    fenced,
    flatten,
    inline,
    render_check,
    render_comment,
    render_could_not_diagnose,
    render_title,
)
from evals.runner.markdown_threats import threats
from tests.factories import make_failure, make_run_ref
from tests.redaction_corpus import Planted, planted_secrets


def diagnosis(**changes: Any) -> Diagnosis:
    fields: dict[str, Any] = {
        "category": Category.DEPENDENCY,
        "probable_cause": "The module `requests` is not installed.",
        "evidence": [Evidence(line=10, text="ModuleNotFoundError: No module named 'requests'")],
        "suggested_fix": "Declare requests in the dependencies.",
        "confidence": 0.9,
        "source": "rules",
        "rule_ids": ["PY-MODNF"],
    }
    return Diagnosis(**(fields | changes))


def item(
    job_name: str = "tests", step_name: str | None = "Run pytest", **changes: Any
) -> tuple[Failure, Diagnosis]:
    failure = make_failure()
    job = failure.job.model_copy(
        update={
            "job": failure.job.job.model_copy(update={"job_name": job_name}),
            "step_name": step_name,
        }
    )
    return failure.model_copy(update={"job": job}), diagnosis(**changes)


def report(*items: tuple[Failure, Diagnosis], **changes: Any) -> DiagnosisReport:
    return DiagnosisReport(run=make_run_ref(), items=list(items) or [item()], **changes)


# --- inline text: attacks that must come out inert --------------------

ATTACKS = [
    "@octocat please look",
    "ping @org/team and @everyone",
    "mail me at someone@example.com",
    "&#64;octocat &#x40;octocat &commat;octocat &amp;#64;octocat",
    "see #123, owner/repo#45 and GH-9",
    "[click here](https://evil.example/login)",
    "![pixel](https://evil.example/p.png)",
    "[ref]: https://evil.example\n[ref]",
    "<https://evil.example/autolink>",
    "https://evil.example/x and www.evil.example/y and ftp://evil.example/z",
    "<script>alert(1)</script> <img src=x onerror=alert(1)> <!-- hidden -->",
    "<details><summary>x</summary>y</details>",
    "# Heading\n> quote\n- item\n1. first\n| a | b |\n---\n===",
    "# starts as a heading",
    "> starts as a quote",
    "- starts as a list",
    "1. starts as an ordered list",
    "*bold* _em_ ~~strike~~ __under__",
    "`unbalanced backtick and [x](http://evil.example)",
    "`a` [x](http://evil.example) `b`",
    "``` closing fence ``` # heading",
    "javascript:alert(1) data:text/html,<b>x</b> [x](javascript:alert(1))",
    "https://github.com@evil.example/phish https://github.com.evil.example/",
    "https://evil.example/‮gpj.exe zero​width @​octocat",
    "http://github.com/insecure https://github.com:8443/odd",
    "\x1b]8;;https://evil.example\x07link\x1b]8;;\x07 \x00\x07",
]


@pytest.mark.parametrize("attack", ATTACKS)
def test_untrusted_prose_renders_inert(attack: str) -> None:
    assert threats(inline(attack, 500)) == []


@pytest.mark.parametrize("attack", ATTACKS)
def test_untrusted_text_in_code_and_evidence_renders_inert(attack: str) -> None:
    assert threats(code(attack, 500)) == []
    assert threats(fenced([flatten(attack, 300)])) == []


def test_a_mention_keeps_its_text_but_cannot_notify() -> None:
    assert inline("thanks @octocat", 100) == f"thanks @{ZWJ}octocat"


def test_an_entity_cannot_hide_a_mention() -> None:
    text = inline("&#64;octocat &commat;octocat", 100)

    assert "&#64;" not in text  # the ampersand is escaped: no entity is left to decode
    assert "&commat;" not in text
    assert threats(text) == []


def test_issue_references_are_shown_as_code() -> None:
    assert inline("see #123", 100) == "see `#123`"
    assert inline("owner/repo#45", 100) == "`owner/repo#45`"
    assert inline("GH-9 done", 100) == "`GH-9` done"
    assert inline("version 1.2#3x", 100) == "version 1.2`#3`x"  # matched anywhere: no gaps
    assert inline("#0_", 100) == "`#0`\\_"


def test_a_url_off_the_allowlist_is_inline_code_and_one_on_it_is_an_autolink() -> None:
    assert inline("see https://evil.example/x.", 200) == "see `https://evil.example/x`."
    assert inline("see https://docs.python.org/3/library/os.html.", 200) == (
        "see <https://docs.python.org/3/library/os.html>."
    )
    assert inline("www.evil.example", 100) == "`www.evil.example`"


@pytest.mark.parametrize("host", sorted(ALLOWED_HOSTS))
def test_every_allowlisted_host_is_allowed_over_https(host: str) -> None:
    assert allowed_url(f"https://{host}/a/b?c=d#e")
    assert not allowed_url(f"http://{host}/a")
    assert not allowed_url(f"https://{host}.evil.example/a")
    assert not allowed_url(f"https://evil.example/{host}")
    assert not allowed_url(f"https://{host}@evil.example/")
    assert not allowed_url(f"https://user:pass@{host}/")
    assert not allowed_url(f"https://{host}:8443/")
    assert not allowed_url(f"https://x.{host}/")


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/a b",
        "https://github.com/<x>",
        "https://github.com\\@evil.example",
        "https://gist.github.com/x",
        "ftp://github.com/x",
        "https://github.com/" + "a" * 600,
        "",
    ],
)
def test_odd_urls_are_not_allowed(url: str) -> None:
    assert not allowed_url(url)


def test_backtick_pairs_become_code_spans_and_odd_backticks_are_text() -> None:
    assert inline("run `pip install x` now", 100) == "run `pip install x` now"
    assert inline("a `b", 100) == "a \\`b"
    assert inline("a ` b ` c ``", 100) == "a ` b ` c"  # balanced: code spans, the empty one drops


def test_text_is_one_line_cut_to_its_limit_after_flattening() -> None:
    text = inline("line one\n\n  line   two\t\tthree", 100)

    assert "\n" not in text
    assert text == "line one line two three"
    assert len(flatten("x" * 1000, 500)) == 500


def test_secrets_are_redacted_again_before_rendering() -> None:
    planted = planted_secrets()[0]

    assert planted.secret not in inline(f"leaked {planted.secret}", 500)
    assert planted.secret not in code(planted.secret, 500)
    assert f"[REDACTED:{planted.kind}]" in flatten(planted.line, 500)


def test_invisible_and_bidi_characters_are_dropped() -> None:
    assert flatten("a​b‮c⁦d﻿e", 50) == "abcde"


def test_a_fence_is_longer_than_any_backtick_run_in_its_content() -> None:
    block = fenced(["```", "````` five", "text"])

    assert block.split("\n")[0] == "``````text"
    assert block.endswith("``````")
    assert fenced(["plain"]).startswith("```text\n")
    assert threats(block) == []


# --- title, labels --------------------


@pytest.mark.parametrize(
    ("confidence", "label"),
    [
        (1.0, "high"),
        (0.75, "high"),
        (0.749, "medium"),
        (0.45, "medium"),
        (0.449, "low"),
        (0.0, "low"),
    ],
)
def test_confidence_labels(confidence: float, label: str) -> None:
    assert confidence_label(confidence) == label


def test_the_title_names_the_category_and_the_confidence() -> None:
    assert render_title(report()) == "Likely cause: dependency (high confidence)"
    low = report(item(category=Category.UNKNOWN, confidence=0.0))
    assert render_title(low) == "Likely cause: unknown (low confidence)"


def test_the_title_uses_the_most_confident_job_and_counts_the_others() -> None:
    two = report(item(category=Category.NETWORK, confidence=0.5), item(confidence=0.9))

    assert render_title(two) == "Likely cause: dependency (high confidence) (+1 more)"


def test_without_failed_jobs_there_is_nothing_to_diagnose() -> None:
    empty = DiagnosisReport(run=make_run_ref(), items=[])

    assert render_title(empty) == "Could not diagnose"
    assert "No failed job was found" in render_check(empty).summary


# --- the check run --------------------


def test_the_check_summary_has_a_section_per_job_and_a_footer() -> None:
    out = render_check(report(item("build"), item("lint", step_name=None)))

    assert out.title.startswith("Likely cause: dependency")
    assert out.summary.count("**Job:**") == 2
    assert "**Job:** build · **Step:** Run pytest" in out.summary
    assert "**Job:** lint\n" in out.summary
    assert out.summary.rstrip().endswith(f"_{FOOTER}_")
    assert threats(out.summary, document=True) == []


def test_a_section_shows_cause_evidence_fix_and_source() -> None:
    summary = render_check(report()).summary

    assert "**Probable cause:** The module `requests` is not installed." in summary
    assert (
        "**Evidence:**\n```text\nL10: ModuleNotFoundError: No module named 'requests'\n```"
        in summary
    )
    assert "**Suggested fix:** Declare requests in the dependencies." in summary
    assert "**Source:** `rule PY-MODNF` · confidence high (0.90)" in summary


def test_evidence_line_numbers_are_labels_never_links() -> None:
    summary = render_check(report()).summary

    assert "#step:5:10" not in summary  # D-64: they are positions in the retained log
    assert "L10:" in summary


def test_the_step_is_linked_when_the_repository_name_is_sound() -> None:
    summary = render_check(report()).summary

    assert (
        "[open the step](https://github.com/acme/widgets/actions/runs/5000/job/1#step:5:1)"
        in summary
    )


def test_the_step_link_is_dropped_for_an_odd_repository_name() -> None:
    run = make_run_ref(repo_full_name="acme/w](https://evil.example)")
    failure = make_failure(run=run)
    summary = render_check(DiagnosisReport(run=run, items=[(failure, diagnosis())])).summary

    assert "open the step" not in summary
    assert threats(summary, document=True) == []


def test_a_cut_log_is_said_so() -> None:
    failure, diag = item()
    failure = failure.model_copy(
        update={"excerpt": failure.excerpt.model_copy(update={"truncated": True})}
    )

    assert "only its end was read" in render_check(report((failure, diag))).summary


def test_a_diagnosis_without_supporting_lines_says_so() -> None:
    summary = render_check(report(item(evidence=[], evidence_verified=False))).summary

    assert "No line of the log supports this diagnosis." in summary
    assert "**Evidence:**" not in summary


@pytest.mark.parametrize("reason", sorted(FALLBACK_TEXT))
def test_a_fallback_says_why_the_rules_diagnosis_is_shown(reason: FallbackReason) -> None:
    summary = render_check(report(item(fallback_reason=reason))).summary

    assert f"· {FALLBACK_TEXT[reason]}" in summary


def test_the_fork_and_budget_texts_are_the_ones_the_spec_promises() -> None:
    assert FALLBACK_TEXT["fork_policy"] == "LLM not used for fork PRs"
    assert "limit" in FALLBACK_TEXT["budget"]


def test_the_source_of_each_kind_of_diagnosis() -> None:
    llm = render_check(report(item(source="llm", model="gpt-x-2026-11", prompt_version="diag-v1")))
    cache = render_check(report(item(source="cache")))
    unmatched = render_check(report(item(rule_ids=[], category=Category.UNKNOWN, confidence=0.0)))

    assert "**Source:** `LLM gpt-x-2026-11 · prompt diag-v1`" in llm.summary
    assert "`cached from an earlier run`" in cache.summary
    assert "`rules (no rule matched)`" in unmatched.summary


def test_a_model_name_cannot_carry_markup() -> None:
    evil = "x](https://evil.example) <b>@octocat`"
    summary = render_check(report(item(source="llm", model=evil, prompt_version="v`1"))).summary

    assert threats(summary, document=True) == []


def test_the_rule_baseline_is_shown_when_the_report_carries_it() -> None:
    failure, llm = item(
        source="llm", category=Category.UNKNOWN, model="m", prompt_version="diag-v1"
    )
    rules = diagnosis(confidence=0.9)
    out = render_check(report((failure, llm), rules_baseline={failure.id: rules}))

    assert (
        "**Rule baseline disagrees:** `PY-MODNF` says `dependency` (confidence 0.90)" in out.summary
    )
    assert "Rule baseline" not in render_check(report((failure, llm))).summary


def _similar(run_id: int, **changes: object) -> SimilarFailure:
    fields: dict[str, object] = {
        "failure_id": uuid4(),
        "run_id": run_id,
        "run_attempt": 1,
        "score": 0.91,
        "category": Category.DEPENDENCY,
        "probable_cause": "requests was missing; see https://evil.example/x @octocat",
        "fix_commit_sha": "abc1234",
    }
    return SimilarFailure(**(fields | changes))  # type: ignore[arg-type]


def test_the_check_lists_up_to_three_similar_runs_with_a_link_built_from_ids() -> None:
    failure, diag = item()
    found = [_similar(900 + i) for i in range(4)]
    rep = report((failure, diag), similar={failure.id: found})

    summary = render_check(rep).summary

    assert "**Similar past failures:**" in summary
    repo = failure.job.job.run.repo_full_name
    for run_id in (900, 901, 902):
        assert f"[run #{run_id}](https://github.com/{repo}/actions/runs/{run_id})" in summary
    assert "run #903" not in summary
    assert (
        " · `dependency` (score 0.91): requests was missing; see `https://evil.example/x` @"
        in summary
    )
    assert "fixed by `abc1234`" in summary
    assert threats(summary, document=True) == []


def test_the_comment_lists_only_the_best_similar_run_on_one_line() -> None:
    failure, diag = item()
    rep = report((failure, diag), similar={failure.id: [_similar(900), _similar(901)]})

    comment = render_comment(rep)

    assert "**Similar past failure:**" in comment
    assert "run #900" in comment
    assert "run #901" not in comment
    assert threats(comment, document=True) == []


def test_a_bad_commit_or_repository_name_never_makes_a_link_or_markup() -> None:
    failure, diag = item()
    bad = _similar(900, fix_commit_sha="abc](https://evil.example/x)")
    rep = report((failure, diag), similar={failure.id: [bad]})

    assert "fixed by" not in render_check(rep).summary
    run = failure.job.job.run.model_copy(update={"repo_full_name": "evil.example/x)(y"})
    odd = failure.model_copy(
        update={
            "job": failure.job.model_copy(
                update={"job": failure.job.job.model_copy(update={"run": run})}
            )
        }
    )
    rep = report((odd, diag), similar={odd.id: [_similar(900)]})

    summary = render_check(rep).summary
    assert "[run #900](" not in summary
    assert "run #900" in summary
    assert threats(summary, document=True) == []


def test_no_similar_section_without_similar_failures() -> None:
    assert "Similar past" not in render_check(report()).summary


# --- the sticky comment --------------------


def test_the_comment_starts_with_the_hidden_marker() -> None:
    comment = render_comment(report())

    assert comment.startswith("<!-- cicd-doctor:v1 pr=12 workflow=7 -->\n")
    assert comment.splitlines()[1:3] == [
        "### CICD Doctor: CI",
        "**Likely cause: dependency (high confidence)**",
    ]
    assert comment.rstrip().endswith(f"_{FOOTER}_")
    assert threats(comment, document=True) == []


def test_the_comment_heading_names_the_workflow_inertly() -> None:
    hostile = "# [x](https://evil.example) @octocat <b>ci</b> #12"
    rep = DiagnosisReport(run=make_run_ref(workflow_name=hostile), items=report().items)

    comment = render_comment(rep)

    heading = comment.splitlines()[1]
    assert heading.startswith("### CICD Doctor: ")
    assert threats(comment, document=True) == []
    assert comment.splitlines()[2].startswith("**Likely cause")  # the heading is one line


def test_a_workflow_without_a_name_leaves_the_heading_plain() -> None:
    rep = DiagnosisReport(run=make_run_ref(workflow_name="  ​ "), items=report().items)

    assert render_comment(rep).splitlines()[1] == "### CICD Doctor"


# --- the name of the check run (D-76) --------------------


def test_the_check_name_is_the_workflow_name_after_the_prefix() -> None:
    assert check_name("CI") == "CICD Doctor: CI"
    assert check_name("  Build   and\ttest\nnow ") == "CICD Doctor: Build and test now"


def test_a_long_workflow_name_is_cut_with_an_ellipsis() -> None:
    name = check_name("w" * 200)

    assert name == "CICD Doctor: " + "w" * 59 + "…"
    assert len(name) <= MAX_CHECK_NAME_CHARS
    assert check_name("w" * 60) == "CICD Doctor: " + "w" * 60  # exactly at the limit: not cut


def test_a_workflow_name_with_markup_is_plain_text_not_escaped() -> None:
    assert check_name("<b>ci</b> & **deploy** @octocat https://x.example/a") == (
        "CICD Doctor: <b>ci</b> & **deploy** @octocat https://x.example/a"
    )


def test_a_workflow_name_with_nothing_visible_gives_the_bare_prefix() -> None:
    assert check_name("") == "CICD Doctor"
    assert check_name(" \t\n\u200b\u202e\x1b[0m ") == "CICD Doctor"


def test_a_secret_in_the_workflow_name_does_not_reach_the_check_name() -> None:
    for planted in planted_secrets():
        assert planted.secret not in check_name(f"deploy {planted.line}")


@settings(max_examples=300, deadline=None)
@given(name=st.text(max_size=300))
def test_the_check_name_is_always_short_one_line_and_clean(name: str) -> None:
    result = check_name(name)

    assert result.startswith(CHECK_NAME_PREFIX)
    assert len(result) <= MAX_CHECK_NAME_CHARS
    assert result == check_name(name)  # deterministic
    assert result == " ".join(result.split())  # one line, no odd whitespace
    assert all(unicodedata.category(c) not in {"Cc", "Cf", "Zl", "Zp"} for c in result)
    assert result == result.strip()


@settings(max_examples=100, deadline=None)
@given(
    planted=st.sampled_from(planted_secrets()),
    before=st.text(max_size=30),
    after=st.text(max_size=30),
)
def test_a_planted_secret_never_comes_out_of_the_check_name(
    planted: Planted, before: str, after: str
) -> None:
    assert planted.secret not in check_name(f"{before} {planted.line} {after}")


def test_the_comment_shows_the_attempt_only_after_the_first() -> None:
    first = render_comment(report())
    rerun = DiagnosisReport(run=make_run_ref(run_attempt=2), items=report().items)

    assert "Attempt" not in first
    assert "\nAttempt 2\n" in render_comment(rerun)


def test_the_comment_is_shorter_than_the_summary() -> None:
    evidence = [Evidence(line=i, text="e" * 200) for i in range(1, 9)]
    rep = DiagnosisReport(
        run=make_run_ref(), items=[(make_failure(), diagnosis(evidence=evidence))]
    )

    comment, summary = render_comment(rep), render_check(rep).summary

    assert comment.count("\nL") == 3  # only the first three evidence lines
    assert summary.count("\nL") == 8
    assert len(comment) < len(summary)


def test_a_pull_request_without_a_number_still_has_a_valid_marker() -> None:
    rep = DiagnosisReport(run=make_run_ref(pr_number=None), items=report().items)

    assert render_comment(rep).startswith("<!-- cicd-doctor:v1 pr=0 workflow=7 -->")


# --- sizes --------------------


def test_a_huge_report_stays_within_the_limits_and_says_what_it_left_out() -> None:
    items = [
        item(f"job {i}", probable_cause="c" * 500, suggested_fix="f" * 1000) for i in range(80)
    ]
    rep = DiagnosisReport(run=make_run_ref(), items=items)

    summary, comment = render_check(rep).summary, render_comment(rep)

    assert len(summary) <= MAX_SUMMARY_CHARS
    assert len(comment) <= MAX_COMMENT_CHARS
    assert re.search(r"_\d+ more section\(s\) not shown\._", summary)
    assert re.search(r"_\d+ more section\(s\) not shown\._", comment)
    assert comment.rstrip().endswith(f"_{FOOTER}_")


def test_text_longer_than_its_field_limit_is_cut() -> None:
    summary = render_check(report(item(probable_cause="p" * 499, suggested_fix="s" * 999))).summary

    assert "p" * 499 in summary
    assert "s" * 999 in summary
    long_cause = inline("x" * 2000, 500)
    assert len(long_cause) == 500


# --- when there is nothing to show --------------------


@pytest.mark.parametrize("reason", sorted(COULD_NOT_DIAGNOSE))
def test_could_not_diagnose_uses_fixed_texts(reason: str) -> None:
    out = render_could_not_diagnose(reason)

    assert out.title == "Could not diagnose"
    assert COULD_NOT_DIAGNOSE[reason] in out.summary
    assert out.summary.rstrip().endswith(f"_{FOOTER}_")
    assert threats(out.summary, document=True) == []


def test_an_unknown_reason_is_never_echoed() -> None:
    out = render_could_not_diagnose("[x](https://evil.example) @octocat")

    assert "evil.example" not in out.summary
    assert COULD_NOT_DIAGNOSE["internal_error"] in out.summary


# --- whole documents built from hostile content --------------------


def test_a_hostile_job_name_step_and_diagnosis_render_inert() -> None:
    hostile = (
        "@everyone [pay now](https://evil.example/pay) ![x](https://evil.example/i.png) #1 <b>"
    )
    rep = report(
        item(
            job_name=hostile,
            step_name=hostile,
            probable_cause=hostile + "\n# Heading",
            suggested_fix="Visit https://evil.example/fix or " + hostile,
            evidence=[Evidence(line=3, text=hostile)],
        ),
    )

    assert threats(render_check(rep).summary, document=True) == []
    assert threats(render_comment(rep), document=True) == []


FUZZ_ALPHABET = [
    *"@#<>[]()!`*_|&:/\\ \n\t-+.=~$%abcXYZ019",
    "http",
    "https://",
    "www.",
    "&#64;",
    "github.com",
    "evil.example",
    "@octocat",
    "javascript:",
    "‍",
    "\x1b[0m",
    "1.",
]
FUZZ_TEXT = st.lists(st.sampled_from(FUZZ_ALPHABET), max_size=40).map("".join)


@settings(max_examples=400, deadline=None)
@given(text=st.one_of(FUZZ_TEXT, st.text(max_size=80)))
def test_no_text_makes_a_link_a_mention_or_markup(text: str) -> None:
    assert threats(inline(text, 500)) == []
    assert threats(code(text, 500)) == []
    assert threats(fenced([flatten(text, 300)])) == []


@settings(max_examples=150, deadline=None)
@given(cause=FUZZ_TEXT, fix=FUZZ_TEXT, job=FUZZ_TEXT, evidence=FUZZ_TEXT)
def test_no_report_built_from_fuzzed_text_is_unsafe(
    cause: str, fix: str, job: str, evidence: str
) -> None:
    rep = report(
        item(
            job_name=job or "job",
            probable_cause=cause or "c",
            suggested_fix=fix or "f",
            evidence=[Evidence(line=1, text=evidence[:300] or "e")],
        )
    )

    assert threats(render_check(rep).summary, document=True) == []
    assert threats(render_comment(rep), document=True) == []


@settings(max_examples=150, deadline=None)
@given(name=st.one_of(FUZZ_TEXT, st.text(max_size=100)))
def test_no_workflow_name_makes_the_comment_unsafe(name: str) -> None:
    rep = DiagnosisReport(run=make_run_ref(workflow_name=name), items=report().items)

    assert threats(render_comment(rep), document=True) == []


@pytest.mark.parametrize("name", ["_://", "a://", "://x", "_:// ", "x_://y", "\\_://", "_://_://"])
def test_a_scheme_separator_in_a_workflow_name_is_never_a_bare_url(name: str) -> None:
    # Found by the fuzz test above: `_://` is no URL, but once escaped (`\_://`) a reader that
    # skips the escape sees `a://`. Fixed in the renderer, not in the oracle.
    rep = DiagnosisReport(run=make_run_ref(workflow_name=name), items=report().items)

    assert threats(render_comment(rep), document=True) == []
    assert threats(render_check(rep).summary, document=True) == []


def test_a_cached_diagnosis_names_the_run_it_came_from_as_a_number() -> None:
    failure, cached = item(source="cache")

    named = render_check(report((failure, cached), cached_from_runs={failure.id: 5000})).summary
    anonymous = render_check(report((failure, cached))).summary

    assert "`cached from run #5000`" in named
    assert "`cached from an earlier run`" in anonymous


# --- resolved failures: the banner (D-84) ---------------------------------


FAILING_SHA, FIX_SHA = "a" * 40, "b" * 40


def resolved(status: FailureStatus, fix_sha: str | None = None, **diagnosis_changes: Any) -> Any:
    failure, diag = item(**diagnosis_changes)
    return failure.model_copy(update={"status": status, "fix_commit_sha": fix_sha}), diag


def test_a_flaky_failure_says_so_with_the_exact_message() -> None:
    rep = report(resolved(FailureStatus.RESOLVED_FLAKY))

    comment = render_comment(rep)

    assert "**✅ Passed on re-run without code changes — recorded as flaky**" in comment
    assert "Likely cause" not in comment
    assert render_title(rep) == "✅ Passed on re-run without code changes — recorded as flaky"
    assert "Compare the changes" not in comment
    assert "**Probable cause:**" in comment  # the compact diagnosis stays below


def test_a_fixed_failure_says_the_commit_shortened_and_links_the_comparison() -> None:
    rep = report(resolved(FailureStatus.RESOLVED_BY_COMMIT, FIX_SHA))

    comment = render_comment(rep)

    assert "**✅ Fixed by `bbbbbbb`**" in comment
    assert "Likely cause" not in comment
    assert render_title(rep) == "✅ Fixed by bbbbbbb"
    link = (
        f"[Compare the changes](https://github.com/acme/widgets/compare/{FAILING_SHA}...{FIX_SHA})"
    )
    assert link in comment
    assert threats(comment, document=True) == []
    summary = render_check(rep).summary
    assert summary.startswith("✅ Fixed by `bbbbbbb`")
    assert threats(summary, document=True) == []


@pytest.mark.parametrize(
    "bad",
    [
        "abc](https://evil.example/x)",
        "b" * 39,
        "B" * 40,
        "b" * 41,
        "b" * 39 + "\n",
        None,
        "",
    ],
)
def test_a_malformed_fix_sha_gets_no_sha_and_no_link(bad: str | None) -> None:
    rep = report(resolved(FailureStatus.RESOLVED_BY_COMMIT, bad))

    comment = render_comment(rep)

    assert "✅ Fixed by a later commit" in comment
    assert "compare" not in comment.lower()
    assert "evil.example" not in comment
    assert threats(comment, document=True) == []


def test_an_odd_repository_name_or_failing_sha_gets_no_comparison_link() -> None:
    failure, diag = resolved(FailureStatus.RESOLVED_BY_COMMIT, FIX_SHA)
    run = failure.job.job.run.model_copy(update={"repo_full_name": "evil.example/x)(y"})
    odd = failure.model_copy(
        update={
            "job": failure.job.model_copy(
                update={"job": failure.job.job.model_copy(update={"run": run})}
            )
        }
    )
    other = failure.model_copy(
        update={
            "job": failure.job.model_copy(
                update={
                    "job": failure.job.job.model_copy(
                        update={"run": failure.job.job.run.model_copy(update={"head_sha": "xyz"})}
                    )
                }
            )
        }
    )

    for subject in (odd, other):
        comment = render_comment(report((subject, diag)))
        assert "✅ Fixed by `bbbbbbb`" in comment
        assert "Compare the changes" not in comment
        assert threats(comment, document=True) == []


def test_mixed_or_open_failures_keep_the_likely_cause_title() -> None:
    fixed = resolved(FailureStatus.RESOLVED_BY_COMMIT, FIX_SHA)
    flaky = resolved(FailureStatus.RESOLVED_FLAKY)
    still_open = item()

    for rep in (report(fixed, flaky), report(fixed, still_open), report(still_open)):
        assert render_title(rep).startswith("Likely cause")
        assert "✅" not in render_comment(rep)


def test_a_stale_failure_has_no_banner() -> None:
    failure, diag = item()
    stale = failure.model_copy(update={"status": FailureStatus.STALE})

    assert render_title(report((stale, diag))).startswith("Likely cause")


def test_a_resolved_comment_with_a_poisoned_diagnosis_is_still_inert() -> None:
    attack = (
        "[click](https://evil.example/login) @octocat <script>x</script> ![i](https://e.x/p.png)"
    )
    rep = report(
        resolved(
            FailureStatus.RESOLVED_BY_COMMIT,
            FIX_SHA,
            probable_cause=attack,
            suggested_fix=attack,
            evidence=[Evidence(line=1, text=attack)],
        )
    )

    for markdown in (render_comment(rep), render_check(rep).summary):
        assert threats(markdown, document=True) == []
        assert "evil.example" not in re.sub(r"`[^`]*`", "", markdown)
