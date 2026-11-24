variable "aws_region" {
  description = "AWS region of the whole project: us-east-2, the only region where the organization policy allows workloads (D-121)."
  type        = string
  default     = "us-east-2"
}

variable "name_prefix" {
  description = "Prefix of every name in the project. This layer finds the persistent layer by names built from it (D-125)."
  type        = string
  default     = "cicd-doctor"
}

variable "vpc_cidr" {
  description = "CIDR of the project VPC. Two public and two private /24 subnets are cut from it."
  type        = string
  default     = "10.20.0.0/16"
}

variable "api_port" {
  description = "Port the API container listens on (`cicd-doctor api --port`, default 8000). The ALB reaches it."
  type        = number
  default     = 8000
}

variable "alb_listener_port" {
  description = "Port of the ALB listener (PR e). CloudFront reaches the ALB over HTTP inside AWS (D-120)."
  type        = number
  default     = 80
}

variable "log_retention_days" {
  description = "Retention of every log group. Short on purpose: the groups are destroyed with the stack, so copy any evidence out before `down`."
  type        = number
  default     = 3

  validation {
    condition     = contains([1, 3, 5, 7], var.log_retention_days)
    error_message = "Use 1, 3, 5 or 7 days (CloudWatch accepts only fixed values; the demo needs no more)."
  }
}

variable "log_streams" {
  description = "One log group per process. `migrate` is the one-off migration task of PR e."
  type        = set(string)
  default     = ["api", "worker", "migrate"]
}

variable "db_engine_version" {
  description = "PostgreSQL version. 17.11 is offered in us-east-2 and AWS documents pgvector 0.8.2 for it; 17.9 and later carry the pgvector CVE-2026-3172 fix (D-121). The real proof is CREATE EXTENSION vector in the live migration."
  type        = string
  default     = "17.11"
}

variable "db_instance_class" {
  description = "Smallest class: orderable for PostgreSQL 17 with gp3 in us-east-2 (measured read-only, D-121)."
  type        = string
  default     = "db.t4g.micro"
}

variable "db_allocated_storage_gb" {
  description = "Storage in GB (gp3). 20 is the minimum for gp3 on PostgreSQL."
  type        = number
  default     = 20
}

variable "db_name" {
  description = "Initial database."
  type        = string
  default     = "cicd_doctor"
}

variable "db_username" {
  description = "Master user. The app and the migrations connect with it (D-121). Its password is managed by RDS."
  type        = string
  default     = "cicd_admin"
}
