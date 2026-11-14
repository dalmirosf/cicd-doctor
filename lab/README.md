# cicd-doctor-lab

Source of the **cicd-doctor-lab** repository: GitHub Actions workflows that **fail on purpose**,
each with a known cause. CICD Doctor's evals use their logs as labelled cases (SPEC §11.1 of
the main repository; decision D-61).

Every workflow:

- runs only when started by hand (`workflow_dispatch`), so publishing the repository runs nothing;
- has `permissions: contents: read` and a `timeout-minutes`;
- needs no secret, cloud account or paid service. The secret in `secret-misspelled.yml` is
  misspelled on purpose and must **not** be created.

`cases.yaml` lists each case: its workflow, the diagnosis it should get (category, cause, fix)
and regexes that mark its evidence lines.

| Category | Cases |
|---|---|
| broken_test | `py-assert`, `py-ruff` (lint gate), `py-mypy` (type gate) |
| dependency | `py-modnf`, `py-resolve`, `pip-no-dist`, `docker-base`, `tf-init` |
| network | `dns`, `conn-refused` |
| config_or_secrets | `missing-input`, `secret-misspelled`, `docker-copy`, `tf-validate`, `aws-no-credentials` |
| timeout | `job-timeout` (ends `cancelled`, so it is not captured: SPEC §14), `step-timeout` |
| infra_or_deploy | `oom-137`, `disk-full` |
| flaky | `flaky-random`, `flaky-timing` (each fails about half of the time) |
| unknown | `opaque-exit`, `obscure-tool` |

A real `AccessDenied` from AWS would need an AWS account and an IAM role, so it is left out;
`aws-no-credentials` covers the AWS credential rules without one.

## Publish

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
Get-ChildItem lab\.github\workflows\*.yml | ForEach-Object { gh workflow run $_.Name -R $repo }
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
