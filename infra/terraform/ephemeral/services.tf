# The running services (PR e, D-120, D-123): the API behind the ALB, the worker, and the one-off
# migration task. One image for all three; the command decides (D-119). Fargate, public subnets,
# public IP, awsvpc (D-121).

locals {
  image = "${data.aws_ecr_repository.app.repository_url}:${var.image_tag}"

  # Secret containers of the persistent layer, found by name (D-125, D-122). The value is injected by
  # ECS when the task starts: it is never in Terraform, in the task definition or in the state.
  # ENV VAR -> secret name suffix (persistent/secrets.tf).
  secret_names = {
    GITHUB_PRIVATE_KEY    = "github-app-private-key"
    GITHUB_WEBHOOK_SECRET = "github-webhook-secret"
    OPENAI_API_KEY        = "openai-api-key"
    LANGFUSE_PUBLIC_KEY   = "langfuse-public-key"
    LANGFUSE_SECRET_KEY   = "langfuse-secret-key"
  }

  # The internet-facing API needs only the webhook secret (to check the signature) and the
  # database (container.py: api_dependencies). The App key, the OpenAI key and the Langfuse keys
  # belong to the worker alone: a compromised API task must not hold them.
  api_secret_envs = ["GITHUB_WEBHOOK_SECRET"]

  # Plain, non-secret settings. EMBEDDING_CACHE_DIR, TIKTOKEN_CACHE_DIR and HF_HUB_OFFLINE are baked
  # into the image (D-119). Empty values count as unset (settings.py: env_ignore_empty).
  # The names of a secret (KEY, SECRET, TOKEN, PASSWORD, DATABASE_URL) never go here:
  # tests/unit/test_infra_guard.py refuses it.
  api_environment = {
    APP_ENV    = "prod"
    LOG_FORMAT = "json"
    LOG_LEVEL  = "INFO"
    DB_HOST    = aws_db_instance.main.address
    DB_PORT    = tostring(aws_db_instance.main.port)
    DB_NAME    = aws_db_instance.main.db_name
    DB_USER    = aws_db_instance.main.username
  }

  # The worker also reads GitHub, OpenAI and Langfuse. It has 0.5 vCPU: one ONNX thread, not as
  # many as the host has cores.
  worker_environment = merge(local.api_environment, {
    GITHUB_APP_ID             = var.github_app_id
    OPENAI_MODEL              = var.openai_model
    LLM_PRICE_INPUT_PER_MTOK  = var.llm_price_input_per_mtok
    LLM_PRICE_OUTPUT_PER_MTOK = var.llm_price_output_per_mtok
    LANGFUSE_HOST             = var.langfuse_host
    EMBEDDING_THREADS         = "1"
  })

  # The migration reads only the database and the log format.
  migrate_environment = {
    APP_ENV    = "prod"
    LOG_FORMAT = "json"
    DB_HOST    = aws_db_instance.main.address
    DB_PORT    = tostring(aws_db_instance.main.port)
    DB_NAME    = aws_db_instance.main.db_name
    DB_USER    = aws_db_instance.main.username
  }

  # The RDS-managed secret is {"username","password"}: ECS takes one JSON key with "<arn>:password::".
  db_password_secret = {
    name      = "DB_PASSWORD"
    valueFrom = "${aws_db_instance.main.master_user_secret[0].secret_arn}:password::"
  }

  secret_entries = {
    for env, _ in local.secret_names : env => {
      name      = env
      valueFrom = data.aws_secretsmanager_secret.app[env].arn
    }
  }

  api_secrets     = concat([for env in local.api_secret_envs : local.secret_entries[env]], [local.db_password_secret])
  worker_secrets  = concat(values(local.secret_entries), [local.db_password_secret])
  migrate_secrets = [local.db_password_secret]
}

data "aws_secretsmanager_secret" "app" {
  for_each = local.secret_names

  name = "${var.name_prefix}/${each.value}"
}

# ---- task definitions -------------------------------------------------------------------------

resource "aws_ecs_task_definition" "api" {
  family                   = "${var.name_prefix}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.api_cpu
  memory                   = var.api_memory
  execution_role_arn       = data.aws_iam_role.execution.arn
  task_role_arn            = data.aws_iam_role.api_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64" # the CD builds on an x86 runner
  }

  container_definitions = jsonencode([{
    name         = "api"
    image        = local.image
    essential    = true
    command      = ["cicd-doctor", "api", "--host", "0.0.0.0", "--port", tostring(var.api_port)]
    portMappings = [{ containerPort = var.api_port, protocol = "tcp" }]
    environment  = [for k, v in local.api_environment : { name = k, value = v }]
    secrets      = local.api_secrets
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.app["api"].name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "ecs"
      }
    }
  }])
}

resource "aws_ecs_task_definition" "worker" {
  family                   = "${var.name_prefix}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.worker_cpu
  memory                   = var.worker_memory
  execution_role_arn       = data.aws_iam_role.execution.arn
  task_role_arn            = data.aws_iam_role.worker_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name        = "worker"
    image       = local.image
    essential   = true
    command     = ["cicd-doctor", "worker"]
    environment = [for k, v in local.worker_environment : { name = k, value = v }]
    secrets     = local.worker_secrets
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.app["worker"].name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "ecs"
      }
    }
  }])
}

# The one-off migration: same image, `alembic upgrade head`, no service. It connects as the master
# user (a member of rds_superuser), which migration 0001 needs for CREATE EXTENSION vector (D-121).
# The CD registers a new revision of this family with the new image and runs it (D-123); the
# runbook runs this revision by hand the first time.
resource "aws_ecs_task_definition" "migrate" {
  family                   = "${var.name_prefix}-migrate"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = data.aws_iam_role.execution.arn
  task_role_arn            = data.aws_iam_role.worker_task.arn # no policy: it calls no AWS API

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name        = "migrate"
    image       = local.image
    essential   = true
    command     = ["alembic", "upgrade", "head"]
    environment = [for k, v in local.migrate_environment : { name = k, value = v }]
    secrets     = local.migrate_secrets
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.app["migrate"].name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "ecs"
      }
    }
  }])
}

# ---- services ---------------------------------------------------------------------------------
# desired_count is 0 until `services_enabled` (D-123): the migration must run first. The CD owns
# the task definition after the first apply, so Terraform ignores that attribute (otherwise every
# deploy would show up as drift and the next apply would roll the image back).

resource "aws_ecs_service" "api" {
  name            = "${var.name_prefix}-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = var.services_enabled ? 1 : 0
  launch_type     = "FARGATE"

  health_check_grace_period_seconds  = 60
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.api.id]
    assign_public_ip = true # no NAT (D-121)
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = var.api_port
  }

  # The target group joins the ALB only through the listener RULE: CreateService fails with "target
  # group does not have an associated load balancer" if the service is created before it.
  depends_on = [
    aws_lb_listener.http,
    aws_lb_listener_rule.from_our_cloudfront,
    aws_iam_role_policy.execution_reads_db_secret,
  ]

  lifecycle {
    ignore_changes = [task_definition]
  }
}

resource "aws_ecs_service" "worker" {
  name            = "${var.name_prefix}-worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = var.services_enabled ? 1 : 0
  launch_type     = "FARGATE"

  # One worker: stop the old task before the new one starts (a second one only costs money; the
  # queue would tolerate it, SKIP LOCKED).
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.worker.id]
    assign_public_ip = true
  }

  depends_on = [aws_iam_role_policy.execution_reads_db_secret]

  lifecycle {
    ignore_changes = [task_definition]
  }
}
