variable "project_id" {
  type        = string
  description = "The GCP project id for this environment (the design calls it coach-dev)."
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "image" {
  type        = string
  description = <<-EOT
    The image to deploy, tagged with a commit SHA. Passed with `-var` by the deploy
    workflow, and by hand on the first apply (RUNBOOK.md step 3).

    Deliberately no default. The obvious one — a `:bootstrap` tag under
    `us-central1-docker.pkg.dev/coach-dev/coach/` — hard-codes a project id, and in a
    project not called `coach-dev` it silently points Cloud Run at *another* project's
    registry. That surfaces as a 403 on `artifactregistry.repositories.downloadArtifacts`
    naming a project you have never heard of, which is a poor way to learn that a default
    was wrong. Being asked for the value is better than being given someone else's.
  EOT
}

variable "model_name" {
  type    = string
  default = "gemini-3.7-flash"
}

variable "github_repository" {
  type        = string
  description = "owner/repo, for the Workload Identity attribute condition."
}

variable "oauth_client_id" {
  type        = string
  description = <<-EOT
    OAuth 2.0 web client id. Created by hand (RUNBOOK.md step 2) and committed here: it is
    a public value. The client *secret* lives in Secret Manager, never in a tfvars file.
  EOT
}

variable "authorized_domains" {
  type        = list(string)
  description = <<-EOT
    Identity Platform authorized domains, as bare hostnames. Seeded from tfvars because
    the Cloud Run URL is not known until the service exists; see the note in main.tf.
  EOT
}

variable "authorized_domains_urls" {
  type        = list(string)
  description = "The same domains as full https:// origins, for the uploads bucket CORS list."
}

variable "service_url_hint" {
  type        = string
  description = <<-EOT
    The service URL, for TASKS_TARGET_URL. A hint rather than a reference because Cloud
    Tasks targets the service that the same apply creates, and threading the computed URL
    back into the container env would be a cycle.
  EOT
}

variable "notification_channels" {
  type        = list(string)
  default     = []
  description = "Monitoring notification channels for the alert policies."
}
