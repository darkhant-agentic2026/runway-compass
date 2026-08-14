# Modules declare a compatibility range; the environment root pins the exact version via
# its own `~>` constraint and `.terraform.lock.hcl`, so `terraform init` there resolves
# one provider for the whole stack.
terraform {
  required_version = "~> 1.15"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 7.0, < 8.0"
    }
  }
}
