"""The committed LLM answers (D-100, D-108): the fingerprint key, the caps, prune and the scanner.

Nothing here calls OpenAI or the network. The scanner test also walks the real ``evals/answers/``.
"""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from cicd_doctor.adapters.llm.cache import ENTRY_FIELDS, CachedLLMClient
from cicd_doctor.adapters.memory import CharTokenCounter, FakeEmbedder, FakeLLM
from cicd_doctor.adapters.memory.llm import valid
from cicd_doctor.core.diagnosis import prompt as prompt_module
from cicd_doctor.core.diagnosis.cost import CostModel
from cicd_doctor.core.diagnosis.prompt import prompt_fingerprint
from cicd_doctor.core.diagnosis.schema import strict_output_schema
from cicd_doctor.core.errors import LLMError
from cicd_doctor.core.models import DiagnosisRequest, LLMResult
from cicd_doctor.core.redaction import redact_text
from evals.runner import cli
from evals.runner.live import DEFAULT_CACHE_DIR, build_setups
from evals.runner.prune import prune, used_entries
from evals.runner.recording import BudgetExceeded, GuardedLLM, RecordingLLM, SpendGuard
from evals.runner.run import EvalError, LLMSetup, run_evals_async
from tests.evalcases import SYNTHETIC, write_case
from tests.llmcases import make_request
from tests.redaction_corpus import planted_secrets

COUNTER = CharTokenCounter()
PRICES = CostModel(input_per_mtok=Decimal("0.40"), output_per_mtok=Decimal("1.60"))
CALL_COST = Decimal("0.00128")  # a FakeLLM reply at PRICES
MAX_ENTRY_BYTES = 16 * 1024


@pytest.fixture
def cases_dir(tmp_path: Path) -> Path:
    root = tmp_path / "cases"
    for fixture in SYNTHETIC:
        write_case(root, fixture)
    return root


def files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.rglob("*") if p.is_file())


class Counting:
    def __init__(self) -> None:
        self.calls = 0

    async def diagnose(self, request: DiagnosisRequest) -> LLMResult:
        self.calls += 1
        return LLMResult(
            raw_json="{}", model="m-real", input_tokens=2000, output_tokens=300, latency_ms=5
        )


# --- the fingerprint ------------------------------------------------------------------------------


def test_the_fingerprint_is_stable_and_sha256() -> None:
    assert prompt_fingerprint() == prompt_fingerprint()
    assert len(prompt_fingerprint()) == 64
    int(prompt_fingerprint(), 16)


def test_one_word_of_the_prompt_changes_the_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    before = prompt_fingerprint()
    changed = prompt_module.SYSTEM_PROMPT.replace("probable", "possible", 1)
    assert changed != prompt_module.SYSTEM_PROMPT
    monkeypatch.setattr(prompt_module, "SYSTEM_PROMPT", changed)

    assert prompt_fingerprint() != before


def test_one_schema_field_changes_the_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    before = prompt_fingerprint()
    original = strict_output_schema

    def changed() -> dict[str, Any]:
        schema = original()
        schema["properties"]["extra_field"] = {"type": "string"}
        return schema

    monkeypatch.setattr(prompt_module, "strict_output_schema", changed)

    assert prompt_fingerprint() != before


def test_the_key_depends_on_the_fingerprint() -> None:
    request = make_request()
    one = CachedLLMClient(None, Path("."), model="m", fingerprint="a" * 64).key(request)
    two = CachedLLMClient(None, Path("."), model="m", fingerprint="b" * 64).key(request)

    assert one != two
    assert CachedLLMClient(None, Path("."), model="m").key(request) not in (one, two)


async def test_an_entry_without_a_matching_fingerprint_is_a_miss(tmp_path: Path) -> None:
    request = make_request()
    client = CachedLLMClient(Counting(), tmp_path, model="m")
    await client.diagnose(request)
    [entry] = files(tmp_path)
    data = json.loads(entry.read_text("utf-8"))

    for fingerprint in (None, "f" * 64):
        damaged = {k: v for k, v in data.items() if k != "fingerprint"}
        if fingerprint:
            damaged["fingerprint"] = fingerprint
        entry.write_text(json.dumps(damaged), encoding="utf-8")
        with pytest.raises(LLMError, match="run with --live to fill them"):
            await CachedLLMClient(None, tmp_path, model="m").diagnose(request)


# --- --no-cache-read ------------------------------------------------------------------------------


async def test_no_cache_read_calls_again_and_never_overwrites_the_official_entry(
    tmp_path: Path,
) -> None:
    request = make_request()
    inner = Counting()
    await CachedLLMClient(inner, tmp_path, model="m").diagnose(request)
    [official] = files(tmp_path)
    before = official.read_bytes()

    again = CachedLLMClient(inner, tmp_path, model="m", read=False, run_suffix="run-1")
    await again.diagnose(request)
    await again.diagnose(request)

    assert inner.calls == 3  # never served from the cache
    assert official.read_bytes() == before
    assert sorted(p.name for p in files(tmp_path)) == sorted(
        [official.name, official.stem + ".run-1.json"]
    )


def test_reading_off_needs_a_live_client_and_a_suffix(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="live client"):
        CachedLLMClient(None, tmp_path, model="m", read=False, run_suffix="x")
    with pytest.raises(ValueError, match="live client"):
        CachedLLMClient(Counting(), tmp_path, model="m", read=False)


# --- the caps -------------------------------------------------------------------------------------


def live_setup(fake: FakeLLM, directory: Path, guard: SpendGuard) -> LLMSetup:
    cache = CachedLLMClient(GuardedLLM(fake, guard, PRICES), directory, model="m")
    recorder = RecordingLLM(cache)
    return LLMSetup(client=recorder, label="m", cost=PRICES, recorder=recorder, cache=cache)


async def test_max_cost_stops_the_run_and_keeps_what_was_written(
    cases_dir: Path, tmp_path: Path
) -> None:
    guard = SpendGuard(max_cost_usd=Decimal("0.003"), max_calls=1000)
    fake = FakeLLM([valid()] * 10)

    with pytest.raises(BudgetExceeded, match="max-cost-usd"):
        await run_evals_async(
            "llm", ["lab"], cases_dir, COUNTER, llms=[live_setup(fake, tmp_path / "answers", guard)]
        )

    assert len(fake.calls) == 3  # 3 x 0.00128 passes the cap; the fourth call is refused
    assert len(files(tmp_path / "answers")) == 3
    assert guard.cost_usd == 3 * CALL_COST


async def test_max_calls_stops_the_run(cases_dir: Path, tmp_path: Path) -> None:
    guard = SpendGuard(max_cost_usd=Decimal("100"), max_calls=2)
    fake = FakeLLM([valid()] * 10)

    with pytest.raises(BudgetExceeded, match="max-calls"):
        await run_evals_async(
            "llm", ["lab"], cases_dir, COUNTER, llms=[live_setup(fake, tmp_path / "answers", guard)]
        )

    assert (len(fake.calls), len(files(tmp_path / "answers"))) == (2, 2)


async def test_a_cache_hit_spends_and_counts_nothing(cases_dir: Path, tmp_path: Path) -> None:
    fake = FakeLLM([valid()] * len(SYNTHETIC))
    first = SpendGuard(max_cost_usd=Decimal("100"), max_calls=100)
    await run_evals_async(
        "llm", ["lab"], cases_dir, COUNTER, llms=[live_setup(fake, tmp_path / "answers", first)]
    )
    second = SpendGuard(max_cost_usd=Decimal("0.0001"), max_calls=1)

    await run_evals_async(
        "llm",
        ["lab"],
        cases_dir,
        COUNTER,
        llms=[live_setup(FakeLLM(), tmp_path / "answers", second)],
    )

    assert (first.calls, second.calls) == (len(SYNTHETIC), 0)


def test_the_caps_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        SpendGuard(max_cost_usd=Decimal(0), max_calls=1)
    with pytest.raises(ValueError, match="positive"):
        SpendGuard(max_cost_usd=Decimal(1), max_calls=0)


def test_live_without_a_guard_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")

    with pytest.raises(EvalError, match="spending caps"):
        build_setups(["m"], live=True, cache_dir=tmp_path)
    with pytest.raises(EvalError, match="needs --live"):
        build_setups(["m"], live=False, cache_dir=tmp_path, read_cache=False)


def test_the_cli_has_default_caps_and_exits_with_its_own_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    args = cli.build_parser().parse_args(["run", "--live"])
    assert (args.max_cost_usd, args.max_calls) == (Decimal("0.25"), 150)
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["run", "--max-calls", "0"])

    def stop(_: object) -> int:
        raise BudgetExceeded("reached --max-calls 3")

    monkeypatch.setattr(cli, "_run", stop)

    assert cli.main(["run"]) == cli.EXIT_BUDGET == 5
    assert "reached --max-calls 3" in capsys.readouterr().err


def test_the_default_answers_folder_is_evals_answers() -> None:
    assert DEFAULT_CACHE_DIR.name == "answers"
    assert DEFAULT_CACHE_DIR.parent.name == "evals"


# --- the recorder ---------------------------------------------------------------------------------


async def test_the_recorder_keeps_the_answers_and_the_calls_of_each_case() -> None:
    recorder = RecordingLLM(FakeLLM([valid(), valid()]))

    recorder.begin("case-a")
    await recorder.diagnose(make_request())
    await recorder.diagnose(make_request(repair_error="x"))
    recorder.begin("case-b")

    first = recorder.cases["case-a"]
    assert first.calls == 2
    assert first.accepted == first.results[-1]
    assert first.accepted is not None
    assert first.accepted.model
    assert recorder.cases["case-b"].calls == 0
    assert recorder.cases["case-b"].accepted is None


# --- prune ----------------------------------------------------------------------------------------


async def test_prune_deletes_only_the_orphans_and_only_when_asked(
    cases_dir: Path, tmp_path: Path
) -> None:
    answers = tmp_path / "answers"
    guard = SpendGuard(max_cost_usd=Decimal("100"), max_calls=100)
    fake = FakeLLM([valid()] * len(SYNTHETIC) * 3)
    await run_evals_async(
        "all",
        ["lab"],
        cases_dir,
        COUNTER,
        llms=[live_setup(fake, answers, guard)],
        embedder=FakeEmbedder(),
    )
    (answers / ".gitkeep").write_text("", encoding="utf-8")
    real = [p for p in files(answers) if p.name != ".gitkeep"]
    orphan = answers / "ab" / ("ab" + "0" * 62 + ".json")
    orphan.parent.mkdir(exist_ok=True)
    orphan.write_text("{}", encoding="utf-8")
    suffixed = real[0].with_name(real[0].stem + ".run-1.json")
    suffixed.write_text("{}", encoding="utf-8")
    offline = build_setups(["m"], live=False, cache_dir=answers)

    used = await used_entries(offline, ["lab"], cases_dir, COUNTER, FakeEmbedder())
    dry = prune(answers, used)

    assert sorted(dry.orphans) == sorted([orphan, suffixed])
    assert not dry.deleted
    assert orphan.exists()
    assert suffixed.exists()

    done = prune(answers, used, apply=True)

    assert sorted(done.kept) == sorted(real)
    assert not orphan.exists()
    assert not suffixed.exists()
    assert all(p.exists() for p in real)
    assert (answers / ".gitkeep").exists()


async def test_prune_drops_the_answers_of_another_model(cases_dir: Path, tmp_path: Path) -> None:
    answers = tmp_path / "answers"
    guard = SpendGuard(max_cost_usd=Decimal("100"), max_calls=100)
    inner = GuardedLLM(FakeLLM([valid()] * 20), guard, PRICES)
    cache = CachedLLMClient(inner, answers, model="old")
    old = LLMSetup(client=cache, label="old", cost=PRICES, cache=cache)
    await run_evals_async("llm", ["lab"], cases_dir, COUNTER, llms=[old])
    current = build_setups(["new"], live=False, cache_dir=answers)

    used = await used_entries(current, ["lab"], cases_dir, COUNTER, FakeEmbedder())
    result = prune(answers, used, apply=True)

    assert result.kept == []
    assert len(result.orphans) == len(SYNTHETIC)
    assert files(answers) == []


# --- the scanner of the committed answers ---------------------------------------------------------


def problems(path: Path) -> list[str]:
    """Why an entry of ``evals/answers/`` must not be committed (D-100); empty when it is fine."""
    found = []
    raw = path.read_bytes()
    if len(raw) > MAX_ENTRY_BYTES:
        found.append(f"{len(raw)} bytes, more than {MAX_ENTRY_BYTES}")
    text = raw.decode("utf-8", errors="replace")
    if redact_text(text).count:
        found.append("contains something shaped like a secret")
    try:
        data = json.loads(text)
        extra = set(data) ^ ENTRY_FIELDS
        if extra:
            found.append(f"fields not exactly {sorted(ENTRY_FIELDS)}: {sorted(extra)}")
        LLMResult.model_validate(data["result"])
    except (ValueError, KeyError, TypeError) as exc:
        found.append(f"not a valid entry ({type(exc).__name__})")
    return found


def committed_entries() -> list[Path]:
    if not DEFAULT_CACHE_DIR.is_dir():
        return []
    return [p for p in files(DEFAULT_CACHE_DIR) if p.name != ".gitkeep"]


def test_every_committed_answer_is_clean() -> None:
    bad = {p.name: problems(p) for p in committed_entries() if problems(p)}

    assert bad == {}


def valid_entry(**changes: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "model": "m",
        "prompt_version": "diag-v3",
        "fingerprint": "a" * 64,
        "result": {
            "raw_json": "{}",
            "model": "m",
            "input_tokens": 1,
            "output_tokens": 1,
            "latency_ms": 1,
        },
    }
    return entry | changes


def write_entry(tmp_path: Path, entry: dict[str, Any]) -> Path:
    path = tmp_path / "ab" / "entry.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(entry), encoding="utf-8")
    return path


def test_the_scanner_accepts_a_good_entry(tmp_path: Path) -> None:
    assert problems(write_entry(tmp_path, valid_entry())) == []


def test_the_scanner_fails_on_a_secret_from_the_redaction_corpus(tmp_path: Path) -> None:
    secret_line = planted_secrets()[0].line
    result = valid_entry()["result"] | {"raw_json": json.dumps({"evidence": secret_line})}

    found = problems(write_entry(tmp_path, valid_entry(result=result)))

    assert any("secret" in item for item in found)


def test_the_scanner_fails_on_extra_fields_size_and_a_bad_result(tmp_path: Path) -> None:
    assert problems(write_entry(tmp_path, valid_entry(request={"log": "x"})))
    huge = valid_entry()["result"] | {"raw_json": "x" * MAX_ENTRY_BYTES}
    assert any("bytes" in p for p in problems(write_entry(tmp_path, valid_entry(result=huge))))
    assert problems(write_entry(tmp_path, valid_entry(result={"raw_json": "{}"})))
    missing = {k: v for k, v in valid_entry().items() if k != "fingerprint"}
    assert problems(write_entry(tmp_path, missing))
