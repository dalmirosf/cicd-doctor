# Terraform on AWS (P5)

Three independent root modules, applied in this order. Nothing in this folder has been applied yet:
the first `apply` is the live verification (PR h), with the owner's consent. Decisions: D-121 (network
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

MFA on the root user; an admin user with IAM Identity Center and `aws sso login` (no long-lived access keys);
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

**3a. Persistent layer (once, then it stays).** `terraform.tfvars` needs `github_owner` (your GitHub login).

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

Owner, once: protect `main` in GitHub (pull request and review required, no direct pushes, no force pushes). The deploy role is trusted for every run on `main`, so this is what stands between a push and the application secrets (D-123). `cd.yml` must trigger on `push` to `main` only, and no workflow may combine `pull_request_target` with `id-token: write`.

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
services; read the log. (This needs the `DATABASE_URL` change of D-123 (6): until `Settings` builds the URL from `DB_HOST`
and `DB_PASSWORD`, the migration fails at start with a missing setting.) The CD in PR f does exactly this with the new
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
`DB_USER` and `DB_PASSWORD` (from the RDS secret); the app wants one `DATABASE_URL`, and D-123 (6) records that
`Settings` must build it before the live run.
