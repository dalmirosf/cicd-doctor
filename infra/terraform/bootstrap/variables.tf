variable "aws_region" {
  description = "AWS region of the whole project: us-east-2, the only region where the organization policy allows workloads (D-121)."
  type        = string
  default     = "us-east-2"
}

variable "name_prefix" {
  description = "Prefix of every name in the project."
  type        = string
  default     = "cicd-doctor"
}

variable "state_bucket_name" {
  description = "Name of the state bucket. Null builds <name_prefix>-tfstate-<account id>-<region>, which is unique."
  type        = string
  default     = null
}

variable "noncurrent_version_days" {
  description = "Days an old version of a state file is kept (versioning is the undo for a bad apply)."
  type        = number
  default     = 90
}
