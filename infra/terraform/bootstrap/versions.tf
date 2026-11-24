# Layer 0: the bucket that holds the state of the other two layers (D-125).
# Its own state is LOCAL (terraform.tfstate in this folder, git-ignored). The owner applies it once.
terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.74"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project   = var.name_prefix
      Layer     = "bootstrap"
      ManagedBy = "terraform"
    }
  }
}
