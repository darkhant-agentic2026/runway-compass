# Backend configuration for `terraform init -backend-config=backend.hcl`.
#
# Committed on purpose, like `dev.tfvars`: a bucket name is not a secret, and CI needs it.
# It lives here rather than in `backend.tf` because GCS bucket names are globally unique,
# so the value is specific to whoever set this environment up — and it lives here rather
# than in `dev.tfvars` because a backend block is configured before variables exist and
# the GCS backend rejects keys it does not recognise.
bucket = "verted-ai-veo3-88760-tfstate"
