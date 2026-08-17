output "service_url" {
  value       = module.cloud_run.service_url
  description = "The deployed dev URL. M0's exit criterion is a signed-in user seeing their email here."
}

output "identity_api_key" {
  value       = module.identity.identity_api_key
  description = "VITE_IDENTITY_API_KEY for the SPA build. Public by design."
}

output "identity_auth_domain" {
  value       = module.identity.identity_auth_domain
  description = "VITE_IDENTITY_AUTH_DOMAIN for the SPA build."
}

output "workload_identity_provider" {
  value       = module.identity.workload_identity_provider
  description = "Set as the WIF_PROVIDER repository variable in GitHub."
}

output "deployer_service_account" {
  value       = module.identity.deployer_service_account_email
  description = "Set as the DEPLOYER_SERVICE_ACCOUNT repository variable in GitHub."
}

output "artifact_registry" {
  value       = google_artifact_registry_repository.images.name
  description = "The Docker repository the deploy workflow pushes to."
}
