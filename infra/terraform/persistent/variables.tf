variable "aws_region" {
  description = "AWS region of the whole project: us-east-2, the only region where the organization policy allows workloads (D-121)."
  type        = string
  default     = "us-east-2"
}

variable "name_prefix" {
  description = "Prefix of every name in the project. The layers find each other by names built from it (D-125)."
  type        = string
  default     = "cicd-doctor"
}

variable "secret_recovery_window_days" {
  description = "Recovery window of a deleted secret container (7 to 30, or 0 for an immediate delete). 7 because these secrets are not destroyed with the demo (D-122)."
  type        = number
  default     = 7

  validation {
    condition     = var.secret_recovery_window_days == 0 || (var.secret_recovery_window_days >= 7 && var.secret_recovery_window_days <= 30)
    error_message = "Use 0 or a number from 7 to 30."
  }
}

variable "ecr_images_to_keep" {
  description = "How many images the lifecycle policy of the repository keeps."
  type        = number
  default     = 3
}

variable "ecr_tag_mutability" {
  description = "IMMUTABLE: the CD pushes one tag per commit and never moves a tag."
  type        = string
  default     = "IMMUTABLE"

  validation {
    condition     = contains(["IMMUTABLE", "MUTABLE"], var.ecr_tag_mutability)
    error_message = "Use IMMUTABLE or MUTABLE."
  }
}
