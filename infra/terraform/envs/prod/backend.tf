# Terraform state in a GCS backend bucket per environment, with versioning and object
# locking (docs/07-infra-deploy.md#environments). The bucket is created by hand as part
# of the bootstrap — a state bucket cannot be managed by the state it holds.
#
# PARTIAL CONFIGURATION. The bucket is deliberately not here.
#
# A backend block cannot reference variables, locals, or any expression at all: it is
# evaluated before the rest of the configuration exists, so there is nothing to reference
# yet. That is a hard Terraform limitation and not something a different arrangement of
# these files can work around.
#
# GCS bucket names are globally unique, so a hard-coded one is wrong for everybody except
# whoever claimed it first. The supported alternative is a partial configuration supplied
# at init time, which is what `backend.hcl` next to this file is:
#
#   terraform init -backend-config=backend.hcl
#
# `prefix` stays here because it is scoped to this directory rather than to a bucket, so
# it is the same for every environment that uses this layout.
#
# CI runs `terraform init -backend=false` on pull requests, so validation never touches
# the bucket or needs a credential.
terraform {
  backend "gcs" {
    prefix = "envs/prod"
  }
}
