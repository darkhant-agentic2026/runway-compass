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
  # `nonsensitive()` is a deliberate override, not a way around an inconvenience.
  #
  # The provider marks `client.api_key` sensitive, which is the right default for a
  # value it knows nothing about. This one is different in a way that is checkable: it
  # is compiled into the SPA bundle by `docker build --build-arg VITE_IDENTITY_API_KEY`
  # and shipped to every browser that loads the app. It is public in the strongest
  # sense — anyone can read it out of the JavaScript.
  #
  # The security boundary for it is Identity Platform's authorized-domains list plus
  # server-side token verification, neither of which is weakened by the key being known
  # (docs/07-infra-deploy.md#manual-bootstrap-steps-two-both-one-time-per-environment).
  #
  # Leaving it marked sensitive would mean the runbook teaching operators to reach for
  # `terraform output -raw` on a "sensitive" value and paste it into a *non-secret*
  # GitHub repository variable — which blurs exactly the line that marking matters for.
  # It is a single scalar, so nothing else can hide behind this call.
  value       = nonsensitive(try(google_identity_platform_config.this.client[0].api_key, ""))
  description = "Web API key for the SPA build (VITE_IDENTITY_API_KEY). Public by design."
}

output "identity_auth_domain" {
  # Same reasoning as `identity_api_key` above: this ends up in the SPA bundle too. The
  # provider does not currently mark `firebase_subdomain` sensitive, but it sits in the
  # same object as a field that is, so the call is here to keep the two outputs from
  # behaving differently if that ever changes.
  value       = nonsensitive(try("${google_identity_platform_config.this.client[0].firebase_subdomain}.firebaseapp.com", ""))
  description = "Auth domain for the SPA build (VITE_IDENTITY_AUTH_DOMAIN). Public by design."
}
