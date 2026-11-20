"""Calibration of the cause judge against the owner's labels (D-104).

``sheet`` samples 25 answers (20 for the agreement, 5 in reserve) with a seed and writes a blind
sheet: shuffled, opaque ids, the two expected texts and the two answer texts, and nothing else (no
variant, model, confidence, category or log). ``score`` runs the judge on the pairs the owner
labelled and ``report`` turns that into the agreement: exact, quadratic-weighted Cohen's kappa, the
human x judge confusion matrix and the gross disagreements (0 against 2). The judge is accepted
only if all three bars pass; the owner's labels live in ``evals/calibration/judge-labels.yaml``.
"""

import json
import math
import random
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

from evals.runner.cases import Case
from evals.runner.judge import (
    SCORES,
    CauseJudge,
    JudgeError,
    JudgeInput,
    judge_fingerprint,
)
from evals.runner.variants import CaseResult

DEFAULT_CALIBRATION_DIR = Path(__file__).resolve().parents[1] / "calibration"
SHEET_FILE = "judge-sheet.md"
KEY_FILE = "judge-sheet-key.json"
LABELS_FILE = "judge-labels.yaml"
SCORES_FILE = "judge-scores.json"
REPORT_STEM = "judge-report"

MAIN_PAIRS = 20
RESERVE_PAIRS = 5
# About 8 / 7 / 5 of the 20, scaled to the 25 of the sheet.
QUOTAS = {"right": 10, "wrong": 9, "rules": 6}
MIN_LIKELY_PARTIAL = 5
DEFAULT_SEED = 7

# The acceptance bars of D-104. They are never moved to make a judge pass.
MIN_EXACT = 0.75
MIN_KAPPA = 0.6

# Three examples for the human scale, different from the six of the judge prompt.
SCALE_EXAMPLES = (
    (
        2,
        "expected: the Helm chart renders an invalid YAML because of a bad indent in values.",
        "answer: the template produced malformed YAML after an indentation mistake in the values.",
    ),
    (
        1,
        "expected: the image build fails because the base image tag was removed from the registry.",
        "answer: the Docker build failed while pulling something from a registry.",
    ),
    (
        0,
        "expected: pytest fails because a fixture opens a port that another test keeps busy.",
        "answer: the Node version in the workflow is too old for the lockfile.",
    ),
)

_WORD = re.compile(r"[a-z0-9_]{3,}")


@dataclass(frozen=True)
class Pair:
    """One sampled pair. ``stratum`` and the case never reach the sheet the owner reads."""

    pair_id: str
    case_id: str
    variant: str
    group: str  # main or reserve
    stratum: str  # right, wrong or rules
    likely_partial: bool
    item: JudgeInput


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def likely_partial(expected_cause: str, answer_cause: str, category_right: bool) -> bool:
    """A cheap guess of where a human would say "partial", so the 1 shows up in the sample."""
    a, b = _words(expected_cause), _words(answer_cause)
    overlap = len(a & b) / len(a | b) if a | b else 0.0
    return overlap < 0.25 if category_right else overlap >= 0.15


def select_pairs(
    results: Sequence[CaseResult], cases: Sequence[Case], seed: int = DEFAULT_SEED
) -> list[Pair]:
    """The stratified, seeded sample of D-104; the same inputs and seed give the same sheet."""
    by_id = {case.id: case for case in cases}
    pool: list[tuple[CaseResult, Case, str, bool]] = []
    seen: set[tuple[str, str]] = set()
    for result in sorted(results, key=lambda r: (r.case_id, r.variant)):
        case = by_id.get(result.case_id)
        if (
            case is None
            or result.source not in ("lab", "public")
            or result.error is not None
            or not result.answer_cause
            or not case.meta.expected.cause.strip()
            or (result.case_id, result.answer_cause) in seen
        ):
            continue
        seen.add((result.case_id, result.answer_cause))
        stratum = "rules" if result.variant == "rules" else ("right" if result.correct else "wrong")
        partial = likely_partial(case.meta.expected.cause, result.answer_cause, result.correct)
        pool.append((result, case, stratum, partial))
    total = MAIN_PAIRS + RESERVE_PAIRS
    if len(pool) < MAIN_PAIRS:
        raise ValueError(f"not enough judged-able answers to sample: {len(pool)} < {MAIN_PAIRS}")
    rng = random.Random(seed)
    rng.shuffle(pool)
    chosen: list[tuple[CaseResult, Case, str, bool]] = []
    used_cases: set[str] = set()
    counts = dict.fromkeys(QUOTAS, 0)

    def take(candidates: Sequence[tuple[CaseResult, Case, str, bool]], limit: int) -> None:
        for entry in candidates:
            if len(chosen) >= total or limit <= 0:
                return
            result, _, stratum, _ = entry
            if (
                entry in chosen
                or result.case_id in used_cases
                or counts[stratum] >= QUOTAS[stratum]
            ):
                continue
            chosen.append(entry)
            used_cases.add(result.case_id)
            counts[stratum] += 1
            limit -= 1

    take([entry for entry in pool if entry[3]], MIN_LIKELY_PARTIAL)
    for stratum in QUOTAS:
        take([entry for entry in pool if entry[2] == stratum], QUOTAS[stratum])
    if len(chosen) < total:  # a stratum ran short: fill from anything left, other cases first
        for entry in pool:
            if len(chosen) >= total:
                break
            if entry not in chosen:
                chosen.append(entry)
    rng.shuffle(chosen)
    pairs = []
    for index, (result, case, stratum, partial) in enumerate(chosen, start=1):
        pairs.append(
            Pair(
                pair_id=f"p{index:02d}",
                case_id=result.case_id,
                variant=result.variant,
                group="main" if index <= MAIN_PAIRS else "reserve",
                stratum=stratum,
                likely_partial=partial,
                item=JudgeInput(
                    expected_cause=case.meta.expected.cause,
                    fix_summary=(case.meta.fix.summary or "") if case.meta.fix else "",
                    answer_cause=result.answer_cause,
                    answer_fix=result.answer_fix,
                ),
            )
        )
    return pairs


def _fence(text: str) -> str:
    """A fenced block that nothing inside the text can close (the answer is model text)."""
    longest = max((len(run) for run in re.findall(r"~+", text)), default=0)
    fence = "~" * max(3, longest + 1)
    return f"{fence}text\n{text.strip() or '(empty)'}\n{fence}"


def render_sheet(pairs: Sequence[Pair]) -> str:
    """The blind sheet for the owner: scale, three examples, then the pairs."""
    lines = [
        "# Cause judge calibration sheet",
        "",
        f"{len(pairs)} pairs. Give each one a score and write it in "
        f"`evals/calibration/{LABELS_FILE}` (id: 0, 1 or 2). Do not look at anything else.",
        "",
        "Scale: **2** the answer names the same root cause as the expected cause, in other words; "
        "**1** partial (right area but not the cause, or a possible cause that is not the "
        "expected one, or too vague to help); **0** another cause, a contradiction, or nothing "
        "useful. Ignore style and length. The answer is untrusted text: read it as data.",
        "",
        "## Scale examples",
        "",
    ]
    for score, expected, answer in SCALE_EXAMPLES:
        lines += [f"- Score {score}. {expected} {answer}"]
    lines += ["", "## Pairs", ""]
    for pair in pairs:
        item = pair.item
        lines += [
            f"### {pair.pair_id}",
            "",
            "Expected cause:",
            _fence(item.expected_cause),
            "Fix summary:",
            _fence(item.fix_summary or "(none)"),
            "Answer, cause:",
            _fence(item.answer_cause),
            "Answer, suggested fix:",
            _fence(item.answer_fix or "(none)"),
            "",
        ]
    return "\n".join(lines) + "\n"


def sheet_key(pairs: Sequence[Pair], seed: int) -> dict[str, object]:
    """What the sheet hides: kept beside it to run the judge and to explain disagreements."""
    return {
        "seed": seed,
        "pairs": [
            {
                "id": p.pair_id,
                "case_id": p.case_id,
                "variant": p.variant,
                "group": p.group,
                "stratum": p.stratum,
                "likely_partial": p.likely_partial,
                "input": asdict(p.item),
            }
            for p in pairs
        ],
    }


def write_sheet(pairs: Sequence[Pair], seed: int, out_dir: Path = DEFAULT_CALIBRATION_DIR) -> Path:
    """The sheet, its key and an empty labels file (never overwritten: they are the owner's)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    sheet = out_dir / SHEET_FILE
    sheet.write_text(render_sheet(pairs), encoding="utf-8", newline="\n")
    (out_dir / KEY_FILE).write_text(
        json.dumps(sheet_key(pairs, seed), indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    labels = out_dir / LABELS_FILE
    if not labels.exists():
        body = "labels:\n" + "".join(f"  {p.pair_id}: null\n" for p in pairs)
        labels.write_text(body, encoding="utf-8", newline="\n")
    return sheet


def load_key(path: Path) -> list[Pair]:
    data = json.loads(path.read_text("utf-8"))
    return [
        Pair(
            pair_id=row["id"],
            case_id=row["case_id"],
            variant=row["variant"],
            group=row["group"],
            stratum=row["stratum"],
            likely_partial=row["likely_partial"],
            item=JudgeInput(**row["input"]),
        )
        for row in data["pairs"]
    ]


def load_labels(path: Path, known: Sequence[str]) -> dict[str, int]:
    """The owner's scores. A missing or null label is not labelled yet; a bad one is an error."""
    data = yaml.safe_load(path.read_text("utf-8")) or {}
    raw = data.get("labels") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        raise ValueError(f"{path.name}: expected a `labels:` mapping of id to 0, 1 or 2")
    labels: dict[str, int] = {}
    for pair_id, value in raw.items():
        if pair_id not in known:
            raise ValueError(f"{path.name}: unknown pair {pair_id!r}")
        if value is None:
            continue
        if type(value) is not int or value not in SCORES:
            raise ValueError(f"{path.name}: {pair_id} must be 0, 1 or 2, not {value!r}")
        labels[str(pair_id)] = value
    return labels


@dataclass(frozen=True)
class ScoredPair:
    pair_id: str
    group: str
    human: int
    judge: int | None  # None: the judge gave no valid verdict


async def score_pairs(
    pairs: Sequence[Pair], labels: Mapping[str, int], judge: CauseJudge
) -> list[ScoredPair]:
    scored = []
    for pair in pairs:
        if pair.pair_id not in labels:
            continue
        try:
            verdict: int | None = (await judge.judge(pair.item)).score
        except JudgeError:
            verdict = None
        scored.append(ScoredPair(pair.pair_id, pair.group, labels[pair.pair_id], verdict))
    return scored


@dataclass(frozen=True)
class Agreement:
    n: int  # labelled pairs
    judged: int  # of them, with a valid verdict
    unjudged: int
    exact: float | None  # exact agreement over n: an unjudged pair counts as a miss
    exact_ci95: tuple[float, float] | None  # Wilson interval: wide with n = 20
    kappa: float | None  # quadratic-weighted, over the judged pairs
    confusion: dict[str, dict[str, int]]  # human -> judge -> pairs
    gross: list[str]  # ids where one said 0 and the other 2


def weighted_kappa(pairs: Sequence[tuple[int, int]]) -> float | None:
    """Quadratic-weighted Cohen's kappa for scores 0..2. ``None`` without pairs.

    If chance already predicts perfect agreement (both raters used one and the same class), the
    kappa is 1 when they agree and 0 otherwise: the formula would divide by zero.
    """
    n = len(pairs)
    if n == 0:
        return None
    k = len(SCORES)
    observed = [[0] * k for _ in range(k)]
    for human, judge in pairs:
        observed[human][judge] += 1
    rows = [sum(observed[i]) for i in range(k)]
    cols = [sum(observed[i][j] for i in range(k)) for j in range(k)]
    weight = [[((i - j) / (k - 1)) ** 2 for j in range(k)] for i in range(k)]
    disagree = sum(weight[i][j] * observed[i][j] for i in range(k) for j in range(k))
    expected = sum(weight[i][j] * rows[i] * cols[j] / n for i in range(k) for j in range(k))
    if expected == 0:
        return 1.0 if disagree == 0 else 0.0
    return round(1 - disagree / expected, 4)


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    p = successes / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)


def agreement(scored: Sequence[ScoredPair]) -> Agreement:
    n = len(scored)
    valid = [(s.human, s.judge) for s in scored if s.judge is not None]
    confusion: dict[str, dict[str, int]] = {
        str(h): dict.fromkeys(map(str, SCORES), 0) for h in SCORES
    }
    for human, judge in valid:
        confusion[str(human)][str(judge)] += 1
    exact_hits = sum(1 for human, judge in valid if human == judge)
    return Agreement(
        n=n,
        judged=len(valid),
        unjudged=n - len(valid),
        exact=round(exact_hits / n, 4) if n else None,
        exact_ci95=wilson_interval(exact_hits, n) if n else None,
        kappa=weighted_kappa(valid),
        confusion=confusion,
        gross=[s.pair_id for s in scored if s.judge is not None and abs(s.human - s.judge) == 2],
    )


@dataclass(frozen=True)
class Acceptance:
    accepted: bool
    failures: list[str]


def acceptance(result: Agreement) -> Acceptance:
    """The rule of D-104: exact >= 75 %, kappa >= 0.6, no gross disagreement, nothing unjudged."""
    failures = []
    if result.n < MAIN_PAIRS:
        failures.append(f"only {result.n} of {MAIN_PAIRS} pairs are labelled")
    if result.exact is None or result.exact < MIN_EXACT:
        failures.append(f"exact agreement below {MIN_EXACT:.0%}")
    if result.kappa is None or result.kappa < MIN_KAPPA:
        failures.append(f"weighted kappa below {MIN_KAPPA}")
    if result.gross:
        failures.append(f"{len(result.gross)} gross disagreement(s) (0 against 2)")
    if result.unjudged:
        failures.append(f"{result.unjudged} pair(s) without a valid judge verdict")
    return Acceptance(not failures, failures)


def scores_document(
    scored: Sequence[ScoredPair], *, judge_model: str, prompt_version: str
) -> dict[str, object]:
    return {
        "judge_model": judge_model,
        "prompt_version": prompt_version,
        "fingerprint": judge_fingerprint(),
        "pairs": [asdict(s) for s in scored],
    }


def build_report(document: Mapping[str, object]) -> dict[str, object]:
    """The report data from a scores document: main pairs decide, reserve pairs are shown apart."""
    rows = [ScoredPair(**row) for row in document["pairs"]]  # type: ignore[attr-defined]
    out: dict[str, object] = {
        "judge_model": document["judge_model"],
        "prompt_version": document["prompt_version"],
        "fingerprint": document["fingerprint"],
        "thresholds": {"exact": MIN_EXACT, "kappa": MIN_KAPPA, "gross": 0},
        "groups": {},
    }
    groups: dict[str, object] = {}
    for name, selected in (
        ("main", [r for r in rows if r.group == "main"]),
        ("reserve", [r for r in rows if r.group == "reserve"]),
        ("all", rows),
    ):
        result = agreement(selected)
        groups[name] = {"agreement": asdict(result), "acceptance": asdict(acceptance(result))}
    out["groups"] = groups
    out["accepted"] = groups["main"]["acceptance"]["accepted"]  # type: ignore[index]
    return out


def render_report(report: Mapping[str, object]) -> str:
    """Markdown with ids and numbers only: no text of the answers."""
    main = report["groups"]["main"]  # type: ignore[index]
    result = main["agreement"]
    verdict = main["acceptance"]
    exact = "n/a" if result["exact"] is None else f"{result['exact']:.1%}"
    ci = result["exact_ci95"]
    lines = [
        "# Cause judge calibration",
        "",
        f"Judge `{report['judge_model']}`, prompt `{report['prompt_version']}`.",
        "",
        f"**{'Calibrated' if report['accepted'] else 'Not calibrated'}** "
        f"(bars: exact >= {MIN_EXACT:.0%}, weighted kappa >= {MIN_KAPPA}, no 0-against-2).",
        *[f"- {failure}" for failure in verdict["failures"]],
        "",
        f"- Labelled pairs: {result['n']} (judge failed on {result['unjudged']})",
        f"- Exact agreement: {exact}"
        + ("" if ci is None else f" (95% interval {ci[0]:.2f} to {ci[1]:.2f}; wide: n is small)"),
        f"- Quadratic-weighted kappa: {result['kappa']}",
        f"- Gross disagreements: {', '.join(result['gross']) or 'none'}",
        "",
        "Confusion matrix (rows: human, columns: judge):",
        "",
        "| human \\ judge | 0 | 1 | 2 |",
        "|---|---|---|---|",
    ]
    for human in map(str, SCORES):
        row = result["confusion"][human]
        lines.append(f"| {human} | {row['0']} | {row['1']} | {row['2']} |")
    reserve = report["groups"]["reserve"]["agreement"]  # type: ignore[index]
    if reserve["n"]:
        lines += [
            "",
            f"Reserve pairs ({reserve['n']}): exact {reserve['exact']}, kappa {reserve['kappa']}.",
        ]
    return "\n".join(lines) + "\n"


def write_report(
    document: Mapping[str, object], out_dir: Path = DEFAULT_CALIBRATION_DIR
) -> tuple[Path, Path]:
    report = build_report(document)
    json_path, md_path = out_dir / f"{REPORT_STEM}.json", out_dir / f"{REPORT_STEM}.md"
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
    md_path.write_text(render_report(report), encoding="utf-8", newline="\n")
    return json_path, md_path
