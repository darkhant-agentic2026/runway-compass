variable "project_id" {
  type        = string
  description = "The GCP project."
}

variable "region" {
  type        = string
  description = "Cloud Functions (1st gen) location."
}

variable "source_dir" {
  type        = string
  description = "Path to apps/functions. Zipped whole and uploaded as the function's source."
}
