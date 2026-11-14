# Case tf-init: this provider does not exist in the registry.
terraform {
  required_providers {
    nope = {
      source  = "hashicorp/cicd-doctor-lab-does-not-exist"
      version = "~> 1.0"
    }
  }
}
