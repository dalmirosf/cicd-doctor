# Security groups (D-121). Everything is closed except:
#   CloudFront -> ALB (listener port)  ->  API (api_port)  ->  RDS 5432   and   worker -> RDS 5432.
# Tasks have public IPs (no NAT), so the "no inbound" rule is carried by these groups alone: the API
# group accepts only the ALB group, the worker group accepts nothing, the DB group only the two.

# Only CloudFront edge servers may open a connection to the ALB (the listener adds a Host check, D-120).
# The managed prefix list counts for its maximum size in the rule quota of this group; the group
# is dedicated to it for that reason.
data "aws_ec2_managed_prefix_list" "cloudfront" {
  name = "com.amazonaws.global.cloudfront.origin-facing"
}

resource "aws_security_group" "alb" {
  name        = "${var.name_prefix}-alb"
  description = "Application Load Balancer: CloudFront in, API out"
  vpc_id      = aws_vpc.main.id
}

resource "aws_security_group" "api" {
  name        = "${var.name_prefix}-api"
  description = "API tasks: the ALB in, the internet out"
  vpc_id      = aws_vpc.main.id
}

resource "aws_security_group" "worker" {
  name        = "${var.name_prefix}-worker"
  description = "Worker tasks (and the migration task): nothing in, the internet out"
  vpc_id      = aws_vpc.main.id
}

resource "aws_security_group" "db" {
  name        = "${var.name_prefix}-db"
  description = "RDS: PostgreSQL from the API and the worker only"
  vpc_id      = aws_vpc.main.id
}

resource "aws_vpc_security_group_ingress_rule" "alb_from_cloudfront" {
  security_group_id = aws_security_group.alb.id
  description       = "CloudFront origin-facing servers"
  prefix_list_id    = data.aws_ec2_managed_prefix_list.cloudfront.id
  ip_protocol       = "tcp"
  from_port         = var.alb_listener_port
  to_port           = var.alb_listener_port
}

resource "aws_vpc_security_group_egress_rule" "alb_to_api" {
  security_group_id            = aws_security_group.alb.id
  description                  = "Forward to the API tasks"
  referenced_security_group_id = aws_security_group.api.id
  ip_protocol                  = "tcp"
  from_port                    = var.api_port
  to_port                      = var.api_port
}

resource "aws_vpc_security_group_ingress_rule" "api_from_alb" {
  security_group_id            = aws_security_group.api.id
  description                  = "From the ALB only"
  referenced_security_group_id = aws_security_group.alb.id
  ip_protocol                  = "tcp"
  from_port                    = var.api_port
  to_port                      = var.api_port
}

# Egress of the tasks: HTTPS to ECR, Secrets Manager, CloudWatch, GitHub, OpenAI and Langfuse, and
# PostgreSQL to the database group. Without a NAT or endpoints the HTTPS goes through the internet
# gateway, so 443 is open to the world; nothing else is. DNS goes to the VPC resolver, which security
# group egress does not filter.
resource "aws_vpc_security_group_egress_rule" "api_https" {
  security_group_id = aws_security_group.api.id
  description       = "HTTPS out"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_egress_rule" "api_to_db" {
  security_group_id            = aws_security_group.api.id
  description                  = "PostgreSQL to RDS"
  referenced_security_group_id = aws_security_group.db.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_vpc_security_group_egress_rule" "worker_https" {
  security_group_id = aws_security_group.worker.id
  description       = "HTTPS out"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_egress_rule" "worker_to_db" {
  security_group_id            = aws_security_group.worker.id
  description                  = "PostgreSQL to RDS"
  referenced_security_group_id = aws_security_group.db.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

# The VPC default security group allows all traffic between its members; nothing here uses it, so
# it is emptied (no rules at all) and nobody can attach it by accident.
resource "aws_default_security_group" "default" {
  vpc_id = aws_vpc.main.id
}

resource "aws_vpc_security_group_ingress_rule" "db_from_api" {
  security_group_id            = aws_security_group.db.id
  description                  = "PostgreSQL from the API"
  referenced_security_group_id = aws_security_group.api.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_vpc_security_group_ingress_rule" "db_from_worker" {
  security_group_id            = aws_security_group.db.id
  description                  = "PostgreSQL from the worker and the migration task"
  referenced_security_group_id = aws_security_group.worker.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}
