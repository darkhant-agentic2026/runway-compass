# Service accounts, IAM, Workload Identity Federation, and Identity Platform.
#
# The IAM table is docs/07-infra-deploy.md#iam-least-privilege. Two grants in it are easy
# to miss and both fail only at runtime in a deployed environment, so they are called out
# where they are made rather than in a comment at the top.

# --- Service accounts -------------------------------------------------------------------

resource "google_service_account" "api" {
  project      = var.project_id
  account_id   = "coach-api-sa"
  display_name = "coach-api — the only principal that can reach Firestore"
}

resource "google_service_account" "scheduler" {
  project      = var.project_id
  account_id   = "coach-scheduler-sa"
  display_name = "Cloud Scheduler — invokes /internal/tick"
}

resource "google_service_account" "tasks" {
  project      = var.project_id
  account_id   = "coach-tasks-sa"
  display_name = "Cloud Tasks — invokes /internal/runs/{id}/execute"
}

resource "google_service_account" "deployer" {
  project      = var.project_id
  account_id   = "github-deployer-sa"
  display_name = "GitHub Actions deployer (Workload Identity Federation, no keys)"
}

# --- coach-api-sa roles -----------------------------------------------------------------
#
# `datastore.user` here is the entire Firestore access boundary: no other principal can
# read the data, and no client-side path exists (docs/02-data-model.md#access-model).

locals {
  api_roles = [
    "roles/datastore.user",
    "roles/aiplatform.user",
    "roles/cloudtasks.enqueuer",
    "roles/secretmanager.secretAccessor",
    # The IAM role governing Identity Platform. `coach-api-sa` needs it so the
    # `DELETE /api/me` cascade can remove the identity record. Token *verification* needs
    # no IAM at all — it is an offline signature check against Google's public keys.
    "roles/firebaseauth.admin",
    "roles/logging.logWriter",
    "roles/cloudtrace.agent",
  ]
}

resource "google_project_iam_member" "api" {
  for_each = toset(local.api_roles)

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.api.email}"
}

# V4 signed upload URLs are signed through the IAM SignBlob API rather than a downloaded
# key. WITHOUT THIS the upload flow fails at runtime with a signing error, and only in a
# deployed environment — local dev uses the developer's impersonated credentials, so it
# passes there.
resource "google_service_account_iam_member" "api_signs_as_itself" {
  service_account_id = google_service_account.api.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.api.email}"
}

# Creating a Cloud Task that carries an OIDC token as `coach-tasks-sa` requires acting as
# that account.
resource "google_service_account_iam_member" "api_acts_as_tasks" {
  service_account_id = google_service_account.tasks.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.api.email}"
}

# --- github-deployer-sa roles -----------------------------------------------------------

resource "google_project_iam_member" "deployer" {
  for_each = toset([
    "roles/run.admin",
    "roles/artifactregistry.writer",
    "roles/iam.serviceAccountUser",
  ])

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.deployer.email}"
}

# --- Workload Identity Federation --------------------------------------------------------
# Keyless GitHub Actions -> GCP auth. No service account keys anywhere.

resource "google_iam_workload_identity_pool" "github" {
  project                   = var.project_id
  workload_identity_pool_id = "github-pool"
  display_name              = "GitHub Actions"
  description               = "Keyless federation for ${var.github_repository}"
}

resource "google_iam_workload_identity_pool_provider" "github" {
  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-provider"
  display_name                       = "GitHub OIDC"

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
  }

  # Required, and load-bearing: without a condition scoping the provider to this
  # repository, any GitHub Actions workflow anywhere could mint tokens for this project.
  attribute_condition = "assertion.repository == '${var.github_repository}'"

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account_iam_member" "deployer_federation" {
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_repository}"
}

# --- Identity Platform --------------------------------------------------------------------
#
# BOTH resources below depend on a manual bootstrap step that Terraform cannot perform:
# Identity Platform has to be enabled through the Cloud Marketplace first, and the project
# must have billing enabled. See infra/terraform/RUNBOOK.md. Applying without that fails
# here, not somewhere confusing later.
#
# Both are in the GA hashicorp/google provider, so the stack needs no google-beta
# dependency.

resource "google_identity_platform_config" "this" {
  project = var.project_id

  # The Cloud Run service URL, plus a custom domain in prod. A sign-in popup from any
  # other origin is refused, which is the actual security boundary for the public Web API
  # key below.
  #
  # `authorized_domains` REPLACES the list rather than adding to it, and Identity Platform
  # ships three entries by default that things depend on:
  #
  #   <project>.firebaseapp.com   the auth domain itself, which serves the
  #                               /__/auth/handler that signInWithPopup returns to
  #   <project>.web.app           the same handler on the other default domain
  #   localhost                   local development against a real Identity Platform
  #
  # Setting the variable alone therefore deletes the domain the popup handler lives on,
  # and sign-in fails *after* the user has already consented — the popup closes cleanly
  # and the app simply stays signed out, which is about the least debuggable failure
  # available. Union rather than replace.
  #
  # These are derived from `project_id` rather than from
  # `google_identity_platform_config.this.client[0].firebase_subdomain`, which would be a
  # self-reference and a dependency cycle. The default domains use the project id.
  authorized_domains = distinct(concat(
    [
      "localhost",
      "${var.project_id}.firebaseapp.com",
      "${var.project_id}.web.app",
    ],
    var.authorized_domains,
  ))
}

resource "google_identity_platform_default_supported_idp_config" "google" {
  project       = var.project_id
  enabled       = true
  idp_id        = "google.com"
  client_id     = var.oauth_client_id
  client_secret = var.oauth_client_secret

  depends_on = [google_identity_platform_config.this]
}
