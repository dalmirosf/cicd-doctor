variable "image_tag" {
  description = "Tag of the image in the ECR repository (one tag per commit, pushed by the CD or by hand BEFORE the first apply). Only the first creation uses it: afterwards the CD registers new task definition revisions and the services ignore this value (D-123)."
  type        = string
}

variable "services_enabled" {
  description = "false (default): the services exist with zero tasks, so the migration can run on the empty database first. true: one api task and one worker task. The runbook applies once with false, runs the migration, then applies with true (D-123)."
  type        = bool
  default     = false
}

variable "github_app_id" {
  description = "App ID of the production GitHub App (a number, not a secret)."
  type        = string

  validation {
    condition     = can(regex("^[0-9]+$", var.github_app_id))
    error_message = "The App ID is a number."
  }
}

variable "openai_model" {
  description = "OPENAI_MODEL. Empty: the model is unset and the worker diagnoses with rules only."
  type        = string
  default     = ""
}

variable "llm_price_input_per_mtok" {
  description = "LLM_PRICE_INPUT_PER_MTOK (USD per million input tokens). Empty: unset."
  type        = string
  default     = ""
}

variable "llm_price_output_per_mtok" {
  description = "LLM_PRICE_OUTPUT_PER_MTOK (USD per million output tokens). Empty: unset."
  type        = string
  default     = ""
}

variable "langfuse_host" {
  description = "LANGFUSE_HOST. Tracing needs this and the two Langfuse keys (D-118)."
  type        = string
  default     = "https://cloud.langfuse.com"
}

variable "api_cpu" {
  description = "Fargate CPU units of the api task: 256 = 0.25 vCPU. The api idles at about 92 MiB."
  type        = number
  default     = 256
}

variable "api_memory" {
  description = "MiB of the api task."
  type        = number
  default     = 512
}

variable "worker_cpu" {
  description = "Fargate CPU units of the worker task: 512 = 0.5 vCPU."
  type        = number
  default     = 512
}

variable "worker_memory" {
  description = "MiB of the worker task. It idles at about 294 MiB and peaks near 352 MiB while embedding (measured in P5 c)."
  type        = number
  default     = 1024
}
