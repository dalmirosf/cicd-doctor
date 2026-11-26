# Terraform on AWS (P5)

Three independent root modules, applied in this order. Nothing in this folder has been applied yet:
the first `apply` is the live verification (PR h), with the owner's consent; its ordered runbook, consent sentences,
cost table and evidence table are the last section, [Live verification (P5)](#live-verification-p5). Decisions: D-121 (network
and cost), D-122 (secrets), D-124 (teardown), D-125 (layers and state), D-120 (HTTPS entry), D-123 (CD and migration).

| Folder | State | Lives | Holds |
|---|---|---|---|
| `bootstrap/` | **local** `terraform.tfstate` here (git-ignored) | forever, applied once | the private S3 bucket of the state of the other two |
| `persistent/` | S3, key `persistent/terraform.tfstate` | survives every demo | ECR repository, secret **containers**, ECS execution and task roles, GitHub OIDC provider and the deploy role of the CD |
| `ephemeral/` | S3, key `ephemeral/terraform.tfstate` | `up` and `down` per demo | VPC, subnets, security groups, log groups, ECS cluster, RDS PostgreSQL 17, the `api` and `worker` services, the migration task definition, the ALB, CloudFront |

## Region and organization policies (read this first)

The region is **us-east-2 (Ohio)**, not us-east-1. The account belongs to an AWS Organization whose root has the service
control policy `AdvancedModeRegionRestrictionSecurityControlPolicy`: in us-east-1 it denies everything except a list of
global services (IAM, STS, SSO, billing, budgets, CloudFront, logs, ACM, KMS, Route 53, WAF, Organizations ...), so RDS,
ECS, ECR, Secrets Manager, ELB, EC2/VPC and `s3:CreateBucket` do not work there; us-west-2 is stricter; every other region
denies everything (D-121). The IAM and CloudFront APIs are global and stay valid with a provider in us-east-2.

The organization also offers an SCP, `BudgetsSpendLimitDenyNewWorkloads`, as a spend-limit brake: it denies creating RDS,
ECS services, ALBs, CloudFront, Secrets Manager secrets and ECR repositories. It is not known whether it is attached to
this account. It also lists `ecs:CreateService`, `elasticloadbalancing:CreateLoadBalancer`, `cloudfront:CreateDistribution`
and `ec2:RunInstances`, which this stack needs. It does not block `ecs:RunTask` (the migration), task definitions, IAM,
logs or any delete. **If a `Create*` call fails with "explicit deny in a service control policy", stop and ask the owner.
Never try to detach, edit or work around an SCP.**

## Rules

- Without the owner, only `terraform fmt`, `terraform init -backend=false` and `terraform validate` run, each inside a
  module folder. Never `plan`, `apply`, `import` or `destroy`, never an `aws` command.
- Never commit `*.tfstate`, `*.tfvars`, `backend.hcl` or `.terraform/` (the `.gitignore` covers them); the
  `.terraform.lock.hcl` and the `*.example` files are tracked.
- Secret **values** never enter Terraform or its state (D-122). The owner loads them by hand (step 3).
- Check without AWS (what the owner or a reviewer can run any time):

```powershell
terraform fmt -check -recursive infra/terraform
foreach ($d in 'bootstrap', 'persistent', 'ephemeral') {
    terraform -chdir="infra/terraform/$d" init -backend=false -input=false
    terraform -chdir="infra/terraform/$d" validate
}
uv run pytest tests/unit/test_infra_guard.py
```

## Before the first command (owner)

MFA on the root user; an admin user whose short-lived credentials come from `aws login --profile cicd-demo` (console
credentials, rotated about every 15 minutes) or from IAM Identity Center and `aws sso login` (no long-lived access keys);
an AWS Budgets alert (USD 20, 50 % and 80 %); look in the console at which credits or free-plan limits the new
account has. Then, in a PowerShell session (the profile name is yours):

```powershell
$env:AWS_PROFILE = '<your-sso-profile>'
$env:AWS_REGION = 'us-east-2'
aws sts get-caller-identity   # confirm it is the account you mean to use
```

## 1. Bootstrap (once)

```powershell
cd infra/terraform/bootstrap
terraform init
terraform apply
terraform output     # prints state_bucket_name and region
```

Its state stays in `bootstrap/terraform.tfstate` on your machine (git-ignored). Keep that file: it is the only
record of the bucket. If it is lost, `terraform import` the bucket into a new bootstrap state. The bucket has
`prevent_destroy`; it is private, versioned, SSE-S3 encrypted and denies non-TLS requests.

## 2. Backend files (once per layer)

```powershell
Copy-Item infra/terraform/persistent/backend.hcl.example infra/terraform/persistent/backend.hcl
Copy-Item infra/terraform/ephemeral/backend.hcl.example infra/terraform/ephemeral/backend.hcl
# edit both: bucket = "<state_bucket_name output>"
```

## 3. Up

The order matters: the migration needs the database and the image, and the services need the migration.

**3a. Persistent layer (once, then it stays).** `terraform.tfvars` needs `github_owner` (your GitHub login) and the two
numeric ids `github_owner_id` and `github_repo_id` (`gh api users/<owner> --jq .id`, `gh api repos/<owner>/<repo> --jq .id`;
not secrets; D-130).

```powershell
terraform -chdir=infra/terraform/persistent init -backend-config=backend.hcl
terraform -chdir=infra/terraform/persistent apply
terraform -chdir=infra/terraform/persistent output secret_names
terraform -chdir=infra/terraform/persistent output deploy_role_arn
```

If `apply` says the OIDC provider already exists (`EntityAlreadyExists`), the account already has one for GitHub: import
it (`terraform import aws_iam_openid_connect_provider.github <its arn>`) with the owner instead of creating a second.

Load each value by hand, outside the chat and outside Terraform (the value is read from a file or a prompt, never typed
on the command line; the names come from `secret_names`):

```powershell
aws secretsmanager put-secret-value --secret-id cicd-doctor/github-app-private-key --secret-string file://<path-to-the-pem>
# the same for github-webhook-secret, openai-api-key, langfuse-public-key and langfuse-secret-key
```

A task whose secret has no value does not start, so load all five before the services are enabled (3e). The API task reads only the webhook secret; the worker reads all five.

Owner, once: protect `main` in GitHub (pull request and review required, no direct pushes, no force pushes). The deploy role is trusted for every run on `main`, so this is what stands between a push and the application secrets (D-123). `cd.yml` (D-128) triggers on `push` to `main` only, and no workflow may combine `pull_request_target` with `id-token: write`.

**3b. An image in ECR.** The first `apply` of the ephemeral layer needs `image_tag`, and the migration needs the image to
exist. Push it with the CD (PR f) or by hand from a clone at the commit you want to run:

```powershell
$repo = terraform -chdir=infra/terraform/persistent output -raw ecr_repository_url
aws ecr get-login-password | docker login --username AWS --password-stdin $repo.Split('/')[0]
docker build -t "${repo}:<tag>" .
docker push "${repo}:<tag>"
```

**3c. Ephemeral layer, services off.** `terraform.tfvars` needs `image_tag` and `github_app_id`
(optionally `openai_model` and the prices). `services_enabled` stays `false`, so both services exist with zero tasks.

```powershell
terraform -chdir=infra/terraform/ephemeral init -backend-config=backend.hcl
terraform -chdir=infra/terraform/ephemeral apply
```

RDS rotates its managed master password every 7 days and tasks read it only when they start: keep a session well under a week, or restart the tasks after a rotation.

RDS takes several minutes and **CloudFront takes several more** to deploy (`apply` waits for it): expect about 10 to 20
minutes in all (not measured; part of the live PR).

**3d. The migration, once per `up` (the database is new every time).**

```powershell
$cluster = terraform -chdir=infra/terraform/ephemeral output -raw cluster_name
$net = terraform -chdir=infra/terraform/ephemeral output -raw migrate_network_configuration
$task = aws ecs run-task --cluster $cluster --task-definition cicd-doctor-migrate --launch-type FARGATE --network-configuration $net --query 'tasks[0].taskArn' --output text
aws ecs wait tasks-stopped --cluster $cluster --tasks $task
aws ecs describe-tasks --cluster $cluster --tasks $task --query 'tasks[0].containers[0].exitCode'   # must print 0
aws logs tail /cicd-doctor/migrate --since 15m   # on a failure: the reason
```

Exit code 0 means `alembic upgrade head` ran, including `CREATE EXTENSION vector`. Anything else: do not enable the
services; read the log. (`Settings` builds the database URL from `DB_HOST` and the other four parts, D-126.) The CD in PR f does exactly this with the new
image before it updates the services.

**3e. Services on.** Set `services_enabled = true` in `terraform.tfvars` and apply again: one `api` task and one `worker`
task start.

```powershell
terraform -chdir=infra/terraform/ephemeral apply
terraform -chdir=infra/terraform/ephemeral output webhook_url
```

Then `GET <webhook_url minus /webhooks/github>/health` through the same domain must answer `{"status":"ok"}`. A second
`terraform plan` should show no changes. If the CD has deployed since, the services keep the CD's task definition:
Terraform ignores that attribute (D-123).

**3f. The webhook of the production GitHub App (owner, once per `up`).** The CloudFront domain is new every time the
stack is created. In the GitHub App settings, set the webhook URL to the `webhook_url` output; a "Redeliver" of a recent
delivery must show a 2xx answer. Do not recreate the stack in the middle of a verification (D-124).

## 4. Down (ephemeral only)

Copy out whatever you want to keep first (logs, ids, screenshots): the log groups and the database go with it. Deactivate
the webhook of the production GitHub App (or leave it, knowing deliveries will fail) once the stack is gone.

```powershell
terraform -chdir=infra/terraform/ephemeral destroy
```

CloudFront must be disabled before it is deleted, so `destroy` also waits several minutes for it.

Leave the persistent layer alone: its secrets, ECR repository, OIDC provider and roles are the point of the split. To remove
everything (end of the project): destroy ephemeral, then persistent (`secret_recovery_window_days = 7` keeps the
names reserved for a week; set it to `0` and apply before destroying to free them), then remove the `prevent_destroy`
block of the bucket in `bootstrap/`, apply, and destroy it.

### After `down`, each of these must show zero (the console in `us-east-2`, or the CLI one-liners)

| Check | Command |
|---|---|
| RDS instances and snapshots | `aws rds describe-db-instances --query "DBInstances[].DBInstanceIdentifier"` and `aws rds describe-db-snapshots --query "DBSnapshots[].DBSnapshotIdentifier"` |
| Load balancers (ALB/NLB) | `aws elbv2 describe-load-balancers --query "LoadBalancers[].LoadBalancerArn"` |
| ECS tasks and services | `aws ecs list-clusters` (empty) |
| Public IPv4 addresses | `aws ec2 describe-network-interfaces --query "NetworkInterfaces[].Association.PublicIp"` and `aws ec2 describe-addresses` |
| NAT gateways | `aws ec2 describe-nat-gateways --filter Name=state,Values=available,pending` |
| CloudFront distributions | `aws cloudfront list-distributions --query "DistributionList.Items[].Id"` |
| VPC left over | `aws ec2 describe-vpcs --filters Name=tag:Project,Values=cicd-doctor` |
| Secret of the RDS password | `aws secretsmanager list-secrets --query "SecretList[].Name"` shows only the five `cicd-doctor/...` ones |

Also look at Billing > Cost Explorer the next day.

## Quick up and down (from the second time on)

After the first live verification (P5 h) the expensive layer is rebuilt in about 15 to 25 minutes. What stays between two
sessions, at about USD 2.05 a month: the state bucket, the ECR repository with its last three images, the five secrets
**with their values**, the roles and the OIDC provider. Your local `backend.hcl` and `terraform.tfvars` files stay too.

**Up.** Every `terraform` and `aws` command below is yours (the owner), and `aws login --profile cicd-demo` comes first:
its credentials rotate about every 15 minutes, so start a long `apply` right after a fresh login. In Windows PowerShell 5.1
write `"-out=name.tfplan"` and `"-backend-config=backend.hcl"` **in quotes**: unquoted, the dot splits the argument and
`plan` fails with "Too many command line arguments".

1. `image_tag` in `ephemeral/terraform.tfvars` must be a tag that still exists in ECR (`aws ecr describe-images`); the
   repository keeps the last three. Set `services_enabled = false`.
2. `terraform -chdir=infra/terraform/ephemeral plan "-out=ephemeral.tfplan"`, read it, then `apply ephemeral.tfplan`.
   Measured on 2026-11-25: 44 resources in about 6 minutes. The services exist with zero tasks.
3. The migration (3d above). The database is new and empty every time, so the cache and the memory of failures start
   from nothing: the first diagnosis of each kind of failure calls the model. Measured: about 1 minute, exit code 0.
4. `services_enabled = true`, `plan`, `apply` (under a minute), then wait for the target `healthy`
   (`aws elbv2 wait target-in-service --target-group-arn <arn>`; about 30 seconds).
5. **The CloudFront domain is new every time.** Set the webhook URL of the production GitHub App to
   `terraform -chdir=infra/terraform/ephemeral output -raw webhook_url`, turn "Active" on, and "Redeliver" a recent
   delivery: a 2xx is the proof that the body and the signature arrive intact.
6. Check `GET /health` and `GET /ready` through the same domain. A second `plan -detailed-exitcode` of both layers must
   exit 0.

Running cost with the services on is about USD 0.11 an hour (not measured; Cost Explorer shows it the next day). Keep a
session to the hours you use.

**Down.** Copy out what you want to keep, turn "Active" off in the app, then `terraform -chdir=infra/terraform/ephemeral
destroy` and the table *After `down`*. The persistent layer is never destroyed here.

**A repository of your own or another one.** The production app is private and installed only on the lab. For another
repository of yours: GitHub, Settings, Applications, Installed GitHub Apps, Configure, and add the repository. For
someone else's public repository the app cannot be installed (it is private): fork it into your account and install the app
on the fork, or make the app public (not recommended for a demo). A repository with `.github/cicd-doctor.yml` that sets
`llm: off`, `on_demand` or an invalid file does not call the model, and so leaves no trace in Langfuse.

**Two facts to remember about the live checks.**

- GitHub masks a token of its own format (`ghp_` plus 36 letters) in the Actions log as `***`, so a planted GitHub-shaped
  token proves nothing about our redactor: the app never sees it. Plant a key our redactor knows and GitHub does not mask,
  an AWS access key id (`AKIA` plus 16 characters, assembled at run time so no secret-shaped literal is committed, D-59).
- Langfuse keeps only the first 4000 characters of a trace's input and output (`MAX_TEXT_CHARS` in
  `adapters/observability/langfuse_tracer.py`). The input of a run is usually longer and the failure is at its end, so
  look for the redaction marker in the **output**, where the model quotes the failing line: put the key inside the error
  line itself (`[REDACTED:aws_access_key_id]` shows there and in the sticky comment).

## Cost

**All numbers below are estimates, not measurements.** The prices are from the public AWS price pages as I remember them
(on-demand, Linux x86, 730-hour month). Ohio is assumed to be priced like N. Virginia, which is normally the case: confirm it; none comes from the provider documentation, and none was checked in
the console. Confirm them in the AWS console and by spike C (a real session); spike B1 already showed that `db.t4g.micro` and PostgreSQL 17.7 to 17.11 are offered in us-east-2 before relying
on them. The sizes are the plan's assumptions: API 0.25 vCPU / 0.5 GB, worker 0.5 vCPU / 1 GB, one task each (ARM Fargate would be about 20 % cheaper, but the CD builds x86).

| Item (up) | Assumption | USD per hour |
|---|---|---|
| ALB | 0.0225 per hour + about 0.008 for the minimum LCU | 0.0305 |
| RDS `db.t4g.micro`, single-AZ | 0.016 per hour | 0.016 |
| RDS gp3 storage, 20 GB | 0.115 per GB-month | 0.003 |
| Fargate API | 0.25 vCPU x 0.04048 + 0.5 GB x 0.004445 | 0.012 |
| Fargate worker | 0.5 vCPU x 0.04048 + 1 GB x 0.004445 | 0.025 |
| Public IPv4 | 0.005 per address per hour: 2 for the ALB, 1 per task (the migration task adds one for a minute) | 0.020 |
| CloudFront (the first 1 TB and 10 million requests a month are free), CloudWatch Logs, data transfer | pennies at demo volume | about 0 |
| **Total with everything up** | | **about 0.11** |

The ALB, the two Fargate tasks, RDS and the four public addresses are the whole bill. A 4-hour verification session is about USD 0.45; a full day about USD 2.6. The migration task, run for a minute, is
a fraction of a cent. Left up for a month the stack would be about USD 80, which is why `down` exists.

| Persistent layer (always on) | Assumption | USD per month |
|---|---|---|
| 5 secrets | 0.40 each (the RDS password secret adds 0.40 prorated while the database exists) | 2.00 |
| ECR | 3 images, about 0.5 GB at 0.10 per GB-month | 0.05 |
| State bucket, IAM, S3 requests | pennies | about 0 |
| **Total** | | **about 2.05** |

A new account may have credits or a free plan that covers part of this: look in the console, do not count on it.

## What the later PRs attach to

`terraform -chdir=infra/terraform/ephemeral output` prints `webhook_url`, `cloudfront_domain`, `alb_dns_name`,
`service_names`, `migrate_task_family`, `migrate_network_configuration`, `cluster_name`, `public_subnet_ids`,
`security_group_ids` (alb, api, worker, db), `log_group_names` (`/cicd-doctor/api`, `/worker`, `/migrate`),
`execution_role_arn`, task role ARNs, `ecr_repository_url`, `db_host`, `db_port`, `db_name`, `db_username` and
`db_master_secret_arn`. `persistent` outputs `secret_arns` keyed by environment variable, `deploy_role_arn` (the role
`cd.yml` assumes by OIDC; not a secret) and `github_trusted_sub`. The containers get `DB_HOST`, `DB_PORT`, `DB_NAME`,
`DB_USER` and `DB_PASSWORD` (from the RDS secret); `Settings` builds the one database URL from them (D-126).

## CD (`.github/workflows/cd.yml`, D-128)

On every push to `main` the workflow builds the image, pushes it as `cicd-doctor:<commit sha>` (the repository is
immutable: no `latest`) and, **if the ephemeral layer is up**, registers new task definition revisions with that image,
runs the migration task (it must exit 0), then updates `api` and `worker` and waits for them to be stable. If the cluster
is down it ends green with a notice after the push. It uses no AWS key: the job asks GitHub for an OIDC token and assumes
the deploy role.

Owner, once, after `persistent` is applied: in the repository settings, **Settings > Secrets and variables > Actions >
Variables**, create `AWS_DEPLOY_ROLE_ARN` (the output `deploy_role_arn`) and, only if the region is not `us-east-2`,
`AWS_REGION`. They are variables, not secrets: neither is sensitive. A first run with the variable missing fails at once
with a message. The deploy role's permissions are in `persistent/oidc.tf` and the workflow uses nothing else.

## Live verification (P5)

This is the runbook that checks the AWS stack and the production GitHub App against SPEC §13 P5 (boxes 1, 2, 4, 5 and
6; box 3, the Terraform GitHub provider, was dropped by D-129). **It was run on 2026-11-25**; what was observed is in
the evidence table at the end, and the costs are still estimates until Cost Explorer is read. The split is
the one of `lab/README.md` ("Live verification (P1c)" and "(P3)"): **you** run the blocks tagged WRITES and SPENDS; the
READ-ONLY blocks only read, and a session that helps you reads with `aws ... describe/list/get` and `gh` `GET` calls.
Claude never runs a `terraform plan/apply/destroy/import`, never an `aws` command and never reads or prints a secret, a
`.env` or the state (see *Rules*). Everything is for **Windows PowerShell 5.1**: no `&&`, no `2>&1` on a native command,
no `<placeholder>` outside quotes, and no two variables whose names differ only in case (`$PROFILE` is a PowerShell
automatic variable: this runbook never uses it).

Decisions it relies on: D-117 (JSON logs), D-118 (Langfuse), D-119 (image), D-120 (CloudFront), D-121 (network, region
and the SCPs), D-122 (secrets), D-123 and D-128 (CD and migration), D-124 (teardown), D-125 (layers), D-126 (database URL).

### Tags and consent

| Tag | What it does | Consent |
|---|---|---|
| **READ-ONLY** | Reads AWS, GitHub or your machine. `terraform plan` is here: it reads, writes nothing in AWS (it can take the state lock for a moment). | none; but `plan` and every `aws` command still wait for **you**, because Claude does not run them |
| **WRITES** | Creates or changes something in AWS or GitHub that costs nothing by the hour (a bucket, the secret containers, a repository variable, a webhook URL, a pull request). | one sentence per step, given in the chat (shown at each step) |
| **SPENDS** | Creates or leaves running something billed by the hour or per call (RDS, ALB, Fargate, public IPv4, CloudFront, OpenAI, Langfuse beyond the free plan). | the same, naming the account, the region and the time box |
| **BY HAND** | Only you can do it: Claude will not, even if you ask (see the table near the end). | n/a |

A consent is for **that step, in this session**. It does not carry over to the next step, to another account or to a
later session. If the chat only says "go", the step does not run.

### Variables (once per PowerShell session)

Replace the words in capitals. Run from the root of the clone you deploy from (the folder with `infra/`).

```powershell
$TF  = 'infra/terraform'
$ME  = 'OWNER/cicd-doctor'
$LAB = 'OWNER/cicd-doctor-lab'
$BOT = 'PROD-APP-SLUG[bot]'      # the slug of the PRODUCTION app, not the dev one (D-30)
$env:AWS_PROFILE        = 'cicd-demo'
$env:AWS_REGION         = 'us-east-2'
$env:AWS_DEFAULT_REGION = 'us-east-2'
```

### 0. Preconditions (READ-ONLY, you tick each one)

| # | Precondition | How you know |
|---|---|---|
| 1 | MFA is on the root user of the account you use, and you work as an admin user, never as root | the console shows it |
| 2 | The **budget of the account you chose** is in place and large enough for the estimate: about USD 0.11 per hour up, so about USD 0.5 for a 4 h session, plus USD 2.05 a month for the persistent layer (§Cost). A budget of USD 1 would alert at the first session | `aws budgets describe-budgets --account-id (aws sts get-caller-identity --query Account --output text) --query "Budgets[].{name:BudgetName,limit:BudgetLimit.Amount}"` |
| 3 | The production GitHub App exists (you created it; Claude does not). Permissions, copied from `DIAGNOSIS_PERMISSIONS`, `EXPLAIN_PERMISSIONS` and `CONFIG_PERMISSIONS` in `adapters/github/auth.py` and SPEC §9.6: **Actions: read**, **Checks: write**, **Pull requests: write**, **Issues: write**, **Contents: read**, **Metadata: read**. Events: **Workflow run** and **Issue comment** (installation events arrive by default) | the app's settings page |
| 4 | Its **webhook secret** is a long random value you chose and still have (the same one goes into Secrets Manager in step 5); its **private key** `.pem` is downloaded to a folder only you can read (never in the repository); its **App ID** is noted (a number, not a secret) | by eye |
| 5 | The app's **App ID is 5913403** (a plain setting, not a secret; it goes into `github_app_id`), it is installed **only on `cicd-doctor-lab`**, and its `.pem` is stored outside the repository. Its webhook URL is a **placeholder** (`https://example.invalid/webhooks/github`) and "Active" is on or off as you prefer; the real URL is set in step 12 (the app is already installed, so no `installation` event will arrive: the first real delivery is a `workflow_run` of step 13, or a redelivery of an old one) | the app's settings page |
| 6 | The **dev** app and the local processes are out of the way for the acceptance flow: stop `cicd-doctor api`, `worker` and `smee`, and uninstall the dev app from the lab (or deactivate its webhook) before step 13, or two bots will answer the same pull request | `Get-Process` lists no process whose path contains `cicd-doctor` |
| 7 | A Langfuse cloud project exists in the **US** region, so `LANGFUSE_HOST` is `https://us.cloud.langfuse.com` (the owner is confirming it from the browser URL; if it turns out to be the EU host `https://cloud.langfuse.com`, use that one). The Terraform variable `langfuse_host` defaults to the EU host, so set it in `terraform.tfvars` (step 8). `LANGFUSE_HOST` is a plain setting, not a secret; the public and secret keys **are** secrets, loaded by hand in step 5. Langfuse's "AI powered features" (data sent to AWS Bedrock) are left **OFF** | the project settings |
| 8 | `main` is protected in GitHub (pull request and review required, no direct push, no force push): the deploy role trusts every run on `main` (D-123) | `gh api repos/OWNER/cicd-doctor/branches/main/protection --jq .required_pull_request_reviews` prints an object |
| 9 | Docker Desktop is running (the first image is built by the CD, but step 2 builds it locally) | `docker version` |
| 10 | An OpenAI key and `OPENAI_MODEL` (`gpt-6-luna`, D-88) and its two prices per million tokens (the prices go into `terraform.tfvars`, the key into Secrets Manager) | by eye |
| 11 | The lab's `main` has **no** `.github/cicd-doctor.yml`, or one with the default `llm: auto` (a leftover `p3-*.yml` with `llm: "off"`, `on_demand` or an invalid file would stop the model call and with it the trace) | `gh api repos/OWNER/cicd-doctor-lab/contents/.github/cicd-doctor.yml` answers 404 |

The tools and the credentials (READ-ONLY):

```powershell
aws --version
terraform version
docker --version
gh --version
gh auth status
aws login --profile cicd-demo
aws sts get-caller-identity --profile cicd-demo --query "{account:Account,arn:Arn}"
```

`aws login` opens the browser and gives **console credentials that rotate about every 15 minutes**. Read the account id in
the answer and confirm it is the account you mean to use (you decide that, not the runbook). Terraform's AWS provider
must read that profile too: **whether it does is unverified** and is found at the first `plan` of step 3 (the
provider is `~> 6.74`; support for the `aws login` credential type came late in the 6.x line). If the `plan` says it
cannot find credentials, use the fallback, which exports the short-lived credentials of the profile into **this**
session only (nothing is printed, nothing is written to a file):

```powershell
Invoke-Expression (aws configure export-credentials --profile cicd-demo --format powershell | Out-String)
```

Both ways expire. The long steps (`apply` of the ephemeral layer: 10 to 20 minutes, `destroy`: several) must start right
after a fresh `aws login` and, with the fallback, right after a fresh export: credentials that expire in the middle of an
apply leave a half-built stack and, sometimes, a state lock (see *Abort*).

Region and policy canary (READ-ONLY). It proves that the account may run workloads in `us-east-2` and that the SCP of the
organization has not changed since D-121 measured it:

```powershell
aws rds describe-db-engine-versions --engine postgres --engine-version 17.11 --region us-east-2 --query "DBEngineVersions[0].EngineVersion" --output text
```

It prints `17.11`. An "explicit deny in a service control policy" here is already a **stop** (see *Stop conditions*).

### 1. Offline checks (READ-ONLY, your machine)

Same as the box in *Rules*, plus the repository gates. Nothing here touches AWS:

```powershell
terraform fmt -check -recursive $TF
foreach ($layer in 'bootstrap', 'persistent', 'ephemeral') {
    terraform -chdir="$TF/$layer" init -backend=false -input=false
    terraform -chdir="$TF/$layer" validate
}
uv run pytest tests/unit/test_infra_guard.py tests/unit/test_cd_workflow.py tests/unit/test_docker_files.py
```

### 2. Box 1: the image and `docker compose up` (LOCAL, no AWS)

`.env` needs `GITHUB_APP_ID` and `GITHUB_WEBHOOK_SECRET` (the **dev** values are fine here) and the key at
`docker/secrets/github-app.pem` (`GITHUB_PRIVATE_KEY_FILE`). Do not paste any of them anywhere.

```powershell
$t0 = Get-Date
docker compose -f docker/compose.yaml --env-file .env up --build -d
do { Start-Sleep -Seconds 2; $ready = try { (Invoke-RestMethod http://127.0.0.1:8000/ready).status } catch { 'down' } } until ($ready -eq 'ready' -or ((Get-Date) - $t0).TotalSeconds -gt 180)
"ready after {0:N0} s: {1}" -f ((Get-Date) - $t0).TotalSeconds, $ready
docker image ls --format "{{.Repository}}:{{.Tag}} {{.Size}}" | Select-String cicd-doctor
docker compose -f docker/compose.yaml --env-file .env exec -T api id -u
docker compose -f docker/compose.yaml --env-file .env ps
docker compose -f docker/compose.yaml --env-file .env down
```

Criterion: `ready`, the user id is **10001** (not root), the worker is `running`, and the image size is written in the
evidence table (D-119 measured 769 MB). `docker compose down` keeps the database volume; add `-v` only if you want it gone.

### 3. Bootstrap: the state bucket

**Plan (READ-ONLY).** Consent: *"plan bootstrap in account A, us-east-2"* (A = the id from step 0).

```powershell
terraform -chdir="$TF/bootstrap" init
terraform -chdir="$TF/bootstrap" plan -out=bootstrap.tfplan
```

This is where the credentials question of step 0 is answered. Read the plan: one S3 bucket and its settings, nothing else.

**Apply (WRITES).** Consent: *"apply bootstrap in account A, us-east-2"*. A bucket costs pennies.

```powershell
terraform -chdir="$TF/bootstrap" apply bootstrap.tfplan
terraform -chdir="$TF/bootstrap" output
```

Keep `infra/terraform/bootstrap/terraform.tfstate` (local, git-ignored): it is the only record of the bucket. Then write
the two `backend.hcl` files (the bucket name is the output `state_bucket_name`):

```powershell
Copy-Item "$TF/persistent/backend.hcl.example" "$TF/persistent/backend.hcl"
Copy-Item "$TF/ephemeral/backend.hcl.example" "$TF/ephemeral/backend.hcl"
```

Edit both by hand (`bucket = "..."`), and write `$TF/persistent/terraform.tfvars` with
`github_owner = "OWNER"` (your GitHub login), `github_owner_id` and `github_repo_id` (the numeric ids, D-130).

### 4. Persistent layer: ECR, secret containers, roles, OIDC

**Plan (READ-ONLY).** Consent: *"plan persistent in account A"*.

```powershell
terraform -chdir="$TF/persistent" init -backend-config=backend.hcl
terraform -chdir="$TF/persistent" plan -out=persistent.tfplan
```

Read it: one ECR repository (immutable tags), five secret containers with **no** version, the ECS roles, the GitHub OIDC
provider and the role `cicd-doctor-deploy`. If the OIDC provider already exists in the account, the `apply` fails with
`EntityAlreadyExists`: stop and import it with the owner (see 3a above).

**Apply (WRITES).** Consent: *"apply persistent in account A"*. About USD 2.05 a month from here on (§Cost). If a
`Create*` fails with "explicit deny in a service control policy" (Secrets Manager and ECR are on the SCP's list): **stop**.

```powershell
terraform -chdir="$TF/persistent" apply persistent.tfplan
terraform -chdir="$TF/persistent" output secret_names
terraform -chdir="$TF/persistent" output deploy_role_arn
```

### 5. The five secret values, by hand (BY HAND, WRITES)

You do this; Claude does not, and the values never go into the chat, a `.tfvars`, an `.env` or a command line. The
function reads each value with a masked prompt (or the key from a file), writes it to a temporary file of your own
profile with no trailing newline, hands the **file** to the CLI and deletes the file. Paste the function into your
terminal as it is: it has no value in it.

```powershell
function Set-DemoSecret {
    param(
        [Parameter(Mandatory)][string]$Name,
        [string]$FromFile
    )
    if ($FromFile) {
        aws secretsmanager put-secret-value --secret-id $Name --secret-string "file://$FromFile" --query VersionId --output text
        return
    }
    $secure = Read-Host -Prompt "Value for $Name" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    $tmp = Join-Path $env:LOCALAPPDATA ("demo-secret-" + [guid]::NewGuid().ToString('N') + ".tmp")
    try {
        [IO.File]::WriteAllText($tmp, [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr))
        aws secretsmanager put-secret-value --secret-id $Name --secret-string "file://$tmp" --query VersionId --output text
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        if (Test-Path $tmp) { Remove-Item $tmp -Force }
    }
}
```

Consent: *"load the five secrets in account A"*. Then, one by one (the names are the output `secret_names`; they are
`cicd-doctor/github-app-private-key`, `github-webhook-secret`, `openai-api-key`, `langfuse-public-key` and
`langfuse-secret-key`):

```powershell
Set-DemoSecret -Name 'cicd-doctor/github-app-private-key' -FromFile 'C:\PATH\TO\prod-app.private-key.pem'
Set-DemoSecret -Name 'cicd-doctor/github-webhook-secret'
Set-DemoSecret -Name 'cicd-doctor/openai-api-key'
Set-DemoSecret -Name 'cicd-doctor/langfuse-public-key'
Set-DemoSecret -Name 'cicd-doctor/langfuse-secret-key'
```

Each call prints a version id (not the value). Check that each secret has a current version without reading it
(READ-ONLY; **never** `get-secret-value`):

```powershell
foreach ($name in 'github-app-private-key', 'github-webhook-secret', 'openai-api-key', 'langfuse-public-key', 'langfuse-secret-key') {
    aws secretsmanager describe-secret --secret-id "cicd-doctor/$name" --query "{name:Name,changed:LastChangedDate,stages:VersionIdsToStages}" --output json
}
```

Each must show a version with the stage `AWSCURRENT`. A task whose secret has no value does not start (D-122). Delete the
`.pem` from the folder you downloaded it to when you are done, or keep it where only you can read it.

### 6. Repository variable for the CD (WRITES, GitHub)

Not a secret (D-128). Consent: *"set AWS_DEPLOY_ROLE_ARN on OWNER/cicd-doctor"*.

```powershell
$role = terraform -chdir="$TF/persistent" output -raw deploy_role_arn
gh variable set AWS_DEPLOY_ROLE_ARN --repo $ME --body $role
gh variable list --repo $ME
gh secret list --repo $ME
```

The last command is READ-ONLY and is box 4's "no long-lived key" evidence: no `AWS_*` secret may be listed.

### 7. The first image, from the CD (WRITES, a few Actions minutes)

The ephemeral layer needs an image tag that exists. The CD pushes it and, with no cluster, ends green with a notice
(D-128). This is also the first real test of the OIDC `sub` and of the "stack is down" path. It must run **from `main`**:
the role trusts nothing else. Consent: *"dispatch cd.yml on main of OWNER/cicd-doctor"*.

```powershell
gh workflow run cd.yml --repo $ME --ref main
Start-Sleep -Seconds 10
$cdRun = gh run list --repo $ME --workflow cd.yml --limit 1 --json databaseId --jq '.[0].databaseId'
gh run watch $cdRun --repo $ME
$sha = git rev-parse origin/main
aws ecr describe-images --repository-name cicd-doctor --query "imageDetails[].{tags:imageTags,bytes:imageSizeInBytes,pushed:imagePushedAt}" --output json
```

Criterion: the run is green and its log has the notice "The cluster cicd-doctor does not exist"; ECR lists one image
tagged with the **full** commit sha of `main`. `$sha` is the `image_tag` of the next step (check that it is the tag
shown, not a newer `main`). If it fails with `Could not assume role` or `Not authorized to perform
sts:AssumeRoleWithWebIdentity`: the `sub` of the real token is not `repo:OWNER@OWNER-ID/cicd-doctor@REPO-ID:ref:refs/heads/main`
(the immutable subject: `gh api repos/OWNER/cicd-doctor/actions/oidc/customization/sub` shows the prefix, D-130); stop
and read the claim in the log.

### 8. Ephemeral layer, services off (SPENDS)

Write `$TF/ephemeral/terraform.tfvars` by hand (git-ignored; no secrets; the prices are numbers as text):

```hcl
image_tag                 = "FULL-COMMIT-SHA-OF-STEP-7"
github_app_id             = "PROD-APP-ID"
openai_model              = "gpt-6-luna"
llm_price_input_per_mtok  = "PRICE-IN"
llm_price_output_per_mtok = "PRICE-OUT"
langfuse_host             = "https://us.cloud.langfuse.com"
# services_enabled stays false (the default) for now
```

**Plan (READ-ONLY).** Consent: *"plan ephemeral in account A"*.

```powershell
terraform -chdir="$TF/ephemeral" init -backend-config=backend.hcl
terraform -chdir="$TF/ephemeral" plan -out=ephemeral.tfplan
```

Read it: VPC and no NAT, RDS `db.t4g.micro`, ALB, CloudFront, the cluster, two services at zero tasks, the migration task
definition, log groups.

**Apply (SPENDS, the clock starts).** Consent: *"apply ephemeral in account A, us-east-2, services off, time box 4 h"*.
The bill starts with RDS, the ALB and the public addresses (about USD 0.06 an hour before the tasks run). Note the time:

```powershell
$UP_AT = Get-Date
$UP_AT.ToUniversalTime().ToString('o')
terraform -chdir="$TF/ephemeral" apply ephemeral.tfplan
terraform -chdir="$TF/ephemeral" output
```

Write the UTC time in the cost table. Expect 10 to 20 minutes (RDS and CloudFront; not measured). Do **not** destroy or
recreate the stack from here to step 16: the CloudFront domain would change (D-124).

### 9. The migration (SPENDS, cents)

By hand the first time (D-123), exactly the block of 3d above. It proves `CREATE EXTENSION vector` on RDS (migration
`0001`, run as the master user, D-121):

```powershell
$cluster = terraform -chdir="$TF/ephemeral" output -raw cluster_name
$net = terraform -chdir="$TF/ephemeral" output -raw migrate_network_configuration
$task = aws ecs run-task --cluster $cluster --task-definition cicd-doctor-migrate --launch-type FARGATE --network-configuration $net --query 'tasks[0].taskArn' --output text
aws ecs wait tasks-stopped --cluster $cluster --tasks $task
aws ecs describe-tasks --cluster $cluster --tasks $task --query 'tasks[0].{exit:containers[0].exitCode,stopCode:stopCode,reason:stoppedReason}' --output json
aws logs tail /cicd-doctor/migrate --since 30m
```

Consent: *"run the migration task in account A"*. Criterion: `exit` is **0** and the log shows the `Running upgrade` lines
up to the last revision (`0006`). Anything else: do not continue; read the log (a `password authentication failed` points
at the RDS secret; a task stuck in `PROVISIONING` or `STOPPED` with `ResourceInitializationError` at a secret with no
value or a security group).

### 10. Services on (SPENDS, the full rate)

Edit `terraform.tfvars` by hand: `services_enabled = true`. Consent: *"apply ephemeral with services on, same time box"*.

```powershell
terraform -chdir="$TF/ephemeral" plan -out=ephemeral2.tfplan
terraform -chdir="$TF/ephemeral" apply ephemeral2.tfplan
$domain = terraform -chdir="$TF/ephemeral" output -raw cloudfront_domain
$hook = terraform -chdir="$TF/ephemeral" output -raw webhook_url
aws ecs wait services-stable --cluster $cluster --services cicd-doctor-api cicd-doctor-worker
aws ecs describe-services --cluster $cluster --services cicd-doctor-api cicd-doctor-worker --query "services[].{name:serviceName,running:runningCount,desired:desiredCount,td:taskDefinition}" --output json
```

Then the load balancer's view of the API task:

```powershell
$tg = aws elbv2 describe-target-groups --query "TargetGroups[0].TargetGroupArn" --output text
aws elbv2 describe-target-health --target-group-arn $tg --query "TargetHealthDescriptions[].TargetHealth.State" --output text
```

Criterion: one running task per service and the target `healthy`. If the tasks cycle or never reach `RUNNING`, look at
`aws ecs describe-services ... --query "services[].events[:5].message"` and at `/cicd-doctor/api` and `/worker` before
anything else; the usual cause is a secret without a value.

### 11. The second `plan` and the entry through CloudFront (READ-ONLY)

Box 2 wants a second `plan` with no changes. Do it **before** the CD touches the task definitions, and again after step 15:

```powershell
terraform -chdir="$TF/ephemeral" plan -detailed-exitcode
"exit code: $LASTEXITCODE"
terraform -chdir="$TF/persistent" plan -detailed-exitcode
"exit code: $LASTEXITCODE"
```

`0` is "no changes", `2` is changes (write what the plan shows), `1` is an error. Then the public entry, from your
machine, through CloudFront (D-120). Two probes tell the app from the load balancer: the app answers JSON, the ALB's
default action is a plain `forbidden`:

```powershell
Invoke-RestMethod "https://$domain/health"
Invoke-RestMethod "https://$domain/ready"
function Invoke-Probe {
    param([string]$Url, [string]$Body, [hashtable]$Headers = @{})
    try {
        $r = Invoke-WebRequest -Uri $Url -Method Post -Body $Body -ContentType 'application/json' -Headers $Headers -UseBasicParsing
        "{0} {1}" -f [int]$r.StatusCode, $r.Content
    }
    catch {
        $resp = $_.Exception.Response
        if ($null -eq $resp) { throw }
        $reader = New-Object IO.StreamReader($resp.GetResponseStream())
        "{0} {1}" -f [int]$resp.StatusCode, $reader.ReadToEnd()
    }
}
Invoke-Probe -Url $hook -Body '{"zen":"probe"}' -Headers @{ 'X-GitHub-Event' = 'ping'; 'X-GitHub-Delivery' = '00000000-0000-4000-8000-000000000000'; 'X-Hub-Signature-256' = 'sha256=0000000000000000000000000000000000000000000000000000000000000000' }
Invoke-Probe -Url $hook -Body '{"zen":"probe"}'
```

On Windows PowerShell 5.1 `Invoke-Probe` can print the status with an empty body; `curl.exe -i` shows it.

Criterion: `/health` is `{"status":"ok"}`; `/ready` is `ready`; both probes answer **401** with
`{"detail":"invalid signature"}` (the body is the app's; a `403 forbidden` would be the ALB's Host rule saying the Host
CloudFront sends is not the distribution's own, D-120). The probes carry no secret and no real delivery id.

### 12. The webhook URL of the production app (BY HAND)

You, in the app's settings: set the webhook URL to the value printed by `$hook` (`https://<domain>.cloudfront.net/webhooks/github`) and
save. The secret stays the one of step 5. The app stays installed only on the lab (installing it on `cicd-doctor` for the optional box 6 of P1c is your call). The
first real delivery is the `workflow_run` of step 13; to see a 2xx earlier, "Redeliver" an old delivery. In "Advanced > Recent deliveries" write the delivery id, the status
and the time; a **2xx** is the criterion. "Redeliver" one delivery to see the same answer again (the second is `200`,
a duplicate). A real delivery whose signature GitHub computed over the original body and which the app accepts is
the proof that CloudFront passes the **POST body and the signature header intact**; a `401` on a real delivery would mean
it does not.

### 13. Boxes 5 and 6: the pull request of the acceptance flow, with a planted fake token (SPENDS, a few cents of OpenAI)

Precondition 6 of step 0 holds (no local worker, no dev app on the lab) and so does precondition 11 (the lab has no
`.github/cicd-doctor.yml`, or the model would not be called and there would be no trace). One pull request does two jobs:
it is the **box 1 pull request of "Live verification (P1c)"** in `lab/README.md` (workflow `pr-py-modnf.yml`; nothing is
invented), and it carries the planted token of box 6.

**The planted token.** The lab has no workflow that prints a secret and this runbook adds none. The pull request edits
the file the existing `pr-py-modnf` workflow already runs, so its **log** carries a fake GitHub token **before** the
failure. The token is assembled at run time, so no secret-shaped literal is committed (D-59), and it is fake (`ghp_`
and 36 letters). Check the prompt before the `git commit`: the path must be the lab clone, not `cicd-doctor` (the P1c
warning). Consent: *"open a lab pull request with a fake token in its log"*.

```powershell
$startDir = Get-Location
$cloneDir = Join-Path $env:TEMP 'lab-p5'
gh repo clone $LAB $cloneDir
Set-Location $cloneDir
git remote get-url origin
git switch -c p5/fake-token
$file = 'python/src/labpkg/cli.py'
$text = Get-Content -Raw $file
$plant = 'print("debug " + "ghp_" + "FAKE" * 9)' + "`n"
Set-Content -Path $file -Value ($plant + $text) -Encoding ascii -NoNewline
git commit -am "p5: plant a fake token in the log"
git push -u origin p5/fake-token
gh pr create --repo $LAB --base main --head p5/fake-token --title "P5 fake token" --body "Live check of P5: a fake token in the log"
$PR  = gh pr view p5/fake-token --repo $LAB --json number --jq .number
$SHA = git rev-parse HEAD
Start-Sleep -Seconds 10
$RUN = gh run list --repo $LAB --branch p5/fake-token --workflow pr-py-modnf.yml --limit 1 --json databaseId --jq '.[0].databaseId'
gh run watch $RUN --repo $LAB
Set-Location $startDir
```

The names `$PR`, `$SHA` and `$RUN` are the ones of the P1c blocks. Now run, unchanged, the **Verification** block of
P1c box 1 and its "keep the ids" block (with `$BOT` the production slug): the check `CICD Doctor: pr-py-modnf`,
`neutral`, the seconds from the end of the run to `completed_at` **90 or less**, exactly **1** comment of the bot.
Then P1c box 2 (**Action** `gh run rerun $RUN --repo $LAB --failed`, then its Verification): the same check id and the
same comment id, the output changed. Boxes 4 and 5 of P1c (an invalid key, a budget of zero) are **not** repeated: they
would need an edit of the task environment and a restart, and the offline tests cover them. Box 6 (the broken test on this
repository) is optional and needs the app installed there. The lab also runs `pr-flaky` (fails in attempt 1) on this pull
request: its diagnosis is another model call; the trace of step 14 is the one of `pr-py-modnf`.

Check that the log of the run holds the fake token (READ-ONLY), otherwise step 14 proves nothing:

```powershell
gh run view $RUN --repo $LAB --log | Select-String 'ghp_FAKE'
```

What the live run taught about this step:

- GitHub masks a token of its own format (`ghp_...`) before the app sees the log, so that planted token
  proves nothing about the redactor. Use a fake AWS-shaped key, assembled at run time (D-59), in a
  second pull request.
- Put it **inside the error line** (`raise RuntimeError("upload failed with key " + "AKIA" + "FAKE" * 4)`):
  the Langfuse input is cut at 4,000 characters, but the model quotes the error line, so the bot's comment
  and the trace output show `[REDACTED:aws_access_key_id]`.
- The failure must be new: a signature already diagnosed is answered by the cache, with no model call and
  no trace.
- A pull request gets one bot comment per failing workflow, so two here (`pr-py-modnf` and `pr-flaky`).
- In PowerShell 5.1 a quoted CloudWatch filter pattern needs its quotes escaped, or it matches nothing.

### 14. Box 6: CloudWatch by `delivery_id`, and Langfuse with the planted fake token (READ-ONLY)

**CloudWatch (READ-ONLY).** Find the delivery id of this run, then follow it through api and worker with Logs Insights
(the console, region `us-east-2`, log groups `/cicd-doctor/api` and `/cicd-doctor/worker`, last 1 hour). First query:

```
fields @timestamp, level, message, delivery_id, github_event, run_id, repository
| filter github_event = "workflow_run.completed" and run_id = RUN-ID-OF-STEP-13
| sort @timestamp asc
```

Then, with the `delivery_id` it returns (the same id is on GitHub's delivery page):

```
fields @timestamp, @log, level, logger, message, job_id, job_kind, run_id, installation_id, repository
| filter delivery_id = "DELIVERY-ID"
| sort @timestamp asc
```

Criterion: JSON objects (the console shows the fields), lines from `api` and from `worker`, correlated by `delivery_id`
through the webhook line, the `diagnose_run` job and the publication, with `job_id`, `run_id`, `installation_id` and
`repository` on the worker lines. From the CLI (also READ-ONLY):

```powershell
aws logs filter-log-events --log-group-name /cicd-doctor/worker --filter-pattern '{ $.delivery_id = "DELIVERY-ID" }' --max-items 20 --query "events[].message" --output text
aws logs filter-log-events --log-group-name /cicd-doctor/worker --filter-pattern 'ghp_FAKE' --query "events[].message" --output text
aws logs filter-log-events --log-group-name /cicd-doctor/api --filter-pattern 'ghp_FAKE' --query "events[].message" --output text
```

The first prints the lines; the other two must print **nothing**. Notes: a rejected signature is logged by design with
no `delivery_id` (nothing from an unverified request is bound, D-117), so the two probes of step 11 are not
correlated; the logs of the tasks start with the task, so a line from before the first request is not a failure.

**Langfuse (the UI, READ-ONLY).** In the project (the traces of the last hour): one trace per model call (a repair is a
second trace; a cache hit, a fallback or a budget refusal makes none), named for the diagnosis, with the **model**, the
**prompt version `diag-v3`**, the **tokens**, the **cost** and the **latency**, the session equal to the `delivery_id`,
and metadata with `repository`, `run_id` and `installation_id`. Then search the input and output of that trace for
`ghp_FAKE` and for `FAKEFAKE`: it must find **nothing**, and the input must show the redaction placeholder where the
token was in the log. If the input shows **neither** the token nor a placeholder, the planted line did not reach the
excerpt and the check proves nothing: plant it nearer the failure and repeat. A trace may take a minute to appear (the SDK batches in the background). If the dashboard shows no trace
at all, check `/cicd-doctor/worker` for one WARNING from the tracer (type name only, once per process) before blaming
anything else; and remember that a second pull request with the **same** failure is answered by the cache and has no
trace (D-81): only the first run on a fresh database proves the trace.

### 15. Box 4: a merge to `main` deploys (WRITES, SPENDS a few Actions minutes)

With the stack up. Open a one-line documentation pull request to `cicd-doctor` and merge it (you do both; Claude does
neither). Consent: *"merge a docs PR to main of OWNER/cicd-doctor while the stack is up"*.

```powershell
Start-Sleep -Seconds 15
$cdId = gh run list --repo $ME --workflow cd.yml --event push --limit 1 --json databaseId --jq '.[0].databaseId'
gh run watch $cdId --repo $ME
gh run view $cdId --repo $ME --log | Select-String 'Migration|exit code|Deployed|task definition|arn:aws:ecs'
aws ecs describe-services --cluster cicd-doctor --services cicd-doctor-api cicd-doctor-worker --query "services[].{name:serviceName,td:taskDefinition,running:runningCount,rollout:deployments[0].rolloutState}" --output json
Invoke-RestMethod "https://$domain/ready"
terraform -chdir="$TF/ephemeral" plan -detailed-exitcode
"exit code: $LASTEXITCODE"
```

Criterion: the push run is green; its log shows three new task definition revisions, `Migration finished with exit code
0` **before** the services were updated; each service runs the new revision (the revision number went up) and
`COMPLETED`; `/ready` is still `ready`; no access key anywhere (step 6's `gh secret list`). The last `plan`
is the **unverified** point of D-123: the services ignore the task definition, but the migration task definition resource
may show the new revision as a difference. Write exactly what it prints. Then, if the permissions are short, the first
failure of the run is an `AccessDenied` naming the action: write it and stop (the role's policy is `persistent/oidc.tf`;
changing it is a code change, not a console edit).

### 16. Copy out, then `down` (SPENDS stops)

First copy what you want to keep (the logs and the database go with the stack): the Logs Insights results, the Langfuse
trace screenshot, the GitHub delivery ids and the ECS events.

```powershell
aws logs filter-log-events --log-group-name /cicd-doctor/worker --start-time ([DateTimeOffset]$UP_AT).ToUnixTimeMilliseconds() --query "events[].message" --output text | Out-File -Encoding utf8 "$env:TEMP\p5-worker-log.txt"
aws logs filter-log-events --log-group-name /cicd-doctor/api --start-time ([DateTimeOffset]$UP_AT).ToUnixTimeMilliseconds() --query "events[].message" --output text | Out-File -Encoding utf8 "$env:TEMP\p5-api-log.txt"
```

They hold redacted log lines, not secrets, and sit in your temp folder: keep them out of the repository. In the production app,
deactivate the webhook (or leave it knowing deliveries will fail). Then, consent *"destroy ephemeral in account A"*:

```powershell
$DOWN_AT = Get-Date
terraform -chdir="$TF/ephemeral" destroy
$DOWN_AT.ToUniversalTime().ToString('o')
"hours up: {0:N2}" -f (((Get-Date) - $UP_AT).TotalHours)
```

CloudFront is disabled before it is deleted, so `destroy` waits for several minutes. Never destroy `persistent` here.
Then go through the table *After `down`* above, plus these (READ-ONLY):

```powershell
aws logs describe-log-groups --log-group-name-prefix /cicd-doctor --query "logGroups[].logGroupName" --output text
aws secretsmanager list-secrets --include-planned-deletion --query "SecretList[].{name:Name,deleted:DeletedDate}" --output json
aws ecr describe-images --repository-name cicd-doctor --query "length(imageDetails)"
aws ecs list-clusters --query "clusterArns" --output text
aws rds describe-db-instances --query "DBInstances[].DBInstanceIdentifier" --output text
aws elbv2 describe-load-balancers --query "LoadBalancers[].LoadBalancerArn" --output text
aws ec2 describe-nat-gateways --filter Name=state,Values=available,pending --query "NatGateways[].NatGatewayId" --output text
aws ec2 describe-addresses --query "Addresses[].PublicIp" --output text
aws cloudfront list-distributions --query "DistributionList.Items[].Id" --output text
```

What may remain: the **persistent** layer (the five secrets, the ECR repository with its images, the OIDC provider and
the roles) and the state bucket. What may not: log groups `/cicd-doctor/api|worker|migrate`, the cluster, RDS (and its
managed secret), ALB, CloudFront, public IPv4 addresses, NAT gateways, a VPC. The log groups are in the ephemeral layer,
so they are gone with it. The secret of the RDS password is deleted with the instance; if `list-secrets` shows it with a
`deleted` date, it is in its recovery window and does not bill after the window.

The next day, **Billing and Cost Management > Cost Explorer** (data arrives with a delay of up to 24 hours; hourly
granularity must be enabled in its settings, otherwise the day is the unit): group by Service, the day or days of the
session, and write the real numbers in the cost table.

### Cost: estimate against what Cost Explorer shows

The estimates are the ones of the *Cost* section above (not measured). Fill the last two columns after the 24 h delay.

| Item | Estimate USD per hour | Hours up | Estimate total | Real (Cost Explorer, by service) | Real minus estimate |
|---|---|---|---|---|---|
| ALB | 0.0305 | | | | |
| RDS `db.t4g.micro` + 20 GB gp3 | 0.019 | | | | |
| Fargate api (0.25 vCPU / 0.5 GB) | 0.012 | | | | |
| Fargate worker (0.5 vCPU / 1 GB) | 0.025 | | | | |
| Public IPv4 (ALB 2, tasks 2) | 0.020 | | | | |
| CloudFront, CloudWatch Logs, data transfer | about 0 | | | | |
| Migration task(s), a minute each | a fraction of a cent | | | | |
| Secrets Manager (5 secrets + the RDS one while it exists) | 2.00 to 2.40 a month | | | | |
| ECR, S3 state bucket | about 0.05 a month | | | | |
| OpenAI (real calls of steps 13 and 14) | not estimated | | | | |
| **Total** | **about 0.11** | | | | |

Time up: `$UP_AT` (UTC) ____ to `$DOWN_AT` (UTC) ____, hours ____. Also write the **AWS Budgets** figure that day.

### Stop conditions

Stop (do not retry, do not work around) and tell the owner when:

- A `Create*` call fails with **"explicit deny in a service control policy"**. The organization may attach
  `BudgetsSpendLimitDenyNewWorkloads` by itself when the forecast nears its USD 20 limit; it denies creating RDS, ECS
  services, ALBs, CloudFront, Secrets Manager secrets and ECR repositories. **Nobody here edits, detaches or works around an
  SCP.** Note which call and which step, run `terraform plan` to see what is left, and decide with the owner whether to
  destroy what exists.
- A budget alert arrives, or Cost Explorer shows more than the estimate by a factor you cannot explain.
- The ephemeral `apply` has not finished after 45 minutes, a task has not reached `RUNNING` after 15 or the target is not
  `healthy` after 10 (the likely causes are in steps 9 and 10).
- Credentials expired in the middle of an `apply` or a `destroy`: refresh them (step 0) and re-run the **same**
  command; do not delete state.
- The state is locked (`Error acquiring the state lock`) and no Terraform process of yours is running: only the owner
  decides on `terraform force-unlock`.
- Anything asks for a secret value in the chat, or a command would print one.

### Abort and clean destroy

At any point, the clean way out is: copy out what you want (step 16), `terraform -chdir=$TF/ephemeral destroy`, and the
table *After `down`*. A destroy of a half-built stack works: RDS has no deletion protection and no final snapshot, and
the ECR repository is in the other layer. Known pitfalls:

- **Secrets Manager recovery window and name reuse.** The five secrets are in `persistent` and survive. If you destroy
  `persistent` for good, their names stay reserved for `secret_recovery_window_days` (7): set it to `0`, apply, then
  destroy to reuse a name at once; a deleted secret inside the window can be restored (`aws secretsmanager
  restore-secret --secret-id ...`). The RDS-managed secret of the password goes with the instance.
- **The RDS password rotates every 7 days** and a task reads it only when it starts. A stack kept up near a week loses its
  connection: restart the tasks, or destroy. Not a concern for a session of hours.
- **The CD after a `down`:** a push to `main` pushes the image and ends green with a notice. If a destroy is interrupted and
  leaves the cluster with services, the CD would try to roll them out against a missing database: finish the destroy.
- **CloudFront** takes minutes to disable and delete; an interrupted `destroy` is re-run, not worked around.
- **A leftover** (a network interface, a security group) after a failed destroy: re-run `destroy`; read the error; never
  delete by hand in the console while Terraform still tracks it, unless the owner decides to.

### What only you do

Claude will not do these even if asked (the rules of this repository and of the session):

| Yours | Why |
|---|---|
| MFA on the root user, the `aws login` and every browser login | authentication is the owner's |
| Creating the production GitHub App, its webhook secret, its private key, installing it | creating an identity and granting access |
| The five secret values (step 5) | a value in the chat, a file or a command line is a leak |
| Every `terraform plan`, `apply` and `destroy` and every `aws` command of this runbook | they create, change or charge resources; the consent sentence is the gate |
| Setting a repository variable, merging a pull request, editing the app's webhook URL, protecting `main` | settings and public content |
| Reading Cost Explorer and the SCP situation in the console, and deciding about an SCP | the account's policies are not ours to change |
| Handing the results back (ids, times, rows, screenshots) | the evidence is what was observed |

### The evidence (paste what you observed)

A box of SPEC §13 P5 is ticked **only** from rows with real ids, times and outputs; "not recorded" means it was not
captured. Leave the result column empty until you have it.

| # | Box | What to observe | Command or place | Result |
|---|---|---|---|---|
| 1 | 1 | `docker compose up` ready, time, user id 10001, image size | step 2 | 2026-11-25: ready in 39 s (build included), uid 10001, 790 MB, api healthy, worker running (0 restarts), migrate ok |
| 2 | 2 | `apply` summary of bootstrap, persistent and ephemeral (resources added) | steps 3, 4, 8, 10 | bootstrap 7 added; persistent 14 added, later 1 changed (D-130); ephemeral 44 added, 06:31:20 to 06:37:23 UTC; services on 06:43:53 |
| 3 | 2 | The second `plan -detailed-exitcode` of ephemeral and persistent: exit code 0 and "No changes" | step 11 | exit code 0, "No changes", both layers, before and after the CD deploy |
| 4 | 2 | ECS: both services at 1 running task; target `healthy` | step 10 | api and worker 1 of 1, target healthy 06:45:17 |
| 5 | 2 | RDS PostgreSQL 17 reachable: the migration exit code 0 and the last revision in its log (`CREATE EXTENSION vector` ran in 0001) | step 9 | task `8e4c9a9a1cbe4f5ba60a8680ac15dd32` exit 0, revisions 0001 to 0006 |
| 6 | 2 | The five secrets with a current version (stage `AWSCURRENT`), no value read | step 5 | five secrets `AWSCURRENT`; no value read |
| 7 | 2 | Execution and task roles, the OIDC provider and the deploy role exist; no access key in the account for the CD | `aws iam get-role --role-name cicd-doctor-deploy --query Role.Arn`; step 6 | execution and task roles, OIDC provider and `cicd-doctor-deploy` created; the repository has no Actions secret |
| 8 | 3 | **Dropped by D-129:** nothing to observe | n/a | dropped |
| 9 | 4 | A push-to-`main` run of `cd.yml`: id, green, the OIDC token accepted | step 15 | run 45569906244 (push `d6973c25`), green, OIDC accepted |
| 10 | 4 | The migration task's exit code 0 before the services were updated; new task definition revisions; services `COMPLETED` | step 15 | revision 2 of the three task definitions; migration `6748b4afaa2245479f59ffad4037705c` exit 0 before the rollout; `COMPLETED`; the later plan showed no changes |
| 11 | 4 | No long-lived AWS key: `gh secret list` shows no `AWS_*` | step 6 | `gh secret list` empty; only the variable `AWS_DEPLOY_ROLE_ARN` |
| 12 | 4 | The stack-down path: the first run (dispatch) ended green with the notice | step 7 | first tries failed (45559287242 OIDC subject, 45560513695 `bash -e`, D-130); after the fix run 45573427741 was green with the "not both active" notice; the missing-cluster branch only in a local simulation |
| 13 | 5 | The prod app webhook URL is the CloudFront one; deliveries 2xx: ids, times, statuses | step 12 | redelivery `659700a2-e86b-11f1-8080-71a309a77212`, 202, 06:56:39 |
| 14 | 5 | `/health` and `/ready` through CloudFront; bad or missing signature answers 401 with the app's JSON | step 11 | `/health` ok, `/ready` ready; bad and missing signature 401 with the app's JSON; the ALB directly times out |
| 15 | 5 | The POST body and signature header arrive intact: a real delivery accepted | step 12 | the real delivery of row 13 was accepted (202) |
| 16 | 5 | P1c box 1 against prod: check run id, status `completed` / `neutral`, comment id, seconds to the check | step 13 | lab PR #10, run 45564977311: one check `completed` / `neutral` in 11 s and comment 7155419086 (check id in SPEC §13 P5) |
| 17 | 5 | P1c box 2 against prod: the same check id and comment id after "Re-run failed jobs" | step 13 | same check and same comment, edited to "Attempt 2" |
| 18 | 6 | CloudWatch: one `delivery_id` followed from the webhook to the publication, JSON, with `job_id`, `run_id`, `installation_id`, `repository` | step 14 | delivery `8d7d042a-e89f-11f1-9119-baab97877528`: `webhook enqueued` (api) to `job done` (worker), with the four ids |
| 19 | 6 | The planted fake token is in the Actions log, and **not** in CloudWatch (0 lines) | step 14 | run 45568512559: the fake AWS key in clear text in the Actions log, 0 lines in the api and worker log groups |
| 20 | 6 | Langfuse: one trace per call with model, `diag-v3`, tokens, cost, latency; no `ghp_FAKE` in input or output | step 14 | traces with `gpt-6-luna`, `diag-v3`, tokens, cost, latency; the output of trace `1eb70bf4ae89ed693bbe75a147d0b7d2` is redacted; input cut at 4,000 characters; a whole-trace text search was not recorded |
| 21 | all | After `down`: every line of *After `down`* shows zero; the persistent layer remains | step 16 | destroy 07:55:09 to 08:02:50, 44 destroyed; every check empty; five secrets and the ECR images remain |
| 22 | all | Cost: hours up and the Cost Explorer total against about USD 0.11 an hour | cost table | up about 1 h 31 min (about USD 0.17 by the estimate); Cost Explorer not recorded |
| 23 | all | Any SCP deny, expired credential, unverified point of D-120, D-121 or D-123 that showed itself (ALB Host rule, `ClusterNotFoundException`, `RunTask` under the policy, the pgvector version) | the step where it showed | no SCP deny, no expired credential; the ALB Host rule matched; the plan drift of D-123 did not appear; two CD defects fixed by D-130; `ClusterNotFoundException` not observed live |
