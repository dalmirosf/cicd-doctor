# cicd-doctor-lab

Source of the **cicd-doctor-lab** repository: GitHub Actions workflows that **fail on purpose**,
each with a known cause. CICD Doctor's evals use their logs as labelled cases (SPEC §11.1 of
the main repository; decision D-61).

Every workflow:

- runs only when started by hand (`workflow_dispatch`), so publishing the repository runs nothing.
  The one exception is `pr-py-modnf.yml` (the P1c live verification, below): it runs on
  `pull_request`, so it also runs nothing until somebody opens a pull request;
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
| timeout | `job-timeout` (ends `cancelled`, so it is not captured: SPEC §14), `step-timeout` |
| infra_or_deploy | `oom-137`, `disk-full` |
| flaky | `flaky-random`, `flaky-timing` (each fails about half of the time) |
| unknown | `opaque-exit`, `obscure-tool` |

`pr-py-modnf.yml` is the same failure as `py-modnf` started by a pull request. It has no entry in
`cases.yaml` (it is not an eval case: the evals capture `py-modnf`) and exists only for the live
verification of P1c.

A real `AccessDenied` from AWS would need an AWS account and an IAM role, so it is left out;
`aws-no-credentials` covers the AWS credential rules without one.

## Publish

The lab repository is published **by hand** from this folder; nothing in the main repository
pushes to it. Publish again whenever a file here changes (the new `pr-py-modnf.yml` included).

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
Get-ChildItem lab\.github\workflows\*.yml | Where-Object { $_.Name -ne "pr-py-modnf.yml" } | ForEach-Object { gh workflow run $_.Name -R $repo }
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

### 7. Clean-up

```powershell
gh pr close $PR6 --repo $ME --delete-branch
gh pr close $PR --repo $LAB --delete-branch
gh pr close $PR3 --repo $LAB --delete-branch
```

The check runs that were written cannot be deleted; they hang from commits of throwaway branches.
