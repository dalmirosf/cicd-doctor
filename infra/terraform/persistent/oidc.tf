# GitHub Actions -> AWS without long-lived keys (D-123). Persistent on purpose: the CD must be able
# to push an image and start a deploy after every teardown of the ephemeral layer.
#
# There can be only ONE OpenID Connect provider per URL in an account. If the account already has
# one for token.actions.githubusercontent.com, `apply` fails with EntityAlreadyExists: import it
# (owner) instead of creating a second one.
resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
  # No thumbprint_list: AWS validates the GitHub certificate chain with its own trusted CAs.
}

locals {
  # The immutable subject (GitHub's `use_immutable_subject`, found at the first live run): the names
  # are followed by their numeric ids, so a renamed or re-registered account or repository never
  # matches.
  github_sub = "repo:${var.github_owner}@${var.github_owner_id}/${var.github_repo}@${var.github_repo_id}:ref:refs/heads/${var.github_deploy_branch}"

  # Names fixed by the ephemeral layer (D-125: the layers find each other by name).
  cluster_name    = var.name_prefix
  service_names   = ["${var.name_prefix}-api", "${var.name_prefix}-worker"]
  migrate_family  = "${var.name_prefix}-migrate"
  account_id      = data.aws_caller_identity.current.account_id
  ecs_arn_prefix  = "arn:aws:ecs:${var.aws_region}:${local.account_id}"
  cluster_arn     = "${local.ecs_arn_prefix}:cluster/${local.cluster_name}"
  task_def_prefix = "${local.ecs_arn_prefix}:task-definition"
}

# Trust: who may become the deploy role. Two exact-match conditions, both StringEquals (no
# StringLike, no wildcard):
#   aud = sts.amazonaws.com       the token was minted for AWS, not for another service;
#   sub = repo:<owner>@<owner id>/<repo>@<repo id>:ref:refs/heads/main
#         only a workflow run of THIS repository on THIS branch. A `pull_request` run (sub
#         `...:pull_request`, even from a fork), a tag, another branch or an `environment:` job carries
#         a different `sub` and is refused. A `pull_request_target` run is NOT refused: it runs on the
#         base branch (`...:ref:refs/heads/main`). So no workflow may combine `pull_request_target` with
#         `id-token: write`; the CD triggers on `push` to main only (D-123, guard test).
data "aws_iam_policy_document" "deploy_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = [local.github_sub]
    }
  }
}

resource "aws_iam_role" "deploy" {
  name                 = "${var.name_prefix}-deploy"
  assume_role_policy   = data.aws_iam_policy_document.deploy_trust.json
  max_session_duration = 3600
}

# What `cd.yml` (PR f) does, and nothing else: push the image, register task definitions, run the
# migration task and wait for it, update the two services and wait for them.
data "aws_iam_policy_document" "deploy" {
  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"] # this action does not support resource-level permissions
  }

  statement {
    sid = "EcrPush"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload",
      "ecr:PutImage",
    ]
    resources = [aws_ecr_repository.app.arn]
  }

  # Neither action supports resource-level permissions. A task definition is only a JSON record
  # (it costs nothing and runs nothing), and what it may do is bounded by PassRole below.
  statement {
    sid       = "TaskDefinitions"
    actions   = ["ecs:RegisterTaskDefinition", "ecs:DescribeTaskDefinition"]
    resources = ["*"]
  }

  statement {
    sid       = "RunTheMigrationTask"
    actions   = ["ecs:RunTask"]
    resources = ["${local.task_def_prefix}/${local.migrate_family}:*"]

    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [local.cluster_arn]
    }
  }

  statement {
    sid       = "WatchTheTasksOfTheCluster"
    actions   = ["ecs:DescribeTasks"]
    resources = ["${local.ecs_arn_prefix}:task/${local.cluster_name}/*"]
  }

  statement {
    sid     = "RollOutTheServices"
    actions = ["ecs:UpdateService", "ecs:DescribeServices"]
    resources = [
      for s in local.service_names : "${local.ecs_arn_prefix}:service/${local.cluster_name}/${s}"
    ]
  }

  # To print why a migration failed.
  statement {
    sid       = "ReadTheMigrationLog"
    actions   = ["logs:GetLogEvents"]
    resources = ["${local.log_group_arn}/migrate:log-stream:*"]
  }

  # A task definition names the execution and task roles: registering one and updating a service
  # to it needs PassRole on exactly those three roles, and only toward ECS tasks.
  statement {
    sid     = "PassOnlyTheTaskRoles"
    actions = ["iam:PassRole"]
    resources = [
      aws_iam_role.execution.arn,
      aws_iam_role.api_task.arn,
      aws_iam_role.worker_task.arn,
    ]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "deploy" {
  name   = "deploy"
  role   = aws_iam_role.deploy.id
  policy = data.aws_iam_policy_document.deploy.json
}
