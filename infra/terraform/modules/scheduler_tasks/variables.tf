variable "project_id" {
  type = string
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "service_url" {
  type        = string
  description = "The Cloud Run URL. Also the OIDC audience."
}

variable "scheduler_service_account_email" {
  type        = string
  description = "coach-scheduler-sa. Distinct from the tasks SA on purpose."
}

variable "schedule" {
  type        = string
  default     = "*/15 * * * *"
  description = "Cron for /internal/tick."
}
