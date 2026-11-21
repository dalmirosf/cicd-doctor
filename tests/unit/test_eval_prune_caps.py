"""Two latent defects of the eval tooling (D-115): prune and the judge cache, errors and the caps.

Offline only: no OpenAI, no network, temporary folders (never the committed ``evals/answers/``).
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from cicd_doctor.adapters.memory import FakeLLM
from cicd_doctor.adapters.memory.llm import Fail, valid
from cicd_doctor.core.diagnosis.cost import CostModel
from cicd_doctor.core.errors import LLMError
from cicd_doctor.core.models import LLMResult
from evals.runner.calibration import Pair
from evals.runner.judge import JudgeInput, LLMJudge
from evals.runner.prune import judge_used_entries, prune
from evals.runner.recording import BudgetExceeded, GuardedLLM, SpendGuard
from tests.llmcases import make_request

PRICES = CostModel(input_per_mtok=Decimal("0.40"), output_per_mtok=Decimal("1.60"))


def write(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    return path


# --- H2: prune and the judge's cache --------------------------------------------------------------


def test_prune_does_not_touch_the_judge_cache_by_default(tmp_path: Path) -> None:
    answers = tmp_path / "answers"
    diagnosis_orphan = write(answers / "ab" / "orphan.json")
    judgement = write(answers / "judge" / "cd" / "judgement.json")

    dry = prune(answers, set())
    assert dry.orphans == [diagnosis_orphan]
    assert judgement not in dry.kept + dry.orphans

    prune(answers, set(), apply=True)
    assert judgement.exists()
    assert not diagnosis_orphan.exists()


def test_prune_with_judge_lookups_keeps_them_and_drops_the_rest(tmp_path: Path) -> None:
    answers = tmp_path / "answers"
    current = write(answers / "judge" / "cd" / "current.json")
    stale = write(answers / "judge" / "ef" / "stale.json")

    dry = prune(answers, set(), judge_used={current})
    assert dry.kept == [current]
    assert dry.orphans == [stale]
    assert stale.exists()  # a dry run deletes nothing

    prune(answers, set(), apply=True, judge_used={current})
    assert current.exists()
    assert not stale.exists()


def _pair(pair_id: str, item: JudgeInput) -> Pair:
    return Pair(
        pair_id=pair_id,
        case_id="c",
        variant="llm",
        group="g",
        stratum="s",
        likely_partial=False,
        item=item,
    )


class ScoreClient:
    async def complete(self, system: str, user: str) -> LLMResult:
        raw = json.dumps({"score": 2, "reason": "same"})
        return LLMResult(raw_json=raw, model="j", input_tokens=1, output_tokens=1, latency_ms=1)


async def test_judge_lookups_are_what_a_current_judge_would_read(tmp_path: Path) -> None:
    judge_dir = tmp_path / "answers" / "judge"
    kept_item = JudgeInput("cause", "fix", "answer one", "fix one")
    stale_item = JudgeInput("cause", "fix", "an answer nobody produces", "fix")
    other_model = JudgeInput("cause", "fix", "answer one", "fix one")
    filler = LLMJudge(ScoreClient(), model="judge-m", cache_dir=judge_dir)
    await filler.judge(kept_item)
    await filler.judge(stale_item)
    await LLMJudge(ScoreClient(), model="other-judge", cache_dir=judge_dir).judge(other_model)
    on_disk = sorted(p for p in judge_dir.rglob("*.json"))
    assert len(on_disk) == 3

    offline = LLMJudge(None, model="judge-m", cache_dir=judge_dir)
    used = await judge_used_entries(offline, [], [], [_pair("p1", kept_item)])

    assert used == {judge_dir / filler.key(kept_item)[:2] / f"{filler.key(kept_item)}.json"}
    result = prune(judge_dir.parent, set(), apply=True, judge_used=used)
    assert result.kept == sorted(used)
    assert sorted(p for p in judge_dir.rglob("*.json")) == sorted(used)  # the rest was deleted


# --- H3: a failed call is still a call -----------------------------------------------------------


async def test_a_failed_call_counts_toward_max_calls() -> None:
    guard = SpendGuard(max_cost_usd=Decimal("100"), max_calls=2)
    fake = FakeLLM([Fail(LLMError("refused")), Fail(LLMError("incomplete")), valid()])
    client = GuardedLLM(fake, guard, PRICES)

    with pytest.raises(LLMError, match="refused"):
        await client.diagnose(make_request())
    assert guard.calls == 1
    with pytest.raises(LLMError, match="incomplete"):
        await client.diagnose(make_request())
    assert guard.calls == 2
    assert guard.cost_usd == 0  # an LLMError carries no usage: the cost is unknown

    with pytest.raises(BudgetExceeded, match="max-calls"):
        await client.diagnose(make_request())
    assert len(fake.calls) == 2  # the third call never reached the provider


async def test_budget_exceeded_is_not_counted_as_a_call() -> None:
    guard = SpendGuard(max_cost_usd=Decimal("100"), max_calls=1)
    guard.add(Decimal(0))
    client = GuardedLLM(FakeLLM([valid()]), guard, PRICES)

    with pytest.raises(BudgetExceeded):
        await client.diagnose(make_request())

    assert guard.calls == 1
