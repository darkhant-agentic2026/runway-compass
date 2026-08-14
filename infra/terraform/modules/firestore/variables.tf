variable "project_id" {
  description = "The GCP project that owns the database."
  type        = string
}

variable "database_name" {
  description = "Firestore database id. `(default)` unless there is a reason otherwise."
  type        = string
  default     = "(default)"
}

variable "location" {
  description = "Region, co-located with Cloud Run."
  type        = string
  default     = "us-central1"
}

variable "enable_pitr" {
  description = "Point-in-time recovery. On in prod (docs/07-infra-deploy.md)."
  type        = bool
  default     = false
}

variable "enable_delete_protection" {
  description = "Refuse to delete the database. On in prod."
  type        = bool
  default     = false
}
