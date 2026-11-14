"""Error signature (SPEC §7.4, D-09, D-59): normalisation, canonical text and hash stability."""

import random
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cicd_doctor.adapters.memory import CharTokenCounter
from cicd_doctor.core.logs import build_excerpt, select_failed_jobs
from cicd_doctor.core.models import LogExcerpt, LogLine, RawLog, WorkflowJob, WorkflowStep
from cicd_doctor.core.signature import (
    canonical_text,
    compute_signature,
    error_type,
    normalize,
    normalize_job_name,
)
from tests.factories import make_run_ref
from tests.logcases import case_excerpts, load_log_case, select_case_jobs

COUNTER = CharTokenCounter()


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("2026-11-13T10:00:05.1234567Z done", "<t> done"),
        ("finished in 0.53s (12ms setup) after 1h 2m", "finished in <t> (<t> setup) after <t>"),
        ("Elapsed 2m 30s, retry in 0:00:03", "Elapsed <t>, retry in <t>"),
        ("request 123e4567-e89b-12d3-a456-426614174000 failed", "request <uuid> failed"),
        ("HEAD is now at 3f2a9c1d4e5b6a7c8d9e0f1a2b3c4d5e6f7a8b9c", "HEAD is now at <hex>"),
        ("object at 0x7f3a2b1c0d", "object at <hex>"),
        (
            'File "/home/runner/work/widgets/widgets/src/app.py", line 42',
            'File "src/app.py", line <n>',
        ),
        (r'File "D:\a\widgets\widgets\src\app.py", line 42', 'File "src/app.py", line <n>'),
        ("/Users/runner/work/widgets/widgets/src/app.py", "src/app.py"),
        ("wrote /tmp/pytest-of-runner/pytest-3/test_a0/out.txt", "wrote <tmp>"),
        (r"script D:\a\_temp\4f1e.ps1 failed", "script <tmp> failed"),
        ("/home/runner/work/_temp/0b9b5f36/x.sh: line 3", "<tmp>: line <n>"),
        ("connect to 10.1.0.12:8080 refused", "connect to <ip>:<port> refused"),
        ("listening on localhost:5432", "listening on localhost:<port>"),
        (
            "GET http://api.internal:9000/v1/items?page=2&q=a",
            "GET http://api.internal:<port>/v1/items",
        ),
        ("bind [fe80::1]:443", "bind [<ip>]:<port>"),
        ("py3.12 v2 test_1 x64 exit code 137", "py3.<n> v2 test_1 x64 exit code <n>"),
        ("token=[REDACTED:github_token] 12 items", "token=[REDACTED:github_token] <n> items"),
        ("  many    spaces\tand tabs  ", "many spaces and tabs"),
        (
            "ModuleNotFoundError: No module named 'requests'",
            "ModuleNotFoundError: No module named 'requests'",
        ),
    ],
)
def test_normalize(line: str, expected: str) -> None:
    assert normalize(line) == expected


@settings(max_examples=300, deadline=None)
@given(st.text(max_size=120))
def test_normalize_is_idempotent_on_any_text(text: str) -> None:
    once = normalize(text)
    assert normalize(once) == once


log_fragments = st.sampled_from(
    [
        "2026-11-13T10:00:05Z",
        "10:00:05.123",
        "1.5s",
        "12 ms",
        "2m 3s",
        "3f2a9c1d4e5b6a7c",
        "0xdeadbeef",
        "123e4567-e89b-12d3-a456-426614174000",
        "/home/runner/work/r/r/src/a.py",
        r"D:\a\r\r\src\a.py",
        "/tmp/x/y",
        "10.0.0.1:80",
        "fe80::1",
        "localhost:5432",
        "https://h.example/p?q=1",
        "py3",
        "137",
        "[REDACTED:jwt]",
        "Error:",
        "'quoted'",
        ":",
        "::",
        ".",
        "-",
    ]
)


@settings(max_examples=300, deadline=None)
@given(st.lists(st.one_of(log_fragments, st.text(alphabet="ab0:./ ", max_size=6)), max_size=10))
def test_normalize_is_idempotent_on_log_like_text(parts: list[str]) -> None:
    for joiner in ("", " "):
        once = normalize(joiner.join(parts))
        assert normalize(once) == once


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("test (ubuntu-latest, 3.12)", "test"),
        ("build / test (windows-latest)", "build / test"),
        ("lint", "lint"),
        ("deploy (prod) now", "deploy (prod) now"),  # only a trailing group is a matrix suffix
    ],
)
def test_normalize_job_name_drops_the_matrix_suffix(name: str, expected: str) -> None:
    assert normalize_job_name(name) == expected


def test_error_type_prefers_the_rule_then_the_exception_then_the_error_marker() -> None:
    texts = [
        "##[error]Process completed with exit code 1.",
        "E   requests.exceptions.ConnectionError: x",
    ]

    assert error_type(texts, "PY-MODNF") == "PY-MODNF"
    assert error_type(texts) == "requests.exceptions.ConnectionError"
    assert error_type(texts[:1]) == "##[error]Process completed with exit code <n>."
    assert error_type(["##[error]Error: Input required: token"]) == "##[error]Error"
    assert error_type(["plain failure"]) == "-"


def _excerpt(lines: list[tuple[int, str]], errors: list[int]) -> LogExcerpt:
    job = select_case_jobs(load_log_case("py-modnf"))[0]
    return LogExcerpt(
        job=job.job,
        step_name=job.step_name,
        lines=[LogLine(number=n, text=t) for n, t in lines],
        omitted=[],
        error_lines=errors,
        token_count=10,
        truncated=False,
        redactions=0,
    )


def test_canonical_text_lists_workflow_job_step_type_and_first_five_error_lines() -> None:
    case = load_log_case("py-modnf")
    job = select_case_jobs(case)[0]
    excerpt = _excerpt(
        [(n, f"Error: thing {n} at 0x{n:08x}") for n in range(1, 8)], list(range(1, 8))
    )

    text = canonical_text(job, excerpt)

    assert text.split("\n") == [
        "CI",
        "build",
        "Run uv run python -m widgets.cli --version",
        "-",
        *["Error: thing <n> at <hex>"] * 5,
    ]


def test_without_error_lines_the_last_lines_stand_in() -> None:
    job = select_case_jobs(load_log_case("py-modnf"))[0]
    excerpt = _excerpt([(1, "one"), (2, ""), (3, "three 3")], [])

    assert canonical_text(job, excerpt).split("\n")[-2:] == ["one", "three <n>"]


def test_the_top_rule_id_changes_the_signature() -> None:
    case = load_log_case("py-modnf")
    job = select_case_jobs(case)[0]
    excerpt = case_excerpts(case, COUNTER)[0]

    assert compute_signature(job, excerpt).hash != compute_signature(job, excerpt, "PY-MODNF").hash
    assert compute_signature(job, excerpt, "PY-MODNF").text.split("\n")[3] == "PY-MODNF"


# --- the same failure from two runs, and different failures ----------------------------------


ERROR_TEMPLATE = [
    "##[group]Run uv run pytest tests/test_api.py",
    "uv run pytest tests/test_api.py",
    "##[endgroup]",
    "============================= test session starts ==============================",
    "platform {platform} -- Python 3.13.7, pytest-8.4.2",
    "rootdir: {workspace}",
    "collected 3 items",
    "",
    "tests/test_api.py ..F",
    "",
    "=================================== FAILURES ===================================",
    "{workspace_file}:{line}: in test_fetch",
    "E   {exception}: HTTPConnectionPool(host='{ip}', port={port}): Max retries exceeded with "
    "url: /v1/items?page={page}&run={run_id} (Caused by NewConnectionError('<urllib3.connection."
    "HTTPConnection object at 0x7f{address}>: Failed to establish a new connection'))",
    "E   request id {request_id}, cache at {tmp}/cache.json",
    "FAILED tests/test_api.py::test_fetch - {exception}: HTTPConnectionPool(host='{ip}', "
    "port={port}) after {duration}s",
    "========================= 1 failed, 2 passed in {duration}s =========================",
    "##[error]Process completed with exit code 1.",
]


def _run_of_the_same_failure(
    seed: int, exception: str = "requests.exceptions.ConnectionError"
) -> tuple[WorkflowJob, RawLog]:
    """A job log and its jobs-API entry for one run: everything that varies between runs varies."""
    rng = random.Random(seed)
    windows = seed % 2 == 1
    start = datetime(2026, 11, 13, 10, 0, tzinfo=UTC) + timedelta(
        days=seed, seconds=rng.randint(0, 9999)
    )
    workspace = r"D:\a\widgets\widgets" if windows else "/home/runner/work/widgets/widgets"
    values = {
        "platform": "win32" if windows else "linux",
        "workspace": workspace,
        "workspace_file": workspace + (r"\tests\test_api.py" if windows else "/tests/test_api.py"),
        "line": "42",
        "exception": exception,
        "ip": f"10.1.{rng.randint(0, 255)}.{rng.randint(1, 254)}",
        "port": str(rng.randint(1024, 65535)),
        "page": str(rng.randint(1, 99)),
        "run_id": str(rng.randint(10**10, 10**11)),
        "address": "".join(rng.choice("0123456789abcdef") for _ in range(10)),
        "request_id": "-".join(
            "".join(rng.choice("0123456789abcdef") for _ in range(n)) for n in (8, 4, 4, 4, 12)
        ),
        "tmp": rf"D:\a\_temp\{rng.randint(1, 9999)}"
        if windows
        else f"/tmp/pytest-of-runner/pytest-{rng.randint(0, 99)}",
        "duration": f"{rng.uniform(0.1, 30):.2f}",
    }
    step_start = start + timedelta(seconds=30)
    lines = []
    for index, template in enumerate(ERROR_TEMPLATE):
        moment = step_start + timedelta(milliseconds=137 * index + rng.randint(0, 99))
        stamp = moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond:06d}0Z"
        lines.append(f"{stamp} {template.format(**values)}")
    step_end = step_start + timedelta(seconds=5)
    job = WorkflowJob(
        job_id=rng.randint(10**10, 10**11),
        name="test (windows-latest, 3.13)" if windows else "test (ubuntu-latest, 3.13)",
        conclusion="failure",
        started_at=start,
        completed_at=step_end,
        runner_os="Windows" if windows else "Linux",
        steps=[
            WorkflowStep(
                number=1,
                name="Set up job",
                conclusion="success",
                started_at=start,
                completed_at=step_start,
            ),
            WorkflowStep(
                number=2,
                name="Run uv run pytest tests/test_api.py",
                conclusion="failure",
                started_at=step_start,
                completed_at=step_end,
            ),
        ],
    )
    return job, RawLog(text="\n".join(lines) + "\n", truncated=False)


def _signature_hash(job: WorkflowJob, raw: RawLog) -> str:
    run = make_run_ref(run_id=job.job_id, run_attempt=1)
    failed = select_failed_jobs(run, [job], limit=3)[0]
    return compute_signature(failed, build_excerpt(failed, raw, COUNTER)).hash


def test_the_same_failure_from_different_runs_has_the_same_hash() -> None:
    hashes = {_signature_hash(*_run_of_the_same_failure(seed)) for seed in range(6)}

    assert len(hashes) == 1  # timestamps, durations, paths, OS, ids, ports and IPs all differ


def test_a_different_exception_has_a_different_hash() -> None:
    same = _signature_hash(*_run_of_the_same_failure(0))
    other = _signature_hash(*_run_of_the_same_failure(0, exception="TimeoutError"))

    assert same != other


def test_different_fixture_failures_have_different_hashes() -> None:
    hashes = []
    for name in ("pytest-assert", "py-modnf", "job-timeout", "docker-copy", "multi-job"):
        case = load_log_case(name)
        for job, excerpt in zip(select_case_jobs(case), case_excerpts(case, COUNTER), strict=True):
            hashes.append(compute_signature(job, excerpt).hash)

    assert len(hashes) == 7
    assert len(set(hashes)) == len(hashes)


def test_hash_is_the_sha256_of_the_text() -> None:
    import hashlib

    case = load_log_case("pytest-assert")
    signature = compute_signature(select_case_jobs(case)[0], case_excerpts(case, COUNTER)[0])

    assert signature.hash == hashlib.sha256(signature.text.encode()).hexdigest()
    assert len(signature.hash) == 64
