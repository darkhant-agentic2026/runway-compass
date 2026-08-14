output "api_service_account_email" {
  value       = google_service_account.api.email
  description = "coach-api-sa; the only principal holding roles/datastore.user."
}

output "scheduler_service_account_email" {
  value       = google_service_account.scheduler.email
  description = "Allow-listed separately from the tasks SA on /internal/*."
}

output "tasks_service_account_email" {
  value       = google_service_account.tasks.email
  description = "Allow-listed separately from the scheduler SA on /internal/*."
}

output "deployer_service_account_email" {
  value       = google_service_account.deployer.email
  description = "For the deploy workflow's `service_account` input."
}

output "workload_identity_provider" {
  value       = google_iam_workload_identity_pool_provider.github.name
  description = "For the deploy workflow's `workload_identity_provider` input."
}

# `client.api_key` and `client.firebase_subdomain` are Terraform outputs rather than
# values copied by hand into CI. The API key lands in state as plain text; the state
# bucket is access-controlled and versioned, and the value is public by design — the
# security boundary is the authorized-domains list and server-side token verification.
output "identity_api_key" {
  value       = try(google_identity_platform_config.this.client[0].api_key, "")
  description = "Web API key for the SPA build (VITE_IDENTITY_API_KEY)."
}

output "identity_auth_domain" {
  value       = try("${google_identity_platform_config.this.client[0].firebase_subdomain}.firebaseapp.com", "")
  description = "Auth domain for the SPA build (VITE_IDENTITY_AUTH_DOMAIN)."
}
