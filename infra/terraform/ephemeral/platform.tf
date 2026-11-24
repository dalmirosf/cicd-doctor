# What the services of PR e attach to: the cluster, the log groups and the pieces of the persistent
# layer, found by well-known names (no terraform_remote_state, D-125). If the persistent layer is not
# applied these lookups fail at plan time with "not found", which is the message we want.

data "aws_ecr_repository" "app" {
  name = var.name_prefix
}

data "aws_iam_role" "execution" {
  name = "${var.name_prefix}-ecs-execution"
}

data "aws_iam_role" "api_task" {
  name = "${var.name_prefix}-api-task"
}

data "aws_iam_role" "worker_task" {
  name = "${var.name_prefix}-worker-task"
}

# One group per process under /<name_prefix>/, the only place the execution role may write.
resource "aws_cloudwatch_log_group" "app" {
  for_each = var.log_streams

  name              = "/${var.name_prefix}/${each.key}"
  retention_in_days = var.log_retention_days
}

# Container Insights stays off: it bills per metric and per log byte.
resource "aws_ecs_cluster" "main" {
  name = var.name_prefix

  setting {
    name  = "containerInsights"
    value = "disabled"
  }
}
