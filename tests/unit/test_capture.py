"""Lab capture (``python -m evals capture``, D-61) with a fake ``gh``: no network, no GitHub."""

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from evals.runner import cli
from evals.runner.capture import (
    CaptureError,
    LabCase,
    LabManifest,
    capture,
    capture_all,
    captured_runs,
    find_evidence,
    load_manifest,
    trim_jobs_payload,
)
from evals.runner.cases import load_cases
from tests.logcases import LOG_CASES_DIR, load_log_case, select_case_jobs
from tests.redaction_corpus import planted_secrets

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
JOB_ID = 71000000002
REPO = "acme/cicd-doctor-lab"
MANIFEST = LabManifest(
    cases=[
        LabCase(
            id="py-modnf",
            workflow="py-modnf.yml",
            job="build",
            category="dependency",  # type: ignore[arg-type]
            cause="requests is not declared",
            fix="declare requests",
            evidence=["ModuleNotFoundError: No module named 'requests'", "never matches"],
            tags=["python"],
            captures=2,
        )
    ]
)


class FakeGh:
    """Answers the few ``gh`` calls capture makes, from the py-modnf log fixture."""

    def __init__(self, runs: list[dict[str, object]] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.runs = runs or []
        self.secret = planted_secrets()[0]
        fixture = LOG_CASES_DIR / "py-modnf"
        self.jobs = json.loads((fixture / "jobs.json").read_text("utf-8"))
        self.jobs["jobs"][0]["runner_name"] = "GitHub Actions 1000000123"
        self.jobs["jobs"][0]["html_url"] = "https://github.com/acme/x/actions/runs/1/job/2"
        log = (fixture / "logs" / f"{JOB_ID}.txt").read_bytes()
        planted = f"2026-11-13T10:00:13.0000000Z {self.secret.line}\n".encode()
        self.log = log + planted

    def __call__(self, args: Sequence[str]) -> bytes:
        self.calls.append(list(args))
        if args[:2] == ["run", "list"]:
            return json.dumps(self.runs).encode()
        if args[1].endswith("/logs"):
            return self.log
        if "/jobs?" in args[1]:
            return json.dumps(self.jobs).encode()
        raise AssertionError(f"unexpected gh call {args}")


def test_capture_writes_a_redacted_labelled_case(tmp_path: Path) -> None:
    gh = FakeGh()
    out_dir = tmp_path / "lab"

    captured = capture(
        REPO, 555, "py-modnf", manifest=MANIFEST, out_dir=out_dir, gh=gh, now=lambda: NOW
    )

    assert captured.path == out_dir / "lab-py-modnf-01"
    assert captured.job_id == JOB_ID
    assert captured.redactions >= 1
    assert captured.missing_evidence == ["never matches"]
    log = (captured.path / "log.txt").read_text("utf-8")
    assert gh.secret.secret not in log
    assert f"[REDACTED:{gh.secret.kind}]" in log
    case = yaml.safe_load((captured.path / "case.yaml").read_text("utf-8"))
    assert case["expected"]["evidence_lines"] == captured.evidence_lines == [89]
    assert case["run_url"] == f"https://github.com/{REPO}/actions/runs/555/attempts/1"
    assert case["lab_case"] == "py-modnf"
    assert case["captured_at"] == "2026-11-13T12:00:00+00:00"
    jobs = json.loads((captured.path / "jobs.json").read_text("utf-8"))
    assert "runner_name" not in jobs["jobs"][0]
    assert "html_url" not in jobs["jobs"][0]
    assert gh.calls[0] == ["api", f"repos/{REPO}/actions/runs/555/attempts/1/jobs?per_page=100"]
    assert gh.calls[1] == ["api", f"repos/{REPO}/actions/jobs/{JOB_ID}/logs"]
    # The eval runner can load the captured case.
    [loaded] = load_cases(tmp_path, ["lab"])
    assert loaded.id == "lab-py-modnf-01"
    assert loaded.meta.job_id == JOB_ID


def test_a_second_capture_gets_the_next_number(tmp_path: Path) -> None:
    capture(REPO, 1, "py-modnf", manifest=MANIFEST, out_dir=tmp_path, gh=FakeGh())
    second = capture(REPO, 2, "py-modnf", manifest=MANIFEST, out_dir=tmp_path, gh=FakeGh())

    assert second.path.name == "lab-py-modnf-02"


def test_unknown_case_job_and_runs_without_failures_are_errors(tmp_path: Path) -> None:
    with pytest.raises(CaptureError, match="not in the lab manifest"):
        capture(REPO, 1, "nope", manifest=MANIFEST, out_dir=tmp_path, gh=FakeGh())
    other = MANIFEST.model_copy(
        update={"cases": [MANIFEST.cases[0].model_copy(update={"job": "lint"})]}
    )
    with pytest.raises(CaptureError, match="no failed job named 'lint'"):
        capture(REPO, 1, "py-modnf", manifest=other, out_dir=tmp_path, gh=FakeGh())
    green = FakeGh()
    green.jobs["jobs"][0]["conclusion"] = "success"
    with pytest.raises(CaptureError, match="no failed or timed-out job"):
        capture(REPO, 1, "py-modnf", manifest=MANIFEST, out_dir=tmp_path, gh=green)


def test_capture_all_takes_failed_runs_until_each_case_has_enough(tmp_path: Path) -> None:
    runs = [
        {"databaseId": 10, "attempt": 1, "conclusion": "success"},
        {"databaseId": 11, "attempt": 1, "conclusion": "failure"},
        {"databaseId": 12, "attempt": 2, "conclusion": "timed_out"},
        {"databaseId": 13, "attempt": 1, "conclusion": "failure"},
    ]

    first, notes = capture_all(REPO, manifest=MANIFEST, out_dir=tmp_path, gh=FakeGh(runs))
    again, notes_again = capture_all(REPO, manifest=MANIFEST, out_dir=tmp_path, gh=FakeGh(runs))

    assert [c.path.name for c in first] == ["lab-py-modnf-01", "lab-py-modnf-02"]
    assert notes == []
    assert again == []  # both captures exist already
    assert notes_again == []
    assert sorted(captured_runs(tmp_path)["py-modnf"]) == [
        f"https://github.com/{REPO}/actions/runs/11/attempts/1",
        f"https://github.com/{REPO}/actions/runs/12/attempts/2",
    ]


def test_capture_all_notes_cases_that_lack_failed_runs(tmp_path: Path) -> None:
    runs = [{"databaseId": 11, "attempt": 1, "conclusion": "failure"}]

    captured, notes = capture_all(REPO, manifest=MANIFEST, out_dir=tmp_path, gh=FakeGh(runs))

    assert len(captured) == 1
    assert notes == ["py-modnf: 1 of 2 captures; run py-modnf.yml again"]


def test_find_evidence_looks_only_at_the_failed_step() -> None:
    case = load_log_case("pytest-assert")
    [job] = select_case_jobs(case)
    text = case.raw_log(job.job.job_id).text

    lines, missing = find_evidence(text, job, ["Current runner version", "assert 3 == 4"])

    assert missing == ["Current runner version"]  # set-up line, outside the failed step
    assert lines == [109]  # "E       assert 3 == 4"


def test_trim_jobs_payload_keeps_only_pipeline_fields() -> None:
    payload = {
        "jobs": [
            {
                "id": 1,
                "name": "run",
                "conclusion": "failure",
                "runner_name": "x",
                "steps": [{"name": "s", "number": 1, "conclusion": "failure", "secret": "y"}],
            }
        ]
    }

    assert trim_jobs_payload(payload) == {
        "total_count": 1,
        "jobs": [
            {
                "id": 1,
                "name": "run",
                "conclusion": "failure",
                "steps": [{"name": "s", "conclusion": "failure", "number": 1}],
            }
        ],
    }


def test_cli_capture_needs_a_run_and_one_case(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["capture", "--repo", REPO]) == 1
    assert "use --all, or --run-id with exactly one --case" in capsys.readouterr().err


def test_the_committed_manifest_loads() -> None:
    manifest = load_manifest()

    assert len(manifest.cases) >= 20
