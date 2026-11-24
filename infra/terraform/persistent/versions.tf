# Layer 1: what survives every teardown: ECR, secret containers, IAM roles (D-125).
terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.74"
    }
  }

  # Native S3 locking (use_lockfile): no DynamoDB table (D-125). The bucket and the region come
  # from backend.hcl:  terraform init -backend-config=backend.hcl
  backend "s3" {
    key          = "persistent/terraform.tfstate"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project   = var.name_prefix
      Layer     = "persistent"
      ManagedBy = "terraform"
    }
  }
}
