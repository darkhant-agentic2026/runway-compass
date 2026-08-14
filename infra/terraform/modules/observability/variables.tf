variable "project_id" {
  type = string
}

variable "service_name" {
  type        = string
  description = "Cloud Run service name, for the log filter."
}

variable "service_host" {
  type        = string
  description = "Hostname of the service, without scheme, for the uptime check."
}

variable "error_threshold" {
  type        = number
  default     = 5
  description = "Errors in a 5-minute window before the policy fires."
}

variable "notification_channels" {
  type        = list(string)
  default     = []
  description = "Monitoring notification channel ids. Created outside this stack."
}
