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

variable "github_owner" {
  description = "GitHub user or organization that owns the repository whose workflow may deploy (D-123). No default: it is not a secret, but it is yours, so it goes in terraform.tfvars."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9-]+$", var.github_owner))
    error_message = "A GitHub login: letters, digits and hyphens only (no wildcard)."
  }
}

variable "github_owner_id" {
  description = "Numeric id of the GitHub user or organization in github_owner (`gh api users/<owner> --jq .id`). Not a secret. It goes into the OIDC subject (immutable subject)."
  type        = string

  validation {
    condition     = can(regex("^[0-9]+$", var.github_owner_id))
    error_message = "A numeric id: digits only (no wildcard)."
  }
}

variable "github_repo_id" {
  description = "Numeric id of the repository (`gh api repos/<owner>/<repo> --jq .id`). Not a secret. It goes into the OIDC subject (immutable subject)."
  type        = string

  validation {
    condition     = can(regex("^[0-9]+$", var.github_repo_id))
    error_message = "A numeric id: digits only (no wildcard)."
  }
}

variable "github_repo" {
  description = "Repository name whose workflow may deploy (D-123)."
  type        = string
  default     = "cicd-doctor"

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]+$", var.github_repo))
    error_message = "A repository name: letters, digits, dot, underscore and hyphen only (no wildcard)."
  }
}

variable "github_deploy_branch" {
  description = "The one branch whose workflow runs may assume the deploy role (D-123)."
  type        = string
  default     = "main"

  validation {
    condition     = can(regex("^[A-Za-z0-9._/-]+$", var.github_deploy_branch))
    error_message = "A branch name: no wildcard."
  }
}
