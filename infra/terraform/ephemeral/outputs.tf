# The names PR e (services, ALB, CloudFront, migration task) attaches to.

output "vpc_id" {
  value = aws_vpc.main.id
}

output "public_subnet_ids" {
  description = "Subnets of the ALB and of the Fargate tasks (assign_public_ip = true)."
  value       = aws_subnet.public[*].id
}

output "private_subnet_ids" {
  value = aws_subnet.private[*].id
}

output "security_group_ids" {
  value = {
    alb    = aws_security_group.alb.id
    api    = aws_security_group.api.id
    worker = aws_security_group.worker.id # also for the migration task
    db     = aws_security_group.db.id
  }
}

output "cluster_name" {
  value = aws_ecs_cluster.main.name
}

output "cluster_arn" {
  value = aws_ecs_cluster.main.arn
}

output "log_group_names" {
  value = { for k, g in aws_cloudwatch_log_group.app : k => g.name }
}

output "ecr_repository_url" {
  value = data.aws_ecr_repository.app.repository_url
}

output "execution_role_arn" {
  value = data.aws_iam_role.execution.arn
}

output "api_task_role_arn" {
  value = data.aws_iam_role.api_task.arn
}

output "worker_task_role_arn" {
  value = data.aws_iam_role.worker_task.arn
}

output "db_host" {
  description = "DB_HOST of the containers."
  value       = aws_db_instance.main.address
}

output "db_port" {
  value = aws_db_instance.main.port
}

output "db_name" {
  value = aws_db_instance.main.db_name
}

output "db_username" {
  value = aws_db_instance.main.username
}

output "db_master_secret_arn" {
  description = "RDS-managed secret. In an ECS `secrets` entry the password is \"<this arn>:password::\" (D-121)."
  value       = aws_db_instance.main.master_user_secret[0].secret_arn
}
