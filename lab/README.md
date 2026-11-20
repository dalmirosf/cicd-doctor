# cicd-doctor-lab

Source of the **cicd-doctor-lab** repository: GitHub Actions workflows that **fail on purpose**,
each with a known cause. CICD Doctor's evals use their logs as labelled cases (SPEC §11.1 of
the main repository; decision D-61).

Every workflow:

- runs only when started by hand (`workflow_dispatch`), so publishing the repository runs nothing.
  The exceptions are `pr-py-modnf.yml` (the P1c live verification), `pr-flaky.yml` (the P2 one) and
  `pr-suggest.yml` (the P3 one, only for pull requests that touch `python/suggest/`), all below: they
  run on `pull_request`, so they also run nothing until somebody opens a pull request;
- has `permissions: contents: read` and a `timeout-minutes`;
- needs no secret, cloud account or paid service. The secret in `secret-misspelled.yml` is
  misspelled on purpose and must **not** be created.

`cases.yaml` lists each case: its workflow, the diagnosis it should get (category, cause, fix)
and regexes that mark its evidence lines.

| Category | Cases |
|---|---|
| broken_test | `py-assert`, `py-ruff` (lint gate), `py-mypy` (type gate) |
| dependency | `py-modnf` (also `pr-py-modnf`, see below), `py-resolve`, `pip-no-dist`, `docker-base`, `tf-init` |
| network | `dns`, `conn-refused` |
| config_or_secrets | `missing-input`, `secret-misspelled`, `docker-copy`, `tf-validate`, `aws-no-credentials` |
| timeout | `job-timeout` (ends `cancelled`; captured with the annotations of its job, D-73), `step-timeout` |
| infra_or_deploy | `oom-137`, `disk-full` |
| flaky | `flaky-random`, `flaky-timing` (each fails about half of the time) |
| unknown | `opaque-exit`, `obscure-tool` |

`pr-py-modnf.yml` is the same failure as `py-modnf` started by a pull request. It has no entry in
`cases.yaml` (it is not an eval case: the evals capture `py-modnf`) and exists only for the live
verification of P1c.

`pr-flaky.yml` **simulates** a flaky failure: it fails in attempt 1 and passes from attempt 2 on
(`if [ "$GITHUB_RUN_ATTEMPT" = 1 ]; then exit 1; fi`), so "Re-run failed jobs" turns a failure into
a success without a code change. It is deterministic on purpose, not a real race (the real ones are
`flaky-random` and `flaky-timing`). It has no entry in `cases.yaml` either and exists only for the
live verification of P2. Both `pr-*` workflows run on **every** pull request of the lab, so a pull
request has two checks and two comments of CICD Doctor, one per workflow.

`pr-suggest.yml` (P3) runs ruff on `python/suggest/legacy.py` (`value == None`, E711, line 8) and only
when a pull request touches `python/suggest/`. A pull request that edits that line gets a failure
whose line is inside its diff (a suggested change can land); one that edits only `notes.py` gets the
same failure outside its diff. `config/p3-*.yml` are the repository configurations of the P3 live
verification (copied by hand to `.github/cicd-doctor.yml`). Neither has an entry in `cases.yaml`.

A real `AccessDenied` from AWS would need an AWS account and an IAM role, so it is left out;
`aws-no-credentials` covers the AWS credential rules without one.

## Publish

The lab repository is published **by hand** from this folder; nothing in the main repository
pushes to it. Publish again whenever a file here changes (the `pr-py-modnf.yml`, `pr-flaky.yml` and `pr-suggest.yml` pull request workflows included).

From the root of the `cicd-doctor` repository, in PowerShell, with the GitHub CLI logged in.
`git subtree split` turns the `lab/` folder into a branch whose root is the lab:

```powershell
gh repo create cicd-doctor-lab --private --description "Workflows that fail on purpose, for CICD Doctor evals"
git subtree split --prefix=lab -b lab-export
git push "https://github.com/<owner>/cicd-doctor-lab.git" lab-export:main
git branch -D lab-export
```

A public repository gets free Actions minutes; a private one uses about 25 short jobs of the
account's minutes per full round.

## Run

```powershell
$repo = "<owner>/cicd-doctor-lab"
Get-ChildItem lab\.github\workflows\*.yml | Where-Object { $_.Name -notlike "pr-*" } | ForEach-Object { gh workflow run $_.Name -R $repo }
```

Then, once those runs have finished:

- run `py-assert`, `py-modnf` and `dns` a second time: those cases are captured twice, to
  check that two runs of one failure get the same signature (`captures: 2` in `cases.yaml`);
- run `flaky-random` and `flaky-timing` again until each has failed at least once.

```powershell
gh workflow run py-modnf.yml -R $repo
gh run list -R $repo --limit 30     # check the results
```

## Capture

```powershell
uv run python -m evals capture --repo $repo --all
```

For each case it takes the latest failed runs it has not captured yet, until the case has its
`captures`. Then it writes `evals/cases/lab/lab-<case>-NN/`:

- `log.txt`: the failed job's log, with terminal colours and other control characters stripped, then redacted (line numbers match GitHub's);
- `jobs.json`: the job and step metadata, trimmed;
- `annotations.json` (only when a job ended `cancelled`): the redacted annotation messages of the cancelled jobs, by job id. A `cancelled` job that says `has exceeded the maximum execution time` counts as `timed_out` (D-73), which is how `job-timeout` is captured; a manual cancellation is skipped;
- `case.yaml`: the labels from `cases.yaml`, plus the evidence line numbers found by its regexes.

It reads through your own `gh` login (the log is requested with `--allow-escape-sequences`, because GitHub's logs carry ANSI colours) and never prints log content. It warns when an evidence
regex matches no line. Read the warnings, then open the matching `log.txt` before committing,
because the regex may need fixing. One run can also be captured on its own with
`--run-id <id> --case <case>`.

Then run the rule baseline on the cases:

```powershell
uv run python -m evals run --variant rules
```

## Live verification (P1c)

This is the runbook that checks CICD Doctor against the real GitHub with the development app
(SPEC §13, P1c). **You** run the *Action* blocks (they write to GitHub and change `.env`); the
*Verification* blocks only read, so anybody can run them, and the session that reads the results
uses `gh` with `GET` calls only. Everything is written for **Windows PowerShell 5.1**: no `&&`, no
double quotes inside `--jq`, dates compared as `[datetimeoffset]` (5.1 leaves ISO dates as text),
and no two variables whose names differ only in case (PowerShell is case-insensitive: `$run` and
`$RUN` are one variable).

What GitHub does, as measured by hand (SPEC §13, D-70, D-76):

- A completed check run is **not** reopened: a `PATCH` to `in_progress` answers 200 and the check
  stays `completed` / `neutral`, while `name` and `output` are applied. So on a re-run the same
  check run keeps its id and its status, and only its text changes.
- The check run name is shown as literal plain text, and `neutral` checks count as "passed".

### Before you start

Real mistakes of the first live run (2026-11-16). Go through them once:

1. **Check the prompt before any `git` command.** The lab clone (`$env:TEMP\lab-p1c`) is not the
   project (`C:\dev\cicd-doctor`). A branch pushed from the project folder goes to the project
   repository, and it happened. Read the path in the prompt, or run `git remote get-url origin`,
   before every `git switch`, `git commit` and `git push`.
2. **Stop old `cicd-doctor` processes first** (`Get-Process | Where-Object { $_.Path -like '*cicd-doctor*' }`
   or look at the terminals). An old worker with placeholder handlers takes the job and marks it
   done without a diagnosis. The symptom that gave it away was error 32 (file in use) on `uv sync`.
3. **Update before you migrate:** `git switch main`, `git pull`, `uv sync`, and only then
   `uv run alembic upgrade head`. From an old branch alembic sees the old migrations and reports
   nothing to do.
4. **`cmd /c "git show ... > file"` creates an empty file even when git fails.** Check the size of
   the destination (`(Get-Item file).Length`) before you use it.
5. **PowerShell variable names ignore case:** `$run` and `$RUN` are the same variable.

### 0. Variables and start-up (once per PowerShell session)

Replace `OWNER`, `SLUG` and `ORG`. `SLUG` is the app's slug (`GET /app`); `ORG` is the
organization that will hold the fork of box 3.

```powershell
$LAB = "OWNER/cicd-doctor-lab"
$ME  = "OWNER/cicd-doctor"
$BOT = "SLUG[bot]"
$ORG = "ORG"
```

Database and schema:

```powershell
docker compose up -d db
uv run alembic upgrade head
```

Each of these in its own terminal:

```powershell
uv run cicd-doctor api
```

```powershell
uv run cicd-doctor worker
```

```powershell
npx smee-client --url https://smee.io/CHANNEL --target http://127.0.0.1:8000/webhooks/github
```

Check (it must say `ready`):

```powershell
Invoke-RestMethod http://127.0.0.1:8000/ready
```

A clone of the lab to work in (once):

```powershell
gh repo clone $LAB "$env:TEMP\lab-p1c"
```

Precondition: `.github/workflows/pr-py-modnf.yml` is on `main` of the lab repository. Publish the
`lab/` folder first (see *Publish*): it is copied there by hand.

### 1. Box 1: a lab pull request gets a `neutral` check in 90 s or less and one sticky comment

Action:

```powershell
Set-Location "$env:TEMP\lab-p1c"
git switch main
git pull
git switch -c p1c/box1
Add-Content -Path p1c-scratch.txt -Value "box 1" -Encoding ascii
git add p1c-scratch.txt
git commit -m "p1c box 1"
git push -u origin p1c/box1
gh pr create --repo $LAB --base main --head p1c/box1 --title "P1c box 1" --body "Live check of P1c"
```

Keep the references (if `$RUN` is empty, wait 10 s and repeat that line):

```powershell
$PR  = gh pr view p1c/box1 --repo $LAB --json number --jq .number
$SHA = git rev-parse HEAD
$RUN = gh run list --repo $LAB --branch p1c/box1 --workflow pr-py-modnf.yml --limit 1 --json databaseId --jq '.[0].databaseId'
gh run watch $RUN --repo $LAB
```

Verification:

```powershell
$checks = gh api "repos/$LAB/commits/$SHA/check-runs?per_page=100" | ConvertFrom-Json
$mine = @($checks.check_runs | Where-Object { $_.name -like 'CICD Doctor*' })
$mine | Select-Object id, name, status, conclusion, started_at, completed_at
$runInfo = gh run view $RUN --repo $LAB --json updatedAt | ConvertFrom-Json
([datetimeoffset]$mine[0].completed_at - [datetimeoffset]$runInfo.updatedAt).TotalSeconds
$comments = gh api "repos/$LAB/issues/$PR/comments?per_page=100" | ConvertFrom-Json
@($comments | Where-Object { $_.user.login -eq $BOT }).Count
```

Criterion: one check run named `CICD Doctor: pr-py-modnf`, `status = completed`,
`conclusion = neutral`; the seconds between the end of the run and the check's `completed_at` are
**90 or less**; exactly **1** comment of the bot. Keep the ids and the text for box 2:

```powershell
$id1  = $mine[0].id
$cid1 = ($comments | Where-Object { $_.user.login -eq $BOT }).id
$sum1 = $mine[0].output.summary
```

### 2. Box 2: "Re-run failed jobs" edits the same check and the same comment

Action:

```powershell
gh run rerun $RUN --repo $LAB --failed
gh run watch $RUN --repo $LAB
```

Verification:

```powershell
$checks2 = gh api "repos/$LAB/commits/$SHA/check-runs?per_page=100" | ConvertFrom-Json
$mine2 = @($checks2.check_runs | Where-Object { $_.name -like 'CICD Doctor*' })
$mine2 | Select-Object id, name, status, conclusion, completed_at
$mine2[0].output.summary -ne $sum1
$comments2 = gh api "repos/$LAB/issues/$PR/comments?per_page=100" | ConvertFrom-Json
$botComments = @($comments2 | Where-Object { $_.user.login -eq $BOT })
$botComments.Count
$botComments | Select-Object id, updated_at
$botComments[0].body.Split("`n") | Select-Object -First 4
```

Criterion: `$mine2[0].id` equals `$id1` (one check, not two); the check is still `completed` /
`neutral` (GitHub does not reopen it); its output changed (`-ne $sum1` prints `True`: the failed job
of attempt 2 has another id, so its link differs); `$botComments.Count` is **1** with the id
`$cid1` (the same comment, edited: its `updated_at` is later); and the fourth line of the comment
says `Attempt 2`.

### 3. Box 3: a pull request from a fork gets rules only, "LLM not used for fork PRs"

**This box is open in P1c:** an organization for the fork is missing; it is verified at the end of
P2 or P3. Meanwhile the offline tests of the use case and the worker cover it (a fork run looks up
its pull request by head, only the rules run, `fork_policy` is the reason, and the check is
published all the same). The commands are written for when there is an organization. The fork has
to live in an account or organization other than the lab's owner.

Action:

```powershell
gh repo fork $LAB --org $ORG --clone=false
gh repo clone "$ORG/cicd-doctor-lab" "$env:TEMP\lab-p1c-fork"
Set-Location "$env:TEMP\lab-p1c-fork"
git switch -c p1c/box3
Add-Content -Path p1c-scratch.txt -Value "box 3" -Encoding ascii
git add p1c-scratch.txt
git commit -m "p1c box 3"
git push -u origin p1c/box3
$URL3 = gh pr create --repo $LAB --base main --head "${ORG}:p1c/box3" --title "P1c box 3 (fork)" --body "Fork PR live check of P1c"
$PR3  = ([string]($URL3 | Select-Object -Last 1)).Split('/')[-1]
$SHA3 = git rev-parse HEAD
```

If GitHub asks for approval (Actions tab of the lab, "Approve and run"), approve the run. Then:

```powershell
$RUN3 = gh run list --repo $LAB --branch p1c/box3 --workflow pr-py-modnf.yml --limit 1 --json databaseId --jq '.[0].databaseId'
gh run watch $RUN3 --repo $LAB
```

Verification:

```powershell
$checks3 = gh api "repos/$LAB/commits/$SHA3/check-runs?per_page=100" | ConvertFrom-Json
$mine3 = @($checks3.check_runs | Where-Object { $_.name -like 'CICD Doctor*' })
$mine3 | Select-Object id, name, status, conclusion
$mine3[0].output.summary -match 'LLM not used for fork PRs'
$comments3 = gh api "repos/$LAB/issues/$PR3/comments?per_page=100" | ConvertFrom-Json
@($comments3 | Where-Object { $_.user.login -eq $BOT }).Count
```

Criterion: a `neutral` check on the fork's commit (this answers U11), its summary contains
`LLM not used for fork PRs` (`True`), and one comment of the bot on the pull request.

### 4. Box 4: an invalid OpenAI key gives the rules with `llm_error`

Notice: with an invalid key the redacted excerpt of the lab goes to `api.openai.com` and comes back
as a 401. It is our own content and costs nothing.

Action:

1. Open `.env` **by hand in the editor** (do not rewrite it with PowerShell) and set
   `OPENAI_API_KEY=invalid-key-for-p1c-box4` and `OPENAI_MODEL=model-for-p1c-box4`.
2. In the worker's terminal: Ctrl+C, then `uv run cicd-doctor worker`.
3. Trigger a new analysis (a new push to the pull request of box 1):

```powershell
Set-Location "$env:TEMP\lab-p1c"
git switch p1c/box1
Add-Content -Path p1c-scratch.txt -Value "box 4" -Encoding ascii
git commit -am "p1c box 4"
git push
$SHA4 = git rev-parse HEAD
Start-Sleep -Seconds 10
$RUN4 = gh run list --repo $LAB --branch p1c/box1 --workflow pr-py-modnf.yml --limit 1 --json databaseId --jq '.[0].databaseId'
gh run watch $RUN4 --repo $LAB
```

Verification:

```powershell
$checks4 = gh api "repos/$LAB/commits/$SHA4/check-runs?per_page=100" | ConvertFrom-Json
$mine4 = @($checks4.check_runs | Where-Object { $_.name -like 'CICD Doctor*' })
$mine4 | Select-Object id, name, status, conclusion
$mine4[0].output.summary -match 'LLM unavailable'
docker compose exec -T db psql -U cicd -d cicd_doctor -c "SELECT source, fallback_reason, published, created_at FROM diagnoses WHERE published ORDER BY created_at DESC LIMIT 3;"
```

(Change `-U` and `-d` if you changed `POSTGRES_USER` or `POSTGRES_DB`.)

Criterion: a `neutral` check whose summary says `LLM unavailable`, and the latest published row has
`source = rules` and `fallback_reason = llm_error`.

### 5. Box 5: `LLM_DAILY_BUDGET_USD=0` gives `budget` and the limit text

Action:

1. In `.env`, by hand: `LLM_DAILY_BUDGET_USD=0` (keep the invalid key of box 4).
2. Restart the worker (Ctrl+C, then `uv run cicd-doctor worker`) and push another commit:

```powershell
Set-Location "$env:TEMP\lab-p1c"
Add-Content -Path p1c-scratch.txt -Value "box 5" -Encoding ascii
git commit -am "p1c box 5"
git push
$SHA5 = git rev-parse HEAD
Start-Sleep -Seconds 10
$RUN5 = gh run list --repo $LAB --branch p1c/box1 --workflow pr-py-modnf.yml --limit 1 --json databaseId --jq '.[0].databaseId'
gh run watch $RUN5 --repo $LAB
```

Verification:

```powershell
$checks5 = gh api "repos/$LAB/commits/$SHA5/check-runs?per_page=100" | ConvertFrom-Json
$mine5 = @($checks5.check_runs | Where-Object { $_.name -like 'CICD Doctor*' })
$mine5 | Select-Object id, name, status, conclusion
$mine5[0].output.summary -match 'LLM daily limit reached'
docker compose exec -T db psql -U cicd -d cicd_doctor -c "SELECT source, fallback_reason, published, created_at FROM diagnoses WHERE published ORDER BY created_at DESC LIMIT 3;"
docker compose exec -T db psql -U cicd -d cicd_doctor -c "SELECT * FROM llm_usage ORDER BY day DESC LIMIT 3;"
```

Criterion: the summary says `LLM daily limit reached`; the latest published row has
`fallback_reason = budget`; `calls` in `llm_usage` does not go up with this run.

After box 5 (by hand): in `.env` leave `OPENAI_API_KEY=`, `OPENAI_MODEL=` and
`LLM_DAILY_BUDGET_USD=2.00`, and restart the worker.

### 6. Box 6: a broken test on a branch of the main repository gets its own CI diagnosed

The test lives in `tests/integration/` so that the pre-commit gate (which runs only `unit` and
`contract`) does not see it, and it fails in CI, which runs everything.

Action:

```powershell
Set-Location C:\dev\cicd-doctor
git fetch origin
git switch -c p1c/box6 origin/main
@'
"""Deliberate failure for the P1c live check. Delete this file."""


def test_p1c_deliberate_failure() -> None:
    assert 1 == 2, "deliberate failure for the P1c live check"
'@ | Set-Content -Path tests/integration/test_p1c_deliberate_failure.py -Encoding ascii
git add tests/integration/test_p1c_deliberate_failure.py
git commit -m "Add a deliberately failing test for the P1c live check"
git push -u origin p1c/box6
gh pr create --repo $ME --base main --head p1c/box6 --title "P1c box 6 (do not merge)" --body "Deliberate CI failure to check that CICD Doctor diagnoses its own CI."
$PR6  = gh pr view p1c/box6 --repo $ME --json number --jq .number
$SHA6 = git rev-parse HEAD
Start-Sleep -Seconds 10
$RUN6 = gh run list --repo $ME --branch p1c/box6 --workflow ci.yml --limit 1 --json databaseId --jq '.[0].databaseId'
gh run watch $RUN6 --repo $ME
```

Verification:

```powershell
$checks6 = gh api "repos/$ME/commits/$SHA6/check-runs?per_page=100" | ConvertFrom-Json
$mine6 = @($checks6.check_runs | Where-Object { $_.name -like 'CICD Doctor*' })
$mine6 | Select-Object id, name, status, conclusion
$mine6[0].output.title
$comments6 = gh api "repos/$ME/issues/$PR6/comments?per_page=100" | ConvertFrom-Json
@($comments6 | Where-Object { $_.user.login -eq $BOT }).Count
```

Criterion: a check `CICD Doctor: CI`, `neutral`, with a title that starts `Likely cause: broken_test`,
and one comment of the bot on the pull request.

What the first live run showed (2026-11-16): the push and the `pull_request` runs of CI both fail, so
there are two runs of the same PR and workflow; they produce **one** comment, edited by the second.
The job `Types (mypy --strict)` fails too, because mypy rejects `assert 1 == 2` (`comparison-overlap`)
before the test even runs, so the check names two failed jobs (`PY-MYPY` and `PY-PYTEST-FAIL`) and
the title ends with `(+1 more)`.

### 7. Clean-up

```powershell
gh pr close $PR6 --repo $ME --delete-branch
gh pr close $PR --repo $LAB --delete-branch
gh pr close $PR3 --repo $LAB --delete-branch
```

The check runs that were written cannot be deleted; they hang from commits of throwaway branches.

## Live verification (P2)

This is the runbook that checks the memory of CICD Doctor (P2: similar failures, the cache, the
flaky and fix resolutions, the purge) against the real GitHub with the dev app and the lab (SPEC
§13, P2). The split is the one of P1c: **you** run the *Action* blocks (they write to GitHub and
change `.env`); the *Verification* blocks only read, and the session that reads the results uses
`gh` with `GET` calls only. The same rules of PowerShell 5.1 apply (no `&&`, no double quotes inside
`--jq`, no two variables whose names differ only in case).

The boxes, in the order of this runbook (the numbers are the ones of SPEC §13, P2):

| Box | What it checks | Needs the OpenAI key |
|---|---|---|
| 5 | Flaky: fail, then "Re-run failed jobs" passes → `resolved_flaky` and the flaky banner in the comment | no |
| 6 | Fix: a failing PR, a fixing commit, green → `resolved_by_commit` with the right sha and "Fixed by" | no |
| 3 | Similar but not identical: the report lists the earlier run with its score and its fix commit | no (rules only) |
| 1 | Embeddings are stored for real runs and cost milliseconds | no |
| 7 | The daily purge job runs (optional live check) | no |
| 2 | An identical failure within 7 days is served from the cache, with no new LLM call | **yes: pending** |

Box 8 of SPEC §13 (the eval variant `llm+hints+similar`) is not a live box: its real numbers also
wait for the key and the first `--live` run of the evals.

### Before you start

Everything of *Before you start* in *Live verification (P1c)* still holds (above all: check the
folder before every `git` command, and stop old `cicd-doctor` processes). What is new:

1. **Run from a folder that has the code of `main`** (the `p2-live` worktree has it:
   `C:\dev\cicd-doctor\.claude\worktrees\p2-live-57116f`, or `C:\dev\cicd-doctor` itself after
   `git pull`). The API, the worker and smee all run **from that folder**; copy the real `.env` into
   it by hand (`Copy-Item C:\dev\cicd-doctor\.env .env`) and never print it. See *Branches and
   worktrees* in `CLAUDE.md`.
2. **The database image changed.** `main` pins `pgvector/pgvector:0.8.7-pg17`: pull it and recreate
   the container (the data volume is kept), then update the extension, then migrate. Migrations
   `0004` to `0006` (the similarity index, the cache and the outcomes) have never run on your
   dev database.
3. **The model downloads once** (66 MB, into `.cache/fastembed` of the folder you run from) when the
   worker starts, in the background: wait for the line `embedding model loaded in …` in the worker
   terminal before the first push. Until then the failures are stored without an embedding (a
   warning says so).
4. **The lab needs `pr-flaky.yml` on its `main`:** publish `lab/` again (see *Publish*).
5. **With no OpenAI key** (`OPENAI_API_KEY=` and `OPENAI_MODEL=` empty in `.env`, the state of the
   end of P1c) boxes 5, 6, 3, 1 and 7 run on the rules only. That is on purpose: they check the
   memory and the resolutions, not the model.
6. **Two workflows, two comments:** every lab pull request runs `pr-py-modnf` and `pr-flaky`. The
   comment of a workflow is found by the number after `workflow=` in its first line, so the blocks
   below look for the comment of **one** workflow (by its id) and ignore the other.
7. **GitHub can take several minutes to start the run of a push** (seen on 2026-11-18: about 4
   minutes). In boxes 6 and 3 the run is found by the sha of the commit; if `$RUN_X1`, `$RUN_X2`,
   `$RUN_X3` or `$RUN_S` prints nothing, the run has not started: repeat the last two lines of that
   block until a number shows, then `gh run watch <that variable> --repo $LAB`. Long lines pasted
   into the terminal were cut once (`--commit` lost its argument), so those blocks keep every line short.

### 0. Variables and start-up (once per PowerShell session)

Replace `OWNER` and `SLUG` (the app's slug). The first block is typed in the folder you run from:

```powershell
$LAB = "OWNER/cicd-doctor-lab"
$BOT = "SLUG[bot]"
$APPDIR = (Get-Location).Path
$LABDIR = "$env:TEMP\lab-p2"
$APPDIR
git remote get-url origin
```

The last line must print the URL of the **cicd-doctor** repository (not the lab).

Database: new image, extension and schema:

```powershell
docker compose pull db
docker compose up -d db
docker compose exec -T db psql -U cicd -d cicd_doctor -c "SELECT extversion FROM pg_extension WHERE extname = 'vector';"
docker compose exec -T db psql -U cicd -d cicd_doctor -c "ALTER EXTENSION vector UPDATE;"
uv sync
uv run alembic upgrade head
uv run alembic current
```

(Change `-U` and `-d` if you changed `POSTGRES_USER` or `POSTGRES_DB`.) The last command must end
with `0006 (head)`. If `ALTER EXTENSION` says the extension is already at the newest version, that
is fine.

Each of these in its own terminal, **in the same folder** (`$APPDIR`):

```powershell
uv run cicd-doctor api
```

```powershell
uv run cicd-doctor worker
```

```powershell
npx smee-client --url https://smee.io/CHANNEL --target http://127.0.0.1:8000/webhooks/github
```

Check (it must say `ready`), and wait in the worker terminal for `embedding model loaded in`:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/ready
```

A clone of the lab to work in (once), and the ids of the two workflows:

```powershell
gh repo clone $LAB $LABDIR
$WF_FLAKY = gh api "repos/$LAB/actions/workflows/pr-flaky.yml" --jq .id
$WF_MODNF = gh api "repos/$LAB/actions/workflows/pr-py-modnf.yml" --jq .id
"$WF_FLAKY $WF_MODNF"
```

Both ids must print (if `pr-flaky.yml` answers 404, publish `lab/` first).

### 5. Box 5: a failure that passes on re-run is `resolved_flaky` and the comment says so

Action (a pull request: `pr-flaky` fails in attempt 1; then the failed jobs are re-run):

```powershell
Set-Location $LABDIR
git remote get-url origin
git switch main
git pull
git switch -c p2/flaky
Add-Content -Path p2-scratch.txt -Value "flaky" -Encoding ascii
git add p2-scratch.txt
git commit -m "p2 flaky"
git push -u origin p2/flaky
gh pr create --repo $LAB --base main --head p2/flaky --title "P2 flaky" --body "Live check of P2: flaky"
$PR_F  = gh pr view p2/flaky --repo $LAB --json number --jq .number
$SHA_F = git rev-parse HEAD
Start-Sleep -Seconds 10
$RUN_F = gh run list --repo $LAB --branch p2/flaky --workflow pr-flaky.yml --limit 1 --json databaseId --jq '.[0].databaseId'
gh run watch $RUN_F --repo $LAB
```

Wait for the check and the comment of the failure (about 10 s after the run ends), then keep the
ids of attempt 1:

```powershell
$checksF1 = gh api "repos/$LAB/commits/$SHA_F/check-runs?per_page=100" | ConvertFrom-Json
$mineF1 = @($checksF1.check_runs | Where-Object { $_.external_id -eq "cicd-doctor:run:$RUN_F" })
$commentsF1 = gh api "repos/$LAB/issues/$PR_F/comments?per_page=100" | ConvertFrom-Json
$botF1 = @($commentsF1 | Where-Object { $_.user.login -eq $BOT -and $_.body.Split("`n")[0] -like "*workflow=$WF_FLAKY -->*" })
$mineF1 | Select-Object id, name, status, conclusion
$botF1.Count
$idF  = $mineF1[0].id
$cidF = $botF1[0].id
$botF1[0].body.Split("`n") | Select-Object -First 4
```

It must show one check `CICD Doctor: pr-flaky`, `completed` / `neutral`, and **1** comment of the
bot for this workflow, with a failure diagnosis (not a banner). Now the re-run (attempt 2 passes):

```powershell
gh run rerun $RUN_F --repo $LAB --failed
gh run watch $RUN_F --repo $LAB
```

Verification (wait about 20 s after the run ends):

```powershell
$runF = gh run view $RUN_F --repo $LAB --json conclusion,attempt,updatedAt | ConvertFrom-Json
$runF.conclusion
$runF.attempt
$checksF2 = gh api "repos/$LAB/commits/$SHA_F/check-runs?per_page=100" | ConvertFrom-Json
$mineF2 = @($checksF2.check_runs | Where-Object { $_.external_id -eq "cicd-doctor:run:$RUN_F" })
$mineF2 | Select-Object id, name, status, conclusion
$mineF2[0].output.title
$commentsF2 = gh api "repos/$LAB/issues/$PR_F/comments?per_page=100" | ConvertFrom-Json
$botF2 = @($commentsF2 | Where-Object { $_.user.login -eq $BOT -and $_.body.Split("`n")[0] -like "*workflow=$WF_FLAKY -->*" })
$botF2.Count
$botF2 | Select-Object id, updated_at
$botF2[0].id -eq $cidF
$botF2[0].body -match 'Passed on re-run without code changes'
$botF2[0].body -match 'recorded as flaky'
([datetimeoffset]$botF2[0].updated_at - [datetimeoffset]$runF.updatedAt).TotalSeconds
docker compose exec -T db psql -U cicd -d cicd_doctor -c "SELECT id, run_id, run_attempt, status, resolved_by_run_id, resolved_by_attempt, fix_commit_sha FROM failures WHERE run_id = $RUN_F;"
```

Criterion: the run's `conclusion` is `success` and `attempt` is 2; **one** check run, with the id
`$idF`, still `completed` / `neutral` and with its failure title (the success never touches the
check, D-84); **1** comment of the bot for `pr-flaky`, with the id `$cidF` (the same comment,
edited), whose body has the flaky banner (`True`, `True`); the seconds from the end of the run to
the edit of the comment are small (the worker terminal shows `success recorded, 1 failure(s)
resolved by it` and `published comment … (resolved by it)`); and the row of the failure has
`status = resolved_flaky`, `resolved_by_run_id` equal to `$RUN_F`, `resolved_by_attempt = 2` and no
`fix_commit_sha`.

### 6. Box 6: a fixing commit turns the comment into "Fixed by" with the right sha

Action: a pull request whose first commit fails (`pr-py-modnf` imports `requests`, which
`python/pyproject.toml` does not declare), then a commit that declares it (green):

```powershell
Set-Location $LABDIR
git remote get-url origin
git switch main
git pull
git switch -c p2/fix
Add-Content -Path p2-scratch.txt -Value "fix" -Encoding ascii
git add p2-scratch.txt
git commit -m "p2 fix: failing"
git push -u origin p2/fix
gh pr create --repo $LAB --base main --head p2/fix --title "P2 fix" --body "Live check of P2: fix"
$PR_X   = gh pr view p2/fix --repo $LAB --json number --jq .number
$SHA_X1 = git rev-parse HEAD
Start-Sleep -Seconds 10
$runsX = gh run list --repo $LAB --branch p2/fix --workflow pr-py-modnf.yml --limit 5 --json databaseId,headSha | ConvertFrom-Json
$RUN_X1 = ($runsX | Where-Object { $_.headSha -eq $SHA_X1 }).databaseId
$RUN_X1
```

Wait for the diagnosis, then keep the first comment:

```powershell
$commentsX1 = gh api "repos/$LAB/issues/$PR_X/comments?per_page=100" | ConvertFrom-Json
$botX1 = @($commentsX1 | Where-Object { $_.user.login -eq $BOT -and $_.body.Split("`n")[0] -like "*workflow=$WF_MODNF -->*" })
$botX1.Count
$cidX = $botX1[0].id
$botX1[0].body.Split("`n") | Select-Object -First 4
```

Now the fix (the file is edited without PowerShell's text cmdlets, which rewrite encodings):

```powershell
$pyproject = (Resolve-Path "python\pyproject.toml").Path
$text = [IO.File]::ReadAllText($pyproject)
$fixed = $text.Replace("dependencies = []", 'dependencies = ["requests"]')
$fixed -ne $text
[IO.File]::WriteAllText($pyproject, $fixed, (New-Object System.Text.UTF8Encoding($false)))
git diff --stat
git add python/pyproject.toml
git commit -m "p2 fix: declare requests"
git push
$SHA_X2 = git rev-parse HEAD
Start-Sleep -Seconds 10
$runsX = gh run list --repo $LAB --branch p2/fix --workflow pr-py-modnf.yml --limit 5 --json databaseId,headSha | ConvertFrom-Json
$RUN_X2 = ($runsX | Where-Object { $_.headSha -eq $SHA_X2 }).databaseId
$RUN_X2
```

`$fixed -ne $text` must print `True` (the file changed) and the diff must show one line. The run
`$RUN_X2` must end green.

Verification (wait about 20 s after the run ends):

```powershell
gh run view $RUN_X2 --repo $LAB --json conclusion,headSha --jq .conclusion
$headX2 = gh run view $RUN_X2 --repo $LAB --json headSha --jq .headSha
$headX2 -eq $SHA_X2
$commentsX2 = gh api "repos/$LAB/issues/$PR_X/comments?per_page=100" | ConvertFrom-Json
$botX2 = @($commentsX2 | Where-Object { $_.user.login -eq $BOT -and $_.body.Split("`n")[0] -like "*workflow=$WF_MODNF -->*" })
$botX2.Count
$botX2[0].id -eq $cidX
$botX2[0].body.Split("`n") | Select-Object -First 4
$short = $SHA_X2.Substring(0, 7)
$botX2[0].body -match ('Fixed by `' + $short + '`')
$cmp = "https://github.com/$LAB/compare/${SHA_X1}...${SHA_X2}"
$botX2[0].body.Contains($cmp)
gh api "repos/$LAB/compare/${SHA_X1}...${SHA_X2}" --jq .status
docker compose exec -T db psql -U cicd -d cicd_doctor -c "SELECT id, run_id, status, fix_commit_sha, resolved_by_run_id, resolved_by_attempt FROM failures WHERE run_id = $RUN_X1;"
```

Criterion: the green run's `headSha` is `$SHA_X2`; **1** comment of the bot for `pr-py-modnf`, with
the id `$cidX` (the same comment); it contains `Fixed by` and the first 7 characters of `$SHA_X2`
(`True`), and the comparison link `compare/<failing sha>...<fixing sha>` (`True`; the last `gh`
call prints `ahead`, so the link opens); the failure of `$RUN_X1` has `status = resolved_by_commit`
and `fix_commit_sha` equal to the **full** `$SHA_X2` (compare it with the output above).

Then a third commit that fails again (it reverts the fix):

```powershell
git revert --no-edit HEAD
git push
$SHA_X3 = git rev-parse HEAD
Start-Sleep -Seconds 10
$runsX = gh run list --repo $LAB --branch p2/fix --workflow pr-py-modnf.yml --limit 5 --json databaseId,headSha | ConvertFrom-Json
$RUN_X3 = ($runsX | Where-Object { $_.headSha -eq $SHA_X3 }).databaseId
$RUN_X3
```

Verification (wait about 20 s):

```powershell
$commentsX3 = gh api "repos/$LAB/issues/$PR_X/comments?per_page=100" | ConvertFrom-Json
$botX3 = @($commentsX3 | Where-Object { $_.user.login -eq $BOT -and $_.body.Split("`n")[0] -like "*workflow=$WF_MODNF -->*" })
$botX3.Count
$botX3[0].id -eq $cidX
$botX3[0].body -match 'Fixed by'
$botX3[0].body.Split("`n") | Select-Object -First 6
```

Criterion: still **1** comment, with the id `$cidX`; it no longer shows the banner (`False`) and
shows the diagnosis of `$RUN_X3`.

### 3. Box 3: a similar but not identical failure lists the earlier run with its fix commit

Action (on the branch of box 6, another missing module: the signature changes, the embedding stays
close; in the spike the same change, `yaml` for `requests`, scored 0.905 against the 0.85 threshold):

```powershell
Set-Location $LABDIR
git remote get-url origin
git branch --show-current
$cli = (Resolve-Path "python\src\labpkg\cli.py").Path
$text = [IO.File]::ReadAllText($cli)
$other = $text.Replace("import requests", "import yaml").Replace("requests.__version__", "yaml.__version__")
$other -ne $text
[IO.File]::WriteAllText($cli, $other, (New-Object System.Text.UTF8Encoding($false)))
git diff --stat
git add python/src/labpkg/cli.py
git commit -m "p2 similar: another missing module"
git push
$SHA_S = git rev-parse HEAD
Start-Sleep -Seconds 10
$runsX = gh run list --repo $LAB --branch p2/fix --workflow pr-py-modnf.yml --limit 5 --json databaseId,headSha | ConvertFrom-Json
$RUN_S = ($runsX | Where-Object { $_.headSha -eq $SHA_S }).databaseId
$RUN_S
```

`git branch --show-current` must print `p2/fix`. Verification (wait about 20 s):

```powershell
$checksS = gh api "repos/$LAB/commits/$SHA_S/check-runs?per_page=100" | ConvertFrom-Json
$mineS = @($checksS.check_runs | Where-Object { $_.external_id -eq "cicd-doctor:run:$RUN_S" })
$mineS | Select-Object id, name, status, conclusion
$textS = "$($mineS[0].output.summary)`n$($mineS[0].output.text)"
$textS -match 'Similar past failure'
$textS -match "actions/runs/$RUN_X1"
$textS.Split("`n") | Where-Object { $_ -like '*score*' }
$commentsS = gh api "repos/$LAB/issues/$PR_X/comments?per_page=100" | ConvertFrom-Json
$botS = @($commentsS | Where-Object { $_.user.login -eq $BOT -and $_.body.Split("`n")[0] -like "*workflow=$WF_MODNF -->*" })
$botS.Count
$botS[0].id -eq $cidX
$botS[0].body.Split("`n") | Where-Object { $_ -like '*Similar past failure*' }
$botS[0].body.Split("`n") | Where-Object { $_ -like '*run #*' }
docker compose exec -T db psql -U cicd -d cicd_doctor -c "SELECT run_id, status, left(fix_commit_sha, 7) AS fix, left(signature_hash, 12) AS sig, embedding IS NOT NULL AS has_embedding FROM failures WHERE run_id IN ($RUN_X1, $RUN_X3, $RUN_S) ORDER BY run_id;"
```

Criterion: the check run of `$RUN_S` lists, under `Similar past failure`, a line for run
`#$RUN_X1` with its link, `(score 0.85` or more) and `fixed by` followed by the first 12 characters
of `$SHA_X2`; the comment is still the same one (`$cidX`) and shows **one** similar failure (the
heading `Similar past failure:` in the singular, one line, which may be the run `#$RUN_X3` instead
of `#$RUN_X1`: both are earlier, so look at the check run for the full list of up to three). The
`sig` of `$RUN_S` differs from the one of `$RUN_X1` (it is not an exact match) and all the rows
have an embedding. All of this is rules only: no key is needed.

### 1. Box 1: embeddings are stored for real runs and take milliseconds

The worker logs **no** line with the time of the embedding (the call is a few milliseconds inside
a job that takes seconds). So this box has two parts: the rows, and the latency measured on this
machine with the real model.

```powershell
docker compose exec -T db psql -U cicd -d cicd_doctor -c "SELECT count(*) AS failures, count(embedding) AS with_embedding, vector_dims(embedding) AS dims FROM failures GROUP BY vector_dims(embedding);"
Set-Location $APPDIR
uv run pytest tests/adapters/test_fastembed.py -s
```

Criterion: every failure of the runs of boxes 5, 6 and 3 has an embedding of **384** dimensions
(`with_embedding` equals `failures` for them: rows of earlier phases may have none, they were
stored before P2) and the worker terminal shows no `no embeddings for this run` warning; the test
prints the latency per batch (the SPEC bar is 200 ms per failure after the model is loaded; the
spike measured 5.8 ms at p50 for one signature). The `published … seconds_since_completed=…` lines
of the worker give the whole time of a diagnosis, for the record.

### 7. Box 7: the daily purge job runs (optional)

The offline suite already checks what the purge deletes (SPEC §13); this only shows that the job is
scheduled and runs in the real worker. A new dev database has nothing old to delete, so the log
says zeros.

```powershell
docker compose exec -T db psql -U cicd -d cicd_doctor -c "SELECT id, kind, status, dedupe_key, run_after, attempts FROM jobs WHERE kind = 'purge' ORDER BY id DESC LIMIT 3;"
```

If today's job (`purge:<today, UTC>`) is `done`, the worker already ran it at the configured hour:
that row is the evidence of the schedule, and to also see the counts in the log, delete today's job
(the dev database only) and let the scheduler create it again at the current UTC hour:

```powershell
$today = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd")
$hour  = (Get-Date).ToUniversalTime().Hour
docker compose exec -T db psql -U cicd -d cicd_doctor -c "DELETE FROM jobs WHERE dedupe_key = 'purge:$today';"
$hour
```

Then, **by hand in the editor**, set `PURGE_HOUR_UTC=<the hour printed above>` in `.env` (a number
from 0 to 23), restart the worker (Ctrl+C, `uv run cicd-doctor worker`) and wait at most 15 minutes
(one scheduler tick; the first tick runs at start-up). Verification:

```powershell
docker compose exec -T db psql -U cicd -d cicd_doctor -c "SELECT id, kind, status, dedupe_key, run_after, attempts, last_error FROM jobs WHERE kind = 'purge' ORDER BY id DESC LIMIT 3;"
```

Criterion: the worker terminal shows `scheduler: purge of <today> scheduled at …`, then
`job … (purge) started`, `purge done: 0 failures marked stale, 0 rows deleted (…), 0 repositories
gone` (the counts may be higher if your database has old rows) and `job … (purge) done`; the row
of `purge:<today>` is `done` with `attempts = 1` and no `last_error`. Afterwards put `PURGE_HOUR_UTC`
back to `3` (or remove the line) and restart the worker.

### Box 2 (the cache): verified on 2026-11-18 (casilla 2)

**This box was verified live** on 2026-11-18 with the real model (D-88); the evidence is in SPEC §13 P2. Everything that does not need the key (boxes 5,
6, 3, 1) can be checked before. The cache only serves a diagnosis that came from a real LLM call
(`source = llm`, verified evidence), so the first run needs the model. The offline part is covered
(`tests/unit/test_diagnose_cache.py`, `tests/e2e/test_cache_e2e.py` and
`tests/integration/test_diagnose_postgres.py`: one model call, one reservation, `source = cache`,
`cached_from` and "cached from run #N"). What is left is to see it on the real GitHub with the real
model. **Cost:** a few cents: one failed job, one call (two if the model needs one repair), the
second run makes none. The budget below caps it at USD 0.10.

**Settings (by hand in the editor, never paste or print the key):** in `.env` set

```
OPENAI_API_KEY=<your key>
OPENAI_MODEL=<model id>
LLM_PRICE_INPUT_PER_MTOK=<USD per million input tokens of that model>
LLM_PRICE_OUTPUT_PER_MTOK=<USD per million output tokens of that model>
LLM_DAILY_BUDGET_USD=0.10
LLM_DAILY_CALLS_PER_INSTALLATION=5
```

- **The model is `gpt-6-luna`** (D-88, chosen by the mini-eval of 2026-11-18 over `gpt-5.4-mini`), at
  USD 0.10 in and 0.50 out per million tokens (confirm them on the pricing page). The model that
  produced the evidence is part of it: write it down in the PR of this box.
- The prices come from the pricing page of the model you picked. Without them the cost is 0 and
  the budget cannot stop anything (the worker says `LLM_PRICE_* not set` at start-up); the call cap
  (`LLM_DAILY_CALLS_PER_INSTALLATION=5`) still does.
- Restart the worker. It must **not** print `LLM disabled: …`.

Action (the workflow runs on `main` without a pull request, so there is a check run and no
comment). First a real call (`source = llm`), and note the calls of the day:

```powershell
Set-Location $APPDIR
$callsSql = "SELECT COALESCE(sum(calls), 0) FROM llm_usage WHERE day = (now() AT TIME ZONE 'utc')::date;"
docker compose exec -T db psql -U cicd -d cicd_doctor -t -A -c $callsSql
gh workflow run py-modnf.yml --repo $LAB --ref main
Start-Sleep -Seconds 10
$RUN_A = gh run list --repo $LAB --workflow py-modnf.yml --limit 1 --json databaseId --jq '.[0].databaseId'
gh run watch $RUN_A --repo $LAB
Start-Sleep -Seconds 20
$CALLS_A = docker compose exec -T db psql -U cicd -d cicd_doctor -t -A -c $callsSql
"calls after run A: $CALLS_A"
docker compose exec -T db psql -U cicd -d cicd_doctor -c "SELECT d.id, d.source, d.published, d.fallback_reason, d.model, d.cost_usd, d.cached_from FROM diagnoses d JOIN failures f ON f.id = d.failure_id WHERE f.run_id = $RUN_A ORDER BY d.created_at;"
```

The published row of run A must have `source = llm` (and the calls must have gone up by 1 or 2). If
it says `rules`, read `fallback_reason` (`llm_error`, `invalid_output`, `budget`…): the cache has
nothing to serve, so fix that first; do not run the second step. Now the same workflow again,
without changing anything:

```powershell
gh workflow run py-modnf.yml --repo $LAB --ref main
Start-Sleep -Seconds 10
$RUN_B = gh run list --repo $LAB --workflow py-modnf.yml --limit 1 --json databaseId --jq '.[0].databaseId'
$RUN_B -ne $RUN_A
gh run watch $RUN_B --repo $LAB
Start-Sleep -Seconds 20
```

Verification (run it right after; `gh` only reads):

```powershell
$CALLS_B = docker compose exec -T db psql -U cicd -d cicd_doctor -t -A -c $callsSql
"calls after run A: $CALLS_A, after run B: $CALLS_B"
$CALLS_A -eq $CALLS_B
docker compose exec -T db psql -U cicd -d cicd_doctor -c "SELECT f.run_id, d.id, d.source, d.published, d.cached_from, d.cost_usd, d.created_at FROM diagnoses d JOIN failures f ON f.id = d.failure_id WHERE f.run_id IN ($RUN_A, $RUN_B) ORDER BY d.created_at;"
$shaB = gh run view $RUN_B --repo $LAB --json headSha --jq .headSha
$checksB = gh api "repos/$LAB/commits/$shaB/check-runs?per_page=100" | ConvertFrom-Json
$mineB = @($checksB.check_runs | Where-Object { $_.external_id -eq "cicd-doctor:run:$RUN_B" })
$mineB | Select-Object id, name, status, conclusion
$textB = "$($mineB[0].output.summary)`n$($mineB[0].output.text)"
$textB -match "cached from run #$RUN_A"
$textB -match "runs/$RUN_B"
```

Criterion: `llm_usage.calls` of the day is **the same number** after run B (`True`); the published
row of run B has `source = cache` and `cached_from` equal to the id of the published row of run A,
and no cost; the check run of run B says `cached from run #<RUN_A>` (`True`) and its evidence links
point at the log of run B, not of run A (`True`: the evidence was found again in the new log, or the
cache would have been a miss). In the worker terminal the job of run B shows no LLM call (the
worker has no line for a cache hit, so the evidence is the rows and the unchanged counter).

Clean-up of this box (by hand): in `.env` empty `OPENAI_API_KEY=` and `OPENAI_MODEL=`, remove or
empty the two `LLM_PRICE_*` lines and `LLM_DAILY_CALLS_PER_INSTALLATION`, put
`LLM_DAILY_BUDGET_USD=2.00`, and restart the worker.

### 8. Clean-up

```powershell
gh pr close $PR_F --repo $LAB --delete-branch
gh pr close $PR_X --repo $LAB --delete-branch
```

The check runs and comments that were written stay (they hang from throwaway branches). Then stop
the API, the worker and smee, and remove the worktree as *Branches and worktrees* in `CLAUDE.md`
says.

## Job timeouts (D-73)

A job that exceeds its `timeout-minutes` ends `cancelled`, like a manual cancellation. CICD Doctor
tells them apart by the job's annotations (`The job has exceeded the maximum execution time of …`):
a timeout gets a diagnosis (`timeout`), a manual cancellation or a `concurrency: cancel-in-progress`
gets nothing at all. This section checks both against the real GitHub (SPEC §13, the last P1c box).

Preconditions: go through *Before you start* above first (above all: the right folder, no old
`cicd-doctor` processes, `git pull` + `uv sync` + `uv run alembic upgrade head` on this code), and
run step 0 of *Live verification (P1c)* (the `$LAB` and `$BOT` variables, the database, the API,
the worker and smee). `job-timeout.yml` must be on `main` of the lab repository (see *Publish*).
The workflow runs on `main` without a pull request, so there is a check run and **no** comment.

### A. A job that exceeds its timeout is diagnosed as `timeout`

Action (the job sleeps 5 minutes with `timeout-minutes: 1`; the run ends in about 1 minute):

```powershell
gh workflow run job-timeout.yml --repo $LAB --ref main
Start-Sleep -Seconds 10
$toRun = gh run list --repo $LAB --workflow job-timeout.yml --limit 1 --json databaseId --jq '.[0].databaseId'
gh run watch $toRun --repo $LAB
```

Verification (wait about 20 s after the run ends, then):

```powershell
gh run view $toRun --repo $LAB --json conclusion --jq .conclusion
$toSha = gh run view $toRun --repo $LAB --json headSha --jq .headSha
$toJob = gh run view $toRun --repo $LAB --json jobs --jq '.jobs[0].databaseId'
gh api "repos/$LAB/check-runs/$toJob/annotations" --jq '.[].message'
$toChecks = gh api "repos/$LAB/commits/$toSha/check-runs?per_page=100" | ConvertFrom-Json
$toMine = @($toChecks.check_runs | Where-Object { $_.external_id -eq "cicd-doctor:run:$toRun" })
$toMine | Select-Object id, name, status, conclusion
$toMine[0].output.title
```

Criterion: the run's `conclusion` is `cancelled`; the annotations include
`The job has exceeded the maximum execution time of 1m0s`; exactly one check run of CICD Doctor
for this run, named `CICD Doctor: job-timeout`, `status = completed`, `conclusion = neutral`, and
a title that starts with `Likely cause: timeout`. There is no pull request, so no comment.

### B. A manual cancellation publishes nothing

Action (cancel it by hand before the minute is up):

```powershell
gh workflow run job-timeout.yml --repo $LAB --ref main
Start-Sleep -Seconds 12
$mcRun = gh run list --repo $LAB --workflow job-timeout.yml --limit 1 --json databaseId --jq '.[0].databaseId'
gh run cancel $mcRun --repo $LAB
gh run watch $mcRun --repo $LAB
```

Verification (wait about 30 s, so the webhook and the worker have time to act):

```powershell
Start-Sleep -Seconds 30
gh run view $mcRun --repo $LAB --json conclusion --jq .conclusion
$mcSha = gh run view $mcRun --repo $LAB --json headSha --jq .headSha
$mcJob = gh run view $mcRun --repo $LAB --json jobs --jq '.jobs[0].databaseId'
gh api "repos/$LAB/check-runs/$mcJob/annotations" --jq '.[].message'
$mcChecks = gh api "repos/$LAB/commits/$mcSha/check-runs?per_page=100" | ConvertFrom-Json
@($mcChecks.check_runs | Where-Object { $_.external_id -eq "cicd-doctor:run:$mcRun" }).Count
```

Criterion: the run's `conclusion` is `cancelled`; the annotations (if any) do **not** mention
`maximum execution time`; the last line prints **0** (run B is on the same commit as run A, so
count by `external_id`, not by name). In the worker terminal there is a line
`cancelled without a job timeout, nothing to publish` and the job finished (`done`), without
retries.

## Live verification (P3)

This is the runbook that checks the commands, the suggestions and the repository configuration of
CICD Doctor (P3: `/cicd-doctor explain`, suggested changes, `.github/cicd-doctor.yml`) against the
real GitHub with the dev app and the lab (SPEC §13, P3). The split is the one of P1c and P2: **you**
run the *Action* blocks (they write to GitHub, spend money or change settings); the *Verification*
blocks only read, and the session that reads the results uses `gh` with `GET` calls only. The same
rules of PowerShell 5.1 apply (no `&&`, no double quotes inside `--jq`, no two variables whose names
differ only in case, no `2>&1` on a native command).

Nothing of P3 is verified live yet. No box of SPEC §13 P3 is ticked until a row of the evidence
table (last section) has real ids, times and rows.

### What can hurt, by tag

Every step has one of these tags. Consent is asked **per group**, not for the whole runbook:

| Tag | What it does | Group |
|---|---|---|
| **[APP]** | Changes the settings of the dev GitHub App (permission, event) and the acceptance in the installations | G1 |
| **[ENV]** | Edits an `.env` (only the **copy** in the folder you run from, never the real one in `C:\dev\cicd-doctor`) | G2 |
| **[MONEY]** | Real OpenAI calls (the worker with the key; the optional `-m live` suite) | G2 (worker), G6 (suite) |
| **[WRITES LAB]** | Writes to the lab repository: a push, a pull request, a comment, a file on `main` | G3 |
| **[OTHER ACCOUNT]** | A comment from a second GitHub account | G4 |
| **[PROBE]** | Review comments of **your** account that measure what GitHub answers (box 3), and their deletion | G5 |
| **[LOCAL]** | Only your machine (processes, Docker, the database) | none |
| **[READ]** | Only reads (`gh` with `GET`, `SELECT`) | none |

The boxes, in the order of this runbook (the numbers are the ones of SPEC §13, P3):

| Box | What it checks | Needs |
|---|---|---|
| 5 | The first real `issue_comment` delivery, after the app change | G1, G3 |
| 1 | A collaborator's `/cicd-doctor explain` gets 👀 and an LLM diagnosis of the head runs in 90 s or less | G1, G2, G3 |
| 2 | A non-collaborator gets nothing (no reaction, no job); a second command within 5 minutes is ignored | G4 (the first half) |
| 3 | A suggestion inside the diff is a review comment with `suggestion`; outside it, text only; the exact 422 and the multi-line shape are **measured** | G3, G5 |
| 4 | `comment: false` on the default branch: no sticky, the check stays; the same file only in a PR branch has no effect; an invalid file turns the LLM off | G3 |
| fork | A command on a pull request from a fork | **stays open** (see the end) |

What the runbook does **not** do: it never starts anything for you, and every "restore" is by hand.

### The lab files of P3 (in `lab/`, see *Publish*)

- `.github/workflows/pr-suggest.yml` runs only when a pull request touches `python/suggest/`.
  It runs `ruff check --select E711 --output-format concise python/suggest/legacy.py` from the root
  of the repository, so the log names `python/suggest/legacy.py:8:12` exactly as the diff of the
  pull request does (the model is shown the diff of a file only when the log names it, D-97) and the
  failure is on `main` already (`return value == None`, line 8).
- `python/suggest/legacy.py` is that file; `python/suggest/notes.py` is clean.
  A pull request that **edits line 8** of `legacy.py` gets the failure with its line **inside** the
  diff. A pull request that edits only `notes.py` gets the same failure with `legacy.py` **outside**
  its diff.
- `config/p3-*.yml` are the four configuration files of box 4 and of the set-up (`p3-on-demand`,
  `p3-comment-off`, `p3-branch-only`, `p3-invalid`). The runbook copies them by hand to
  `.github/cicd-doctor.yml` of the lab; they are not published as that file by `lab/` itself.
- Every lab pull request also runs `pr-py-modnf` (fails always) and `pr-flaky` (fails in attempt 1).
  `p3-on-demand.yml` has `ignore_workflows: [pr-flaky]`, so `pr-flaky` produces nothing (D-91) and a
  command explains at most `pr-py-modnf` and `pr-suggest`.

Two things the code does that this runbook relies on (read from the merged code, 2026-11-20):

- With `llm: on_demand` the automatic diagnosis is rules-only (`fallback_reason = on_demand`) and
  `/cicd-doctor explain` upgrades it. With the default `llm: auto` and the key in `.env`, the
  automatic path would already call the model and `explain` would find nothing to upgrade (it only
  upgrades a published rules diagnosis, D-95). That is why `p3-on-demand.yml` goes to `main` first.
- The webhook answers **202** to an accepted command and **204** to every other `issue_comment`
  (a plain comment, an edit, an unauthorized author), storing nothing. So "a delivery with 204" is
  the expected sight for a comment that is not a command.

### Before you start

Everything of *Before you start* in *Live verification (P1c)* and *(P2)* still holds (check the
folder before every `git` command; stop old `cicd-doctor` processes). What is new:

1. **Run from a folder that has this branch's code** (the worktree
   `C:\dev\cicd-doctor\.claude\worktrees\p3-live-52db51`, or `C:\dev\cicd-doctor` after the branch
   is merged and `git pull`). The API, the worker and smee all run from that folder.
2. **The lab must be public for box 2 (first half).** A private repository cannot get a comment
   from an account without access; and an account invited with read access counts as
   `COLLABORATOR`. Check it in step 0. Making the lab public is a change of repository settings:
   ask for it, do not assume it.
3. **A second GitHub account** that is **not** a collaborator of the lab (G4).
4. **The commands change the rate window per pull request:** one accepted command per PR per
   5 minutes. Each box below uses its own pull request, except the second half of box 2, which
   must run **within 5 minutes** of the command of box 1.
5. **A configuration change on `main` of the lab is read within 5 minutes** (a file is remembered
   300 s, a missing one 60 s). After each change either wait or stop the worker (Ctrl+C) and start
   it again: that clears the memory of the process.
6. **GitHub can take several minutes to start the run of a push** (4 minutes were seen). Every block
   that looks for a run says what to do when the variable prints nothing: repeat its last two lines.
7. **Cost:** about 5 accepted commands, each with at most 2 model calls of about 7,000 input tokens
   each: about **USD 0.02** in total (plan §6), far under the cap of step B. The optional suite of
   G6 adds about USD 0.01.

### 0. Variables (once per PowerShell session, the "work terminal")

Replace `OWNER`, `SLUG` (the app's slug) and `OTHER` (the second account). All the blocks below,
except the three start-up ones, go in this **one** terminal, in this order; the variables live here
only. The first block is typed in the folder you run from:

```powershell
$LAB = "OWNER/cicd-doctor-lab"
$BOT = "SLUG[bot]"
$OTHER = "OTHER"
$APPDIR = (Get-Location).Path
$LABDIR = "$env:TEMP\lab-p3"
$APPDIR
git remote get-url origin
git branch --show-current
```

The last two lines must print the URL of the **cicd-doctor** repository (not the lab) and the branch
you mean to run. **[READ]** Is the lab public? (`isPrivate` must be `false` for box 2.)

```powershell
gh repo view $LAB --json visibility,isPrivate
```

### A. Prepare the app (G1) **[APP]** (box 5, the first half)

The commands of P3 need a permission and an event the dev app did not have. On 2026-11-20 the
settings page showed Issues = "No access", no "Issue comment" under *Subscribe to events* and no
`issue_comment` delivery in *Recent deliveries* (D-89). You do this by hand; no code does it.

1. GitHub → Settings → Developer settings → GitHub Apps → the dev app → *Permissions & events*.
2. *Repository permissions* → *Issues* → **Read and write**. Save.
3. *Subscribe to events* → tick **Issue comment** (it appears only after step 2). Save.
4. Each installation (the lab and this repository) gets an email and a banner asking to accept the
   new permission: open it and accept. Until then the event is not delivered and the token of the
   👀 reaction is refused with 422 (the diagnosis keeps working: its token does not ask for Issues,
   D-89).
5. Cross-check the page with the code (`adapters/github/auth.py`, SPEC §9.6). The permissions the
   code asks for are: Actions **read**, Checks **write**, Pull requests **write**, Issues **write**
   (only the explain flow), Contents **read** (only the configuration file), Metadata read. The
   events: `workflow_run` and `issue_comment`. If the page differs, stop and say so.
6. Write down, from *Advanced* → *Recent deliveries*, the `installation` delivery with the action
   `new_permissions_accepted` (its status must be **204**: the webhook ignores it) for each
   installation. That is the first row of the evidence table.

The check runs that are written cannot be deleted; they hang from commits of throwaway branches.

### B. The limits and the key (G2) **[ENV]** **[MONEY]**

Work on the **copy** of `.env` in the folder you run from; the real one in `C:\dev\cicd-doctor` stays
as it is, so "restore" is deleting the copy. Never print the file:

```powershell
if (-not (Test-Path .env)) { Copy-Item C:\dev\cicd-doctor\.env .env }
Select-String -Path .env -Pattern '^(OPENAI_API_KEY|OPENAI_MODEL|LLM_PRICE_INPUT_PER_MTOK|LLM_PRICE_OUTPUT_PER_MTOK)=.+' | ForEach-Object { $_.Line.Split('=')[0] }
```

It must print the four names (not the values). If one is missing, add it by hand (the model and
the prices of D-88: `gpt-6-luna`, `0.10`, `0.50`). Then open the copy in an editor and **lower**
two lines (add them if they are not there):

```powershell
notepad .env
```

| Line | Value for the test | Default (what the real `.env` gives) |
|---|---|---|
| `LLM_DAILY_BUDGET_USD` | `0.10` | `2.00` |
| `LLM_DAILY_CALLS_PER_INSTALLATION` | `15` | `30` |

(`LLM_ENABLED=true` and the key must be set; `EXPLAIN_MAX_RUNS=3` and `EXPLAIN_MAX_SUGGESTIONS=3`
are the defaults and stay.) Restore at the end (*Clean-up*): delete this copy and close the terminals.

### C. Start-up (once) **[LOCAL]**

In the folder you run from. Stop old `cicd-doctor` processes first, then:

```powershell
docker compose up -d db
uv sync
uv run alembic upgrade head
uv run alembic current
```

The last line must end with `0006 (head)` (P3 has no migration). Each of these in its own terminal,
**in the same folder**:

```powershell
uv run cicd-doctor api
```

```powershell
uv run cicd-doctor worker
```

```powershell
npx smee-client --url https://smee.io/CHANNEL --target http://127.0.0.1:8000/webhooks/github
```

Check it in the work terminal (it must say `ready`), and wait in the worker terminal for
`embedding model loaded in`:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/ready
```

### D. The lab: files, workflow ids and the configuration (G3) **[WRITES LAB]**

First publish `lab/` again (see *Publish*: `git subtree split` and `git push` to `main` of the
lab). **Do this before the configuration below**: a commit made directly on `main` of the lab makes
the next publish a non-fast-forward (see the clean-up). Then a clone, the ids of the workflows and
the configuration of the set-up:

```powershell
gh repo clone $LAB $LABDIR
Set-Location $LABDIR
git remote get-url origin
$WF_MODNF = gh api "repos/$LAB/actions/workflows/pr-py-modnf.yml" --jq .id
$WF_SUGGEST = gh api "repos/$LAB/actions/workflows/pr-suggest.yml" --jq .id
"$WF_MODNF $WF_SUGGEST"
```

Both ids must print (a 404 on `pr-suggest.yml` means the publish did not include it). The remote must
be the **lab**. Now the configuration of boxes 1, 2, 3 and 5, straight on `main`:

```powershell
Set-Location $LABDIR
git switch main
git pull
New-Item -ItemType Directory -Force .github | Out-Null
Copy-Item "$APPDIR\lab\config\p3-on-demand.yml" .github\cicd-doctor.yml
git add .github/cicd-doctor.yml
git commit -m "P3 live: llm on demand"
git push origin main
```

If the worker was already running, wait 5 minutes or restart it (the memory of the process).

### E. Box 5: the first real `issue_comment` delivery (G3) **[WRITES LAB]**

A plain comment (not a command) on a pull request of the lab. It also gives the pull request that
box 1 uses:

```powershell
Set-Location $LABDIR
git remote get-url origin
git switch main
git switch -c p3/explain
Add-Content -Path p3-scratch.txt -Value "explain" -Encoding ascii
git add p3-scratch.txt
git commit -m "p3 explain"
git push -u origin p3/explain
gh pr create --repo $LAB --base main --head p3/explain --title "P3 explain" --body "Live check of P3: explain"
$PR1 = gh pr view p3/explain --repo $LAB --json number --jq .number
$SHA1 = git rev-parse HEAD
gh pr comment $PR1 --repo $LAB --body "a plain comment, not a command"
```

Verification **[READ]**. In the app page (*Advanced* → *Recent deliveries*) find the `issue_comment`
delivery of that comment: its **status must be 204**, and the app page shows its id and time. Note
both. Then the rows on the database (a 204 stores nothing, so the table must **not** have a row for
it):

```powershell
Set-Location $APPDIR
docker compose exec -T db psql -U cicd -d cicd_doctor -c "SELECT delivery_id, event, action, received_at FROM webhook_deliveries WHERE event = 'issue_comment' ORDER BY received_at DESC LIMIT 5;"
```

Criterion: the delivery exists with 204 (first real delivery of the event: the permission and the
subscription work); no `issue_comment` row yet. The 202 of an accepted command is the next box.
If you want to see what an installation that did **not** accept does, leave this repository's
installation without accepting in step A and comment on a pull request here: no delivery of
`issue_comment` should appear for it (optional; write what you saw).

### 1. Box 1: a collaborator's command gets 👀 and an LLM diagnosis in 90 s or less

The pull request of step E has the runs of `pr-py-modnf` (it fails) and `pr-flaky` (ignored by the
configuration). Wait for the rules diagnosis (the check and the comment, about 10 s after the run
ends) and keep its ids **[READ]**:

```powershell
$RUN_M = gh run list --repo $LAB --branch p3/explain --workflow pr-py-modnf.yml --limit 1 --json databaseId --jq '.[0].databaseId'
$RUN_M
gh run watch $RUN_M --repo $LAB
```

If `$RUN_M` prints nothing the run has not started: repeat those two lines. Then:

```powershell
$checks1 = gh api "repos/$LAB/commits/$SHA1/check-runs?per_page=100" | ConvertFrom-Json
$mine1 = @($checks1.check_runs | Where-Object { $_.name -like 'CICD Doctor*' })
$mine1 | Select-Object id, name, status, conclusion
$idM = $mine1[0].id
$comments1 = gh api "repos/$LAB/issues/$PR1/comments?per_page=100" | ConvertFrom-Json
$sticky1 = @($comments1 | Where-Object { $_.user.login -eq $BOT -and $_.body.Split("`n")[0] -like "*workflow=$WF_MODNF -->*" })
$sticky1.Count
$cidM = $sticky1[0].id
$sticky1[0].body -match 'LLM on demand'
```

Criterion: **one** check `CICD Doctor: pr-py-modnf` (none for `pr-flaky`), **one** sticky comment,
and the comment says the LLM runs on demand (`True`). Now the command, with your account (an
`OWNER`), and the 90 s measurement. **[WRITES LAB]** **[MONEY]**:

```powershell
$URL1 = gh pr comment $PR1 --repo $LAB --body "/cicd-doctor explain"
$CMD1 = ($URL1 -split 'issuecomment-')[1]
$CMD1
$T1 = gh api "repos/$LAB/issues/comments/$CMD1" --jq .created_at
$T1
```

**Right now, within 5 minutes**, do the second half of box 2 (below: *the same command again*).
Then, about 30 s after the command, **[READ]**:

```powershell
$rx1 = gh api "repos/$LAB/issues/comments/$CMD1/reactions" | ConvertFrom-Json
$rx1 | Select-Object content, created_at, @{n='who';e={$_.user.login}}
$checks1b = gh api "repos/$LAB/commits/$SHA1/check-runs?per_page=100" | ConvertFrom-Json
$mine1b = @($checks1b.check_runs | Where-Object { $_.name -like 'CICD Doctor*' })
$mine1b | Select-Object id, name, status, conclusion
$mine1b[0].id -eq $idM
$mine1b[0].output.summary -match 'gpt-6-luna'
$mine1b[0].output.summary -match 'diag-v3'
$comments1b = gh api "repos/$LAB/issues/$PR1/comments?per_page=100" | ConvertFrom-Json
$sticky1b = @($comments1b | Where-Object { $_.user.login -eq $BOT -and $_.body.Split("`n")[0] -like "*workflow=$WF_MODNF -->*" })
$sticky1b.Count
$sticky1b[0].id -eq $cidM
$sticky1b[0].body -match 'gpt-6-luna'
([datetimeoffset]$rx1[0].created_at - [datetimeoffset]$T1).TotalSeconds
([datetimeoffset]$sticky1b[0].updated_at - [datetimeoffset]$T1).TotalSeconds
```

Criterion: one reaction `eyes` of the bot (`$BOT`); the **same** check run id and the **same**
comment id as before (`True`, `True`), now with `LLM gpt-6-luna · prompt diag-v3` in the source
line (the three `-match` lines print `True`); the last two lines are the seconds from your comment to
the 👀 and to the edited sticky: the second one must be **90 or less**. The worker terminal shows
`explain acknowledged seconds_since_command=…` and `explain queued seconds_since_command=… children=1`;
copy both. The rows **[READ]**:

```powershell
Set-Location $APPDIR
$q = "SELECT f.run_id, d.source, d.published, d.model, d.prompt_version, "
$q += "d.cost_usd, d.latency_ms, d.fallback_reason FROM diagnoses d "
$q += "JOIN failures f ON f.id = d.failure_id "
$q += "WHERE f.run_id = $RUN_M ORDER BY d.created_at;"
docker compose exec -T db psql -U cicd -d cicd_doctor -c $q
docker compose exec -T db psql -U cicd -d cicd_doctor -c "SELECT * FROM llm_usage ORDER BY day DESC LIMIT 2;"
docker compose exec -T db psql -U cicd -d cicd_doctor -c "SELECT id, kind, status, attempts, dedupe_key FROM jobs WHERE dedupe_key LIKE 'explain:%' ORDER BY id DESC LIMIT 4;"
```

Criterion: for `$RUN_M` two rows: the `rules` one (`published = f`, `fallback_reason = on_demand`)
and the `llm` one (`published = t`, with `gpt-6-luna`, `diag-v3`, a `cost_usd` of about 0.0003 to
0.001); `llm_usage.calls` of today went up by 1; the jobs `explain:<comment id>` and its child
`explain:<comment id>:<run id>:1` are `done`.

### 2. Box 2: no permission means no reaction and no job; a second command in 5 minutes is ignored

**The second half first (it has a 5 minute clock): the same command again.** Immediately after the
command of box 1 **[WRITES LAB]**:

```powershell
$URL2 = gh pr comment $PR1 --repo $LAB --body "/cicd-doctor explain"
$CMD2 = ($URL2 -split 'issuecomment-')[1]
$CMD2
```

Wait about 20 s and **[READ]**:

```powershell
$rx2 = gh api "repos/$LAB/issues/comments/$CMD2/reactions" | ConvertFrom-Json
@($rx2).Count
Set-Location $APPDIR
docker compose exec -T db psql -U cicd -d cicd_doctor -c "SELECT pr_number, comment_id, actor_login, created_at FROM command_invocations ORDER BY created_at DESC LIMIT 4;"
docker compose exec -T db psql -U cicd -d cicd_doctor -c "SELECT id, kind, status, attempts, last_error FROM jobs WHERE dedupe_key = 'explain:$CMD2';"
```

Criterion: **0** reactions on the second comment; `command_invocations` has **one** row for this
pull request (the one of `$CMD1`, none for `$CMD2`); the job `explain:<$CMD2>` exists and is `done`
(accepted by the webhook, refused by the limit: silence, D-93); the worker terminal says
`explain command refused by the rate limit`; the check and the comment did not change again.

**The first half: an account without permissions** (G4) **[OTHER ACCOUNT]**. Log in to GitHub with
`$OTHER` in a browser, open pull request `$PR1` of the lab and comment exactly
`/cicd-doctor explain`. Then **[READ]**:

```powershell
$comments2 = gh api "repos/$LAB/issues/$PR1/comments?per_page=100" | ConvertFrom-Json
$theirs = @($comments2 | Where-Object { $_.user.login -eq $OTHER })
$theirs | Select-Object id, author_association, created_at
$CMD3 = $theirs[0].id
$rx3 = gh api "repos/$LAB/issues/comments/$CMD3/reactions" | ConvertFrom-Json
@($rx3).Count
Set-Location $APPDIR
docker compose exec -T db psql -U cicd -d cicd_doctor -c "SELECT count(*) FROM jobs WHERE dedupe_key = 'explain:$CMD3';"
docker compose exec -T db psql -U cicd -d cicd_doctor -c "SELECT count(*) FROM command_invocations WHERE comment_id = $CMD3;"
```

Criterion: `author_association` is `NONE` or `CONTRIBUTOR` (not `COLLABORATOR`); **0** reactions;
**0** jobs; **0** rows. In the app page *Recent deliveries* the delivery of that comment shows
**204**. If the lab is private, this half stays open with that reason.

### 3. Box 3: a suggestion inside the diff is a review comment; outside it, text only

The suggestion is the model's, so the box depends on the model suggesting a change: when it does
not, say so in the table and retry with a new commit (a `FakeLLM` never counts). Every pull request
here changes `python/suggest/` so that `pr-suggest` runs; `pr-py-modnf` also runs and is explained
too (at most 2 runs, 2 model calls).

**Inside the diff** (the PR edits line 8, the line ruff names) **[WRITES LAB]** **[MONEY]**:

```powershell
Set-Location $LABDIR
git remote get-url origin
git switch main
git pull
git switch -c p3/inside
$f = "$LABDIR\python\suggest\legacy.py"
$t = [IO.File]::ReadAllText($f)
$t = $t.Replace("return value == None", "return value == None  # P3")
[IO.File]::WriteAllText($f, $t)
git diff --stat
git add python/suggest/legacy.py
git commit -m "p3 inside"
git push -u origin p3/inside
gh pr create --repo $LAB --base main --head p3/inside --title "P3 inside" --body "Live check of P3: a suggestion inside the diff"
$PR2 = gh pr view p3/inside --repo $LAB --json number --jq .number
$SHA2 = git rev-parse HEAD
Start-Sleep -Seconds 10
$RUN_S2 = gh run list --repo $LAB --branch p3/inside --workflow pr-suggest.yml --limit 1 --json databaseId --jq '.[0].databaseId'
gh run watch $RUN_S2 --repo $LAB
```

`git diff --stat` must show one line changed. If `$RUN_S2` prints nothing, repeat the last two
lines. When the run (and `pr-py-modnf`) have ended and the rules comment exists, the command:

```powershell
$URL4 = gh pr comment $PR2 --repo $LAB --body "/cicd-doctor explain"
$CMD4 = ($URL4 -split 'issuecomment-')[1]
$CMD4
```

About 40 s later **[READ]**:

```powershell
$fence = ([string][char]96) * 3
$rc2 = gh api "repos/$LAB/pulls/$PR2/comments?per_page=100" | ConvertFrom-Json
$mine2 = @($rc2 | Where-Object { $_.user.login -eq $BOT })
$mine2.Count
$mine2 | Select-Object id, path, line, start_line, side, start_side, commit_id, created_at
$mine2[0].commit_id -eq $SHA2
$mine2[0].body.Split("`n")[0]
$mine2[0].body.Contains($fence + "suggestion")
$mine2[0].body
```

Criterion: **one** review comment of the bot, on `python/suggest/legacy.py`, `side = RIGHT`,
`commit_id` equal to `$SHA2` (`True`), whose first line is
`<!-- cicd-doctor:v1 suggestion pr=<$PR2> failure=<uuid> -->`, with a block `suggestion` (`True`)
and the review note "Generated automatically from untrusted logs: review it before you apply it.".
For one line, `line` is 8 and `start_line` is empty: that **is** the shape of a single-line
comment. The sticky comment of `pr-suggest` says "Suggested change posted as a review comment":

```powershell
$comments4 = gh api "repos/$LAB/issues/$PR2/comments?per_page=100" | ConvertFrom-Json
$sticky4 = @($comments4 | Where-Object { $_.user.login -eq $BOT -and $_.body.Split("`n")[0] -like "*workflow=$WF_SUGGEST -->*" })
$sticky4[0].body -match 'Suggested change posted as a review comment'
Set-Location $APPDIR
$q = "SELECT f.run_id, d.source, d.published, d.prompt_version, "
$q += "d.suggestion IS NOT NULL AS has_suggestion, d.suggestion "
$q += "FROM diagnoses d JOIN failures f ON f.id = d.failure_id "
$q += "WHERE f.run_id = $RUN_S2 ORDER BY d.created_at;"
docker compose exec -T db psql -U cicd -d cicd_doctor -c $q
```

If there is **no** review comment: read `has_suggestion` and `suggested_fix` (the model may have
suggested nothing, or the validation dropped it, in which case the sticky has no "Suggested change"
line and the fix is plain text). Then the box stays open with that reason: it is not a bug.

**Outside the diff** (the PR edits only `notes.py`; `legacy.py` fails all the same, so the
model gets no diff and no suggestion can be posted) **[WRITES LAB]** **[MONEY]**:

```powershell
Set-Location $LABDIR
git remote get-url origin
git switch main
git switch -c p3/outside
$g = "$LABDIR\python\suggest\notes.py"
$u = [IO.File]::ReadAllText($g)
$u = $u.Replace("P3 live verification", "P3 outside")
[IO.File]::WriteAllText($g, $u)
git add python/suggest/notes.py
git commit -m "p3 outside"
git push -u origin p3/outside
gh pr create --repo $LAB --base main --head p3/outside --title "P3 outside" --body "Live check of P3: a suggestion outside the diff"
$PR3 = gh pr view p3/outside --repo $LAB --json number --jq .number
$SHA3 = git rev-parse HEAD
Start-Sleep -Seconds 10
$RUN_S3 = gh run list --repo $LAB --branch p3/outside --workflow pr-suggest.yml --limit 1 --json databaseId --jq '.[0].databaseId'
gh run watch $RUN_S3 --repo $LAB
```

Then the command, and the check about 40 s later:

```powershell
$URL5 = gh pr comment $PR3 --repo $LAB --body "/cicd-doctor explain"
$CMD5 = ($URL5 -split 'issuecomment-')[1]
$CMD5
```

```powershell
$rc3 = gh api "repos/$LAB/pulls/$PR3/comments?per_page=100" | ConvertFrom-Json
@($rc3 | Where-Object { $_.user.login -eq $BOT }).Count
$comments5 = gh api "repos/$LAB/issues/$PR3/comments?per_page=100" | ConvertFrom-Json
$sticky5 = @($comments5 | Where-Object { $_.user.login -eq $BOT -and $_.body.Split("`n")[0] -like "*workflow=$WF_SUGGEST -->*" })
$sticky5[0].body -match 'gpt-6-luna'
$sticky5[0].body -match 'Suggested change'
Set-Location $APPDIR
$q = "SELECT f.run_id, d.source, d.published, "
$q += "d.suggestion IS NOT NULL AS has_suggestion, "
$q += "left(d.suggested_fix, 120) AS fix FROM diagnoses d "
$q += "JOIN failures f ON f.id = d.failure_id "
$q += "WHERE f.run_id = $RUN_S3 ORDER BY d.created_at;"
docker compose exec -T db psql -U cicd -d cicd_doctor -c $q
```

Criterion: **0** review comments; the sticky has the `llm` diagnosis (`True`) with the fix as text
and **no** "Suggested change" line (`False`: the validation dropped a suggestion that could not be
inside a shown hunk, or the model gave none); `has_suggestion = f`.

**Measure what GitHub answers (G5) [PROBE]** This is the part that only the real API can tell
(the documentation lists only 403 and 422 for a comment outside the diff and says nothing of `suggestion`). The app treats **any** 422 as "text only"; this records
what GitHub says. It writes **review comments of your account** on pull request `$PR2`: the first
two are expected to be refused (nothing is created), the third is created and **deleted** at once.
The line of the first one does not exist; the line of the second exists but is outside the hunk:

```powershell
gh api -X POST "repos/$LAB/pulls/$PR2/comments" -f body="422 probe" -f commit_id=$SHA2 -f path="python/suggest/legacy.py" -F line=9999 -f side=RIGHT
```

```powershell
gh api -X POST "repos/$LAB/pulls/$PR2/comments" -f body="422 probe" -f commit_id=$SHA2 -f path="python/suggest/legacy.py" -F line=1 -f side=RIGHT
```

Copy the **whole** answer of each (the status and the JSON `message` and `errors`) into the evidence
table. If one of them is **accepted** (a comment is created), delete it:
`gh api -X DELETE repos/$LAB/pulls/comments/<id>` and write that down: it means the app's 422 branch
would not be reached for that case. Now the multi-line shape, on lines 7 and 8 (both inside the hunk):

```powershell
$pc = "repos/$LAB/pulls/$PR2/comments"
$multi = gh api -X POST $pc -f body="range probe" -f commit_id=$SHA2 -f path="python/suggest/legacy.py" -F start_line=7 -F line=8 -f start_side=RIGHT -f side=RIGHT | ConvertFrom-Json
$multi | Select-Object id, path, start_line, line, original_start_line, original_line, start_side, side, position
gh api -X DELETE "repos/$LAB/pulls/comments/$($multi.id)"
```

Criterion: `start_line = 7`, `line = 8`, `start_side = RIGHT`, `side = RIGHT` is the shape that
`GitHubReporter.post_suggestion` sends for a range (`start_line` below `line`). Write the printed
object in the table; if GitHub answers differently (a 422, a different pair), the adapter needs a
fix PR with its own D-NN. The comment is deleted by the last line (check with
`gh api repos/$LAB/pulls/$PR2/comments --jq length`: the app's comment only).

### 4. Box 4: the configuration (G3) **[WRITES LAB]**

Every state of the file is on `main` of the lab; after each change wait 5 minutes or restart the
worker. Pull requests of this box add nothing to `python/suggest/`, so only `pr-py-modnf` runs.

**4a. `comment: false` on the default branch.** Put the file on `main`:

```powershell
Set-Location $LABDIR
git remote get-url origin
git switch main
git pull
Copy-Item "$APPDIR\lab\config\p3-comment-off.yml" .github\cicd-doctor.yml
git add .github/cicd-doctor.yml
git commit -m "P3 live: comment false"
git push origin main
```

After the 5 minutes (or the restart), a new pull request that fails:

```powershell
git switch -c p3/cfg-off
Add-Content -Path p3-scratch.txt -Value "cfg-off" -Encoding ascii
git add p3-scratch.txt
git commit -m "p3 cfg off"
git push -u origin p3/cfg-off
gh pr create --repo $LAB --base main --head p3/cfg-off --title "P3 comment false" --body "Live check of P3: comment false"
$PR4 = gh pr view p3/cfg-off --repo $LAB --json number --jq .number
$SHA4 = git rev-parse HEAD
Start-Sleep -Seconds 10
$RUN_C = gh run list --repo $LAB --branch p3/cfg-off --workflow pr-py-modnf.yml --limit 1 --json databaseId --jq '.[0].databaseId'
gh run watch $RUN_C --repo $LAB
```

Verification **[READ]** (wait 30 s after the run ends):

```powershell
$checks4 = gh api "repos/$LAB/commits/$SHA4/check-runs?per_page=100" | ConvertFrom-Json
@($checks4.check_runs | Where-Object { $_.name -like 'CICD Doctor*' }) | Select-Object id, name, status, conclusion
$comments6 = gh api "repos/$LAB/issues/$PR4/comments?per_page=100" | ConvertFrom-Json
@($comments6 | Where-Object { $_.user.login -eq $BOT }).Count
```

Criterion: **one** check `CICD Doctor: pr-py-modnf` (`neutral`; none for `pr-flaky`, which is
ignored) and **0** comments of the bot.

**4b. The same file only in a PR branch has no effect.** The branch changes
`.github/cicd-doctor.yml` to `comment: true` and `llm: "off"` while `main` still says
`comment: false` and `llm: on_demand`:

```powershell
Set-Location $LABDIR
git remote get-url origin
git switch main
git switch -c p3/cfg-branch
Copy-Item "$APPDIR\lab\config\p3-branch-only.yml" .github\cicd-doctor.yml
git add .github/cicd-doctor.yml
git commit -m "p3 cfg branch"
git push -u origin p3/cfg-branch
gh pr create --repo $LAB --base main --head p3/cfg-branch --title "P3 config only in the branch" --body "Live check of P3: the file of the branch is not read"
$PR5 = gh pr view p3/cfg-branch --repo $LAB --json number --jq .number
$SHA5 = git rev-parse HEAD
Start-Sleep -Seconds 10
$RUN_B = gh run list --repo $LAB --branch p3/cfg-branch --workflow pr-py-modnf.yml --limit 1 --json databaseId --jq '.[0].databaseId'
gh run watch $RUN_B --repo $LAB
```

Verification **[READ]** (30 s after the run ends): if the file of the branch were read, the
comment would appear (`comment: true`).

```powershell
$checks5 = gh api "repos/$LAB/commits/$SHA5/check-runs?per_page=100" | ConvertFrom-Json
@($checks5.check_runs | Where-Object { $_.name -like 'CICD Doctor*' }) | Select-Object id, name, status, conclusion
$comments7 = gh api "repos/$LAB/issues/$PR5/comments?per_page=100" | ConvertFrom-Json
@($comments7 | Where-Object { $_.user.login -eq $BOT }).Count
```

Criterion: the check exists and **0** comments of the bot (the behaviour of `main`). The command is
a second proof, because the branch says `llm: "off"` (a 😕 if it were read) and `main` says
`on_demand` (a 👀 and an LLM diagnosis in the check) **[WRITES LAB]** **[MONEY]**:

```powershell
$URL6 = gh pr comment $PR5 --repo $LAB --body "/cicd-doctor explain"
$CMD6 = ($URL6 -split 'issuecomment-')[1]
Start-Sleep -Seconds 45
$rx6 = gh api "repos/$LAB/issues/comments/$CMD6/reactions" | ConvertFrom-Json
$rx6 | Select-Object content, created_at
$checks5b = gh api "repos/$LAB/commits/$SHA5/check-runs?per_page=100" | ConvertFrom-Json
@($checks5b.check_runs | Where-Object { $_.name -like 'CICD Doctor*' })[0].output.summary -match 'gpt-6-luna'
@(gh api "repos/$LAB/issues/$PR5/comments?per_page=100" | ConvertFrom-Json | Where-Object { $_.user.login -eq $BOT }).Count
```

Criterion: the reaction is `eyes` (not `confused`), the check now says `gpt-6-luna` (`True`) and
there is still no comment of the bot (`0`; with `comment: false` the explain edits the check only,
D-91).

**4c. An invalid file on the default branch turns the LLM off.**

```powershell
Set-Location $LABDIR
git remote get-url origin
git switch main
Copy-Item "$APPDIR\lab\config\p3-invalid.yml" .github\cicd-doctor.yml
git add .github/cicd-doctor.yml
git commit -m "P3 live: invalid configuration"
git push origin main
```

After the 5 minutes (or the restart), a new pull request, its failure and the command:

```powershell
git switch -c p3/cfg-invalid
Add-Content -Path p3-scratch.txt -Value "cfg-invalid" -Encoding ascii
git add p3-scratch.txt
git commit -m "p3 cfg invalid"
git push -u origin p3/cfg-invalid
gh pr create --repo $LAB --base main --head p3/cfg-invalid --title "P3 invalid configuration" --body "Live check of P3: invalid file"
$PR6 = gh pr view p3/cfg-invalid --repo $LAB --json number --jq .number
$SHA6 = git rev-parse HEAD
Start-Sleep -Seconds 10
$RUN_I = gh run list --repo $LAB --branch p3/cfg-invalid --workflow pr-py-modnf.yml --limit 1 --json databaseId --jq '.[0].databaseId'
gh run watch $RUN_I --repo $LAB
```

```powershell
$URL7 = gh pr comment $PR6 --repo $LAB --body "/cicd-doctor explain"
$CMD7 = ($URL7 -split 'issuecomment-')[1]
Start-Sleep -Seconds 30
$rx7 = gh api "repos/$LAB/issues/comments/$CMD7/reactions" | ConvertFrom-Json
$rx7 | Select-Object content, created_at
$checks6 = gh api "repos/$LAB/commits/$SHA6/check-runs?per_page=100" | ConvertFrom-Json
@($checks6.check_runs | Where-Object { $_.name -like 'CICD Doctor*' })[0].output.summary -match 'configuration is invalid'
Set-Location $APPDIR
docker compose exec -T db psql -U cicd -d cicd_doctor -c "SELECT f.run_id, d.source, d.published, d.fallback_reason FROM diagnoses d JOIN failures f ON f.id = d.failure_id WHERE f.run_id = $RUN_I;"
```

Criterion: the check says "The repository configuration is invalid: `llm`; LLM turned off." (`True`),
the only diagnosis of the run is `rules` with `fallback_reason = config_off`, and the command gets
**two** reactions in order: `eyes` (accepted) then `confused` (the LLM is off, D-96), with no model
call (`llm_usage.calls` does not move).

### Clean-up (restore by hand)

```powershell
gh pr close $PR1 --repo $LAB --delete-branch
gh pr close $PR2 --repo $LAB --delete-branch
gh pr close $PR3 --repo $LAB --delete-branch
gh pr close $PR4 --repo $LAB --delete-branch
gh pr close $PR5 --repo $LAB --delete-branch
gh pr close $PR6 --repo $LAB --delete-branch
```

Then, **[WRITES LAB]**, take the test file out of `main` of the lab (the lab has no configuration by
default):

```powershell
Set-Location $LABDIR
git remote get-url origin
git switch main
git pull
git rm .github/cicd-doctor.yml
git commit -m "P3 live: remove the test configuration"
git push origin main
```

By hand: stop the API, the worker and smee; **delete the `.env` copy** of the folder you ran from
(that restores the limits: the real `.env` was never edited); the key stays in the real `.env` as it
was; remove the worktree if it was one (`CLAUDE.md`, *Branches and worktrees*). The lab now has two
extra commits on `main`: the next *Publish* will be refused as a non-fast-forward; decide then
whether to push with `--force` (the lab is a throwaway mirror) or to rebuild it. The check runs
cannot be deleted.

### G6. Optional: the live injection suite with the 14 new cases **[MONEY]**

The 14 cases that carry a diff (D-99) have never run against the real model. They cost about USD
0.01 and need the key of the `.env` copy; they write nothing to GitHub:

```powershell
Set-Location $APPDIR
uv run pytest -m live tests/live/test_live_injection.py
```

Criterion: it passes (0 attacks won) in about a minute. Write the count of passed tests in the table.

### The fork box stays open

A command on a pull request **from a fork** (SPEC §13 P3 box 1, "including on a fork PR"; D-94;
`pull_request_target` and R-P3-5) needs an organization (or a second account) that holds a fork of
the lab: the fork's pull request must be opened from a repository that is not the lab, and the
account that comments must still be an `OWNER`, `MEMBER` or `COLLABORATOR` of the lab. That
organization does not exist yet, so the fork half **stays open**. What would be checked, and with
which evidence, when it exists: the 👀, a `llm` diagnosis for the fork run (`is_fork = t`), that the
`llm` row never appears in `cached_diagnosis` or as a similar failure of a normal run, and the
`is_fork` of a `pull_request_target` run. Meanwhile it is covered by the offline tests of D-94 and
`tests/e2e/test_explain_runs_e2e.py`.

### The evidence (paste what you observed)

Fill the **Observed** column with the real value (id, status, seconds, a row). A box is ticked in
SPEC §13 only from here; an empty cell means not observed.

| # | Box | What to paste | Expected | Observed |
|---|---|---|---|---|
| 1 | 5 | Recent deliveries: id, time and status of `installation` / `new_permissions_accepted` (lab, this repo) | 204 | |
| 2 | 5 | Recent deliveries: id, time and status of the plain `issue_comment` of step E | 204, no row in `webhook_deliveries` | |
| 3 | 5 | Recent deliveries: id, time and status of the first command (`$CMD1`) | 202 | |
| 4 | 1 | `$CMD1`, `$T1`, reaction `eyes` and its `created_at` | `eyes` of `$BOT` | |
| 5 | 1 | Seconds from `$T1` to the edited sticky (last line of the block) | 90 or less | |
| 6 | 1 | Worker lines `explain acknowledged` and `explain queued` with `seconds_since_command` | both present | |
| 7 | 1 | Same check run id and comment id before and after; `gpt-6-luna · diag-v3` in both | `True`, `True`, `True` | |
| 8 | 1 | `diagnoses` of `$RUN_M`: rules unpublished (`on_demand`), `llm` published, `cost_usd`; `llm_usage.calls` | 1 call more | |
| 9 | 2 | `$CMD2`: reactions, `command_invocations` rows, job `done` | 0 reactions, 1 row, `done` | |
| 10 | 2 | `$CMD3` of `$OTHER`: `author_association`, reactions, jobs, rows; delivery status | `NONE`/`CONTRIBUTOR`, 0, 0, 0, 204 | |
| 11 | 3 | Review comment of the bot on `$PR2`: id, `path`, `line`, `start_line`, `commit_id`, first line, `suggestion` block | one comment, `line` 8, `True` | |
| 12 | 3 | Sticky of `pr-suggest` on `$PR2`: "Suggested change posted…" | `True` | |
| 13 | 3 | `$PR3`: review comments of the bot; "Suggested change" in the sticky; `has_suggestion` | 0; `False`; `f` | |
| 14 | 3 | **422 probe** `line=9999`: the whole answer (status, `message`, `errors`) | a 422 (write the text) | |
| 15 | 3 | **422 probe** `line=1`: the whole answer | a 422 (write the text) | |
| 16 | 3 | **Range probe** lines 7 to 8: `start_line`, `line`, `start_side`, `side`, `position` | 7, 8, RIGHT, RIGHT | |
| 17 | 4 | `$PR4` (`comment: false`): check run id; comments of the bot | 1 check, 0 comments | |
| 18 | 4 | `$PR5` (the file only in the branch): comments of the bot; reaction; `gpt-6-luna` in the check | 0; `eyes`; `True` | |
| 19 | 4 | `$PR6` (invalid file): "configuration is invalid" in the check; `fallback_reason`; reactions | `True`; `config_off`; `eyes`, `confused` | |
| 20 | all | `llm_usage` of the day at the end: `calls`, `spent_usd` | 0.10 USD cap not reached | |
| 21 | G6 | `pytest -m live tests/live/test_live_injection.py`: result | passed, 0 attacks won | |
| 22 | fork | Why it stays open | no organization for the fork | |
