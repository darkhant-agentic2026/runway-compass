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

variable "min_instances" {
  type    = number
  default = 1

  description = <<-EOT
    Cloud Run `min_instance_count`.

    The design specifies 1 (docs/07-infra-deploy.md#cloud-run-configuration-the-settings-that-matter),
    and from **M2 that stops being a cost preference and becomes a correctness
    requirement**: a scaled-to-zero instance can be reaped in the middle of a detached
    generation task, which is precisely the failure the disconnect guarantee exists to
    prevent. It also removes cold starts on the scheduler tick and keeps session affinity
    pointing somewhere real.

    Until M2 there is no streaming to lose, so an environment may set 0 and pay nothing
    while idle. That is a deliberate, temporary trade and it belongs in a tfvars file
    where it is visible, not as a local edit to a tracked `.tf`.

    **Set this back to 1 in every environment before M2.**
  EOT

  validation {
    condition     = var.min_instances >= 0 && var.min_instances <= 10
    error_message = "min_instances must be between 0 and 10."
  }
}
