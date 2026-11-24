output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}

output "ecr_repository_arn" {
  value = aws_ecr_repository.app.arn
}

output "execution_role_arn" {
  value = aws_iam_role.execution.arn
}

output "api_task_role_arn" {
  value = aws_iam_role.api_task.arn
}

output "worker_task_role_arn" {
  value = aws_iam_role.worker_task.arn
}

output "secret_arns" {
  description = "Environment variable name -> secret ARN, for the ECS `secrets` field (PR e). ARNs are not secret."
  value       = { for k, s in local.secrets : s.env_var => aws_secretsmanager_secret.app[k].arn }
}

output "secret_names" {
  description = "Where the owner loads each value by hand (aws secretsmanager put-secret-value)."
  value       = { for k, s in local.secrets : s.env_var => aws_secretsmanager_secret.app[k].name }
}

output "log_group_prefix" {
  description = "The ephemeral layer creates its log groups under this prefix; the execution role may write only there."
  value       = local.log_group_prefix
}
