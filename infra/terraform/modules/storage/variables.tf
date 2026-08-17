variable "project_id" {
  type        = string
  description = "The GCP project. Also the bucket-name prefix."
}

variable "location" {
  type        = string
  default     = "US-CENTRAL1"
  description = "Bucket location."
}

variable "api_service_account_email" {
  type        = string
  description = "coach-api-sa, which gets objectAdmin on both buckets and nothing wider."
}

variable "cors_origins" {
  type        = list(string)
  description = "Origins allowed to PUT to the uploads bucket: the Cloud Run service URL."
}

variable "force_destroy" {
  type        = bool
  default     = false
  description = "Allow `terraform destroy` to delete non-empty buckets. Dev only."
}

variable "kms_key_name" {
  type        = string
  default     = null
  description = "Optional CMEK key for the artifacts bucket."
}
