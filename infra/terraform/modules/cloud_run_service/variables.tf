variable "project_id" {
  type = string
}

variable "service_name" {
  type    = string
  default = "coach-api"
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "image" {
  type        = string
  description = "Fully qualified image, tagged with the commit SHA by the deploy workflow."
}

variable "service_account_email" {
  type        = string
  description = "coach-api-sa."
}

variable "env" {
  type        = map(string)
  default     = {}
  description = "Plain environment variables; the `.env` contract in docs/07-infra-deploy.md."
}

variable "secret_env" {
  type        = map(string)
  default     = {}
  description = "Environment variable name -> Secret Manager secret id."
}

variable "min_instances" {
  type        = number
  default     = 1
  description = "1, not 0: avoids scale-to-zero mid-generation."
}

variable "max_instances" {
  type    = number
  default = 10
}

variable "max_concurrency" {
  type        = number
  default     = 40
  description = "Per-instance request concurrency."
}

variable "allow_unauthenticated" {
  type        = bool
  default     = true
  description = "The SPA and /api/* are public; /internal/* is protected in the app."
}

variable "invoker_service_account_emails" {
  type        = list(string)
  default     = []
  description = "Scheduler and Tasks service accounts, granted run.invoker."
}

variable "deletion_protection" {
  type    = bool
  default = false
}
