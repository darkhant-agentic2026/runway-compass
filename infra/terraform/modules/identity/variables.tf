variable "project_id" {
  type        = string
  description = "The GCP project."
}

variable "github_repository" {
  type        = string
  description = "owner/repo. Scopes the Workload Identity provider to this repository only."
}

variable "authorized_domains" {
  type        = list(string)
  description = "Identity Platform authorized domains: the Cloud Run URL, plus a custom domain in prod."
}

variable "oauth_client_id" {
  type        = string
  description = <<-EOT
    OAuth 2.0 web client id for the Google provider. Created by hand — see
    infra/terraform/RUNBOOK.md — and passed in as a tfvar. Public by design.
  EOT
}

variable "oauth_client_secret" {
  type        = string
  sensitive   = true
  description = "The matching client secret, read from Secret Manager by the caller."
}

variable "password_signup_blocking_function_uri" {
  type        = string
  description = <<-EOT
    HTTPS trigger URL of the beforeCreate blocking function that rejects self-service
    email/password sign-up (infra/terraform/modules/blocking_function, apps/functions).
    Identity Platform calls this synchronously before saving a new user.
  EOT
}
