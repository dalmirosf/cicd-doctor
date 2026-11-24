# RDS for PostgreSQL 17 (D-121). Cheapest shape that can hold pgvector: one instance, one AZ, 20 GB
# gp3, default KMS key, no backups, no snapshot on destroy, no Performance Insights. It is a demo
# database: `down` deletes the data. Not publicly accessible; reachable only from the db security group.

resource "aws_db_subnet_group" "main" {
  name       = var.name_prefix
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_db_instance" "main" {
  identifier     = var.name_prefix
  engine         = "postgres"
  engine_version = var.db_engine_version
  instance_class = var.db_instance_class

  allocated_storage = var.db_allocated_storage_gb
  storage_type      = "gp3"
  storage_encrypted = true # the AWS-managed key aws/rds: free, unlike a customer key

  db_name  = var.db_name
  username = var.db_username
  # RDS creates and owns the password in a Secrets Manager secret ({"username","password"}) and
  # deletes it with the instance: the password is in neither the code nor the Terraform state (D-121).
  manage_master_user_password = true

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]
  publicly_accessible    = false
  multi_az               = false

  backup_retention_period      = 0
  skip_final_snapshot          = true
  deletion_protection          = false
  delete_automated_backups     = true
  performance_insights_enabled = false
  monitoring_interval          = 0

  auto_minor_version_upgrade = true
  apply_immediately          = true
}

# The execution role (persistent layer) may read the RDS-managed secret only while it exists: this
# policy is destroyed with the instance, the secret too.
data "aws_iam_policy_document" "read_db_secret" {
  statement {
    sid       = "ReadTheDatabasePassword"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_db_instance.main.master_user_secret[0].secret_arn]
  }
}

resource "aws_iam_role_policy" "execution_reads_db_secret" {
  name   = "read-db-secret"
  role   = data.aws_iam_role.execution.name
  policy = data.aws_iam_policy_document.read_db_secret.json
}
