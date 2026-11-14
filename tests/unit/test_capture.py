"""Lab capture (``python -m evals capture``, D-61) with a fake ``gh``: no network, no GitHub."""

import json
import re
import subprocess
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
    fetch_job_log,
    find_evidence,
    load_manifest,
    run_gh,
    trim_jobs_payload,
)
from evals.runner.cases import load_cases
from tests.logcases import LOG_CASES_DIR, load_log_case, select_case_jobs
from tests.redaction_corpus import planted_secrets

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
PLAIN_ERROR = "ModuleNotFoundError: No module named 'requests'"
COLOURED_ERROR = "\x1b[1;31mModuleNotFoundError\x1b[0m: No module named '\x1b[33mrequests\x1b[0m'"
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


ESCAPE_FLAG = "--allow-escape-sequences"
# What the stored log must not contain: C0 controls except TAB/LF/CR, DEL and the C1 range.
CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x80-\x9f]")
ESCAPES_REFUSED = (
    f"the response contains terminal escape sequences; pass {ESCAPE_FLAG} to output it anyway"
)


class FakeGh:
    """Answers the few ``gh`` calls capture makes, from the py-modnf log fixture.

    The log is a real-looking one: ANSI colours (even between the words of the error), an OSC
    hyperlink, stray control characters, a progress line and a secret split by a colour code.
    Like the real ``gh api``, it refuses to output that log unless it gets the escape flag.
    """

    def __init__(self, runs: list[dict[str, object]] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.runs = runs or []
        self.secret = planted_secrets()[0]
        fixture = LOG_CASES_DIR / "py-modnf"
        self.jobs = json.loads((fixture / "jobs.json").read_text("utf-8"))
        self.jobs["jobs"][0]["runner_name"] = "GitHub Actions 1000000123"
        self.jobs["jobs"][0]["html_url"] = "https://github.com/acme/x/actions/runs/1/job/2"
        text = (fixture / "logs" / f"{JOB_ID}.txt").read_bytes().decode("utf-8")
        assert PLAIN_ERROR in text
        text = text.replace(PLAIN_ERROR, COLOURED_ERROR)  # same line number, colours in between
        token = self.secret.secret
        extra = [
            self.secret.line,
            f"fetching with {token[:10]}\x1b[0m{token[10:]} now",  # a colour code inside a secret
            "\x1b]8;;https://example.com\x1b\\a link\x1b]8;;\x1b\\",
            "bell\x07 backspace\x08 nul\x00 del\x7f c1\x9b end",
            "10%\r55%\r100%",  # a progress line: the bare CRs must survive
        ]
        self.log = (
            text + "".join(f"2026-11-13T10:00:13.0000000Z {line}\n" for line in extra)
        ).encode()

    def __call__(self, args: Sequence[str]) -> bytes:
        self.calls.append(list(args))
        if args[:2] == ["run", "list"]:
            return json.dumps(self.runs).encode()
        endpoint = args[-1]
        if endpoint.endswith("/logs"):
            if ESCAPE_FLAG not in args:
                raise CaptureError(f"gh api failed: {ESCAPES_REFUSED}")
            return self.log
        if "/jobs?" in endpoint:
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
    assert gh.calls[1] == ["api", ESCAPE_FLAG, f"repos/{REPO}/actions/jobs/{JOB_ID}/logs"]
    # The eval runner can load the captured case.
    [loaded] = load_cases(tmp_path, ["lab"])
    assert loaded.id == "lab-py-modnf-01"
    assert loaded.meta.job_id == JOB_ID


def test_the_log_is_read_with_the_escape_flag_and_the_jobs_json_without_it(
    tmp_path: Path,
) -> None:
    gh = FakeGh()

    capture(REPO, 555, "py-modnf", manifest=MANIFEST, out_dir=tmp_path, gh=gh)

    jobs_call, log_call = gh.calls
    assert ESCAPE_FLAG not in jobs_call  # JSON escapes control characters itself
    assert log_call[:2] == ["api", ESCAPE_FLAG]


def test_without_the_flag_gh_refuses_the_log_and_the_error_says_so(tmp_path: Path) -> None:
    gh = FakeGh()

    with pytest.raises(CaptureError, match="terminal escape sequences"):
        gh(["api", f"repos/{REPO}/actions/jobs/{JOB_ID}/logs"])  # what capture used to send


def test_the_stored_log_has_no_terminal_escapes_but_keeps_its_structure(tmp_path: Path) -> None:
    gh = FakeGh()

    captured = capture(REPO, 555, "py-modnf", manifest=MANIFEST, out_dir=tmp_path, gh=gh)

    log = (captured.path / "log.txt").read_bytes().decode("utf-8")
    assert CONTROLS.search(log) is None
    assert "\x1b" not in log
    assert "a link" in log
    assert "https://example.com" not in log  # the OSC target goes with the sequence
    assert "bell backspace nul del c1 end" in log.replace("\x00", "")  # text around controls
    assert "10%\r55%\r100%" in log  # progress lines keep their CRs: the excerpt needs them
    assert log.count("\n") == gh.log.decode("utf-8").count("\n")  # line numbers do not move
    # The colours sat between the words of the error; the evidence regex still finds the line.
    assert PLAIN_ERROR in log
    assert captured.evidence_lines == [89]
    assert "never matches" in captured.missing_evidence


def test_a_secret_split_by_a_colour_code_is_still_redacted(tmp_path: Path) -> None:
    gh = FakeGh()
    token = gh.secret.secret

    captured = capture(REPO, 555, "py-modnf", manifest=MANIFEST, out_dir=tmp_path, gh=gh)

    log = (captured.path / "log.txt").read_bytes().decode("utf-8")
    assert token[:10] not in log
    assert token[10:] not in log
    assert log.count("[REDACTED:github_token]") == 2  # the planted line and the split one
    assert captured.redactions >= 2


def test_fetch_job_log_runs_gh_with_the_flag_through_a_pipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        commands.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout=b"\x1b[31mred\x1b[0m text\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    text = fetch_job_log(run_gh, REPO, 77)

    assert text == "red text\n"
    [(command, kwargs)] = commands
    assert command == ["gh", "api", ESCAPE_FLAG, f"repos/{REPO}/actions/jobs/77/logs"]
    assert kwargs["capture_output"] is True  # stdout goes to a pipe, never to a terminal


def test_run_gh_reports_the_last_line_of_gh_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.CalledProcessError(1, command, stderr=f"{ESCAPES_REFUSED}\n".encode())

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(CaptureError, match="gh api failed: the response contains terminal"):
        run_gh(["api", "x"])


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


def test_capture_all_explains_runs_that_ended_cancelled(tmp_path: Path) -> None:
    # A job that hits its timeout-minutes ends cancelled, as the real lab showed.
    runs = [{"databaseId": 21, "attempt": 1, "conclusion": "cancelled"}]

    captured, notes = capture_all(REPO, manifest=MANIFEST, out_dir=tmp_path, gh=FakeGh(runs))

    assert captured == []
    [note] = notes
    assert note.startswith("py-modnf: 0 of 2 captures; its 1 run(s) ended cancelled")
    assert "will not help" in note
    assert "SPEC §14" in note


def test_a_cancelled_run_next_to_a_captured_one_keeps_the_plain_note(tmp_path: Path) -> None:
    runs = [
        {"databaseId": 21, "attempt": 1, "conclusion": "cancelled"},
        {"databaseId": 22, "attempt": 1, "conclusion": "failure"},
    ]

    _, notes = capture_all(REPO, manifest=MANIFEST, out_dir=tmp_path, gh=FakeGh(runs))

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
