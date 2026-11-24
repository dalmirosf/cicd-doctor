# Secret CONTAINERS only (D-122). There is no secret version resource anywhere: the owner loads
# each value by hand, outside Terraform and outside the chat, so no value ever reaches the state.
# The services (PR e) inject them as environment variables through the ECS `secrets` field.
# Non-secret settings (GITHUB_APP_ID, OPENAI_MODEL, LANGFUSE_HOST ...) are plain environment
# variables of the task definition, also PR e. GITHUB_WEBHOOK_SECRET_PREVIOUS is only needed while
# a webhook secret is being rotated: add a container then.
locals {
  secrets = {
    github_app_private_key = {
      env_var     = "GITHUB_PRIVATE_KEY"
      description = "PEM private key of the production GitHub App"
    }
    github_webhook_secret = {
      env_var     = "GITHUB_WEBHOOK_SECRET"
      description = "Webhook secret of the production GitHub App"
    }
    openai_api_key = {
      env_var     = "OPENAI_API_KEY"
      description = "OpenAI API key"
    }
    langfuse_public_key = {
      env_var     = "LANGFUSE_PUBLIC_KEY"
      description = "Langfuse project public key"
    }
    langfuse_secret_key = {
      env_var     = "LANGFUSE_SECRET_KEY"
      description = "Langfuse project secret key"
    }
  }
}

resource "aws_secretsmanager_secret" "app" {
  for_each = local.secrets

  name                    = "${var.name_prefix}/${replace(each.key, "_", "-")}"
  description             = each.value.description
  recovery_window_in_days = var.secret_recovery_window_days
}
