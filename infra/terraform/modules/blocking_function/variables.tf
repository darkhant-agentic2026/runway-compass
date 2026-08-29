variable "project_id" {
  type        = string
  description = "The GCP project."
}

variable "region" {
  type        = string
  description = "Cloud Functions (2nd gen) location. Matches the Cloud Run service's region."
}

variable "source_dir" {
  type        = string
  description = "Path to apps/functions. Zipped whole and uploaded as the function's source."
}
