# Terraform state in a GCS backend bucket per environment, with versioning and object
# locking (docs/07-infra-deploy.md#environments). The bucket is created by hand as part
# of the bootstrap — a state bucket cannot be managed by the state it holds.
#
# CI runs `terraform init -backend=false` on pull requests, so validation never touches
# this bucket or needs a credential.
terraform {
  backend "gcs" {
    bucket = "coach-prod-tfstate"
    prefix = "envs/prod"
  }
}
