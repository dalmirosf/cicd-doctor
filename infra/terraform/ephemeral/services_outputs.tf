output "webhook_url" {
  description = "Paste this as the webhook URL of the production GitHub App (once per `up`: it changes when the stack is recreated)."
  value       = "https://${aws_cloudfront_distribution.main.domain_name}/webhooks/github"
}

output "cloudfront_domain" {
  value = aws_cloudfront_distribution.main.domain_name
}

output "alb_dns_name" {
  value = aws_lb.main.dns_name
}

output "service_names" {
  value = {
    api    = aws_ecs_service.api.name
    worker = aws_ecs_service.worker.name
  }
}

output "migrate_task_family" {
  description = "`aws ecs run-task --task-definition` takes this family (its latest revision)."
  value       = aws_ecs_task_definition.migrate.family
}

output "migrate_network_configuration" {
  description = "--network-configuration of `aws ecs run-task` for the migration (public subnets, worker security group, public IP)."
  value       = "awsvpcConfiguration={subnets=[${join(",", aws_subnet.public[*].id)}],securityGroups=[${aws_security_group.worker.id}],assignPublicIp=ENABLED}"
}
