# coach-dev. Identical Terraform to prod with different tfvars
# (docs/07-infra-deploy.md#environments).

locals {
  # The YouTube Data API is easy to forget because it is the one dependency reached with
  # an API key rather than IAM, so nothing else in the stack references it.
  services = [
    "run.googleapis.com",
    "firestore.googleapis.com",
    "cloudtasks.googleapis.com",
    "cloudscheduler.googleapis.com",
    "aiplatform.googleapis.com",
    "artifactregistry.googleapis.com",
    "secretmanager.googleapis.com",
    "storage.googleapis.com",
    "identitytoolkit.googleapis.com",
    "iamcredentials.googleapis.com",
    "youtube.googleapis.com",
    "monitoring.googleapis.com",
    "logging.googleapis.com",
    "cloudtrace.googleapis.com",
  ]
}

resource "google_project_service" "enabled" {
  for_each = toset(local.services)

  project = var.project_id
  service = each.value

  # Leave the API enabled if this stack is torn down: disabling a service can break
  # unrelated resources in the same project, and re-enabling is cheap.
  disable_on_destroy = false
}

resource "google_artifact_registry_repository" "images" {
  project       = var.project_id
  location      = var.region
  repository_id = "coach"
  format        = "DOCKER"
  description   = "coach-api images, tagged with the commit SHA."

  depends_on = [google_project_service.enabled]
}

# --- Secrets ----------------------------------------------------------------------------
# Values are set out of band; Terraform owns the container, not the contents.

resource "google_secret_manager_secret" "youtube_api_key" {
  project   = var.project_id
  secret_id = "youtube-api-key"

  replication {
    auto {}
  }

  depends_on = [google_project_service.enabled]
}

# Dev only: local development uses the Gemini API with a developer key, production uses
# Vertex AI with the service account and has no key to rotate.
resource "google_secret_manager_secret" "gemini_api_key" {
  project   = var.project_id
  secret_id = "gemini-api-key"

  replication {
    auto {}
  }

  depends_on = [google_project_service.enabled]
}

resource "google_secret_manager_secret" "oauth_client_secret" {
  project   = var.project_id
  secret_id = "oauth-client-secret"

  replication {
    auto {}
  }

  depends_on = [google_project_service.enabled]
}

data "google_secret_manager_secret_version_access" "oauth_client_secret" {
  project = var.project_id
  secret  = google_secret_manager_secret.oauth_client_secret.secret_id
}

# --- Modules ------------------------------------------------------------------------------

module "identity" {
  source = "../../modules/identity"

  project_id        = var.project_id
  github_repository = var.github_repository
  # The Cloud Run URL is only known after the service exists, so the authorized-domains
  # list is seeded from the tfvar and reconciled on the second apply. This is the one
  # ordering wrinkle in the stack and it is why `terraform apply` is documented as
  # "run it twice on a brand-new project" in RUNBOOK.md.
  authorized_domains  = var.authorized_domains
  oauth_client_id     = var.oauth_client_id
  oauth_client_secret = data.google_secret_manager_secret_version_access.oauth_client_secret.secret_data

  depends_on = [google_project_service.enabled]
}

module "firestore" {
  source = "../../modules/firestore"

  project_id               = var.project_id
  location                 = var.region
  enable_pitr              = false
  enable_delete_protection = false

  depends_on = [google_project_service.enabled]
}

module "storage" {
  source = "../../modules/storage"

  project_id                = var.project_id
  location                  = upper(var.region)
  api_service_account_email = module.identity.api_service_account_email
  cors_origins              = var.authorized_domains_urls
  force_destroy             = true
}

module "cloud_run" {
  source = "../../modules/cloud_run_service"

  project_id            = var.project_id
  region                = var.region
  image                 = var.image
  service_account_email = module.identity.api_service_account_email

  min_instances       = 1
  max_instances       = 10
  max_concurrency     = 40
  deletion_protection = false

  invoker_service_account_emails = [
    module.identity.scheduler_service_account_email,
    module.identity.tasks_service_account_email,
  ]

  # The `.env` contract from docs/07-infra-deploy.md#local-development. `Settings`
  # validates all of it at startup and refuses to boot on anything missing — which is why
  # a typo here fails the deploy smoke test rather than the first request.
  env = {
    ENV                           = "dev"
    GOOGLE_CLOUD_PROJECT          = var.project_id
    MODEL_BACKEND                 = "vertex"
    MODEL_NAME                    = var.model_name
    VERTEX_LOCATION               = var.region
    FIRESTORE_DATABASE            = module.firestore.database_name
    ADK_FIRESTORE_ROOT_COLLECTION = "adk-session"
    ARTIFACT_BUCKET               = module.storage.artifact_bucket
    UPLOAD_BUCKET                 = module.storage.upload_bucket
    TASKS_QUEUE                   = module.scheduler_tasks.queue_id
    TASKS_TARGET_URL              = "${var.service_url_hint}/internal/runs"
    TASKS_INVOKER_SA              = module.identity.tasks_service_account_email
    # Two DIFFERENT service accounts call /internal/*. Collapsing these into one variable
    # would mean either Cloud Scheduler could invoke the executor or the reverse.
    ALLOWED_SCHEDULER_SA = module.identity.scheduler_service_account_email
    ALLOWED_TASKS_SA     = module.identity.tasks_service_account_email
    OAUTH_CLIENT_ID      = var.oauth_client_id
    LOG_LEVEL            = "INFO"

    # FIRESTORE_EMULATOR_HOST is deliberately absent. `Settings` refuses to start when it
    # is set with a non-local ENV, which is the fail-fast guard against a deployed
    # revision silently pointing at nothing.
  }

  secret_env = {
    YOUTUBE_API_KEY = google_secret_manager_secret.youtube_api_key.secret_id
  }
}

module "scheduler_tasks" {
  source = "../../modules/scheduler_tasks"

  project_id                      = var.project_id
  region                          = var.region
  service_url                     = module.cloud_run.service_url
  scheduler_service_account_email = module.identity.scheduler_service_account_email

  depends_on = [google_project_service.enabled]
}

module "observability" {
  source = "../../modules/observability"

  project_id            = var.project_id
  service_name          = module.cloud_run.service_name
  service_host          = replace(replace(module.cloud_run.service_url, "https://", ""), "/", "")
  notification_channels = var.notification_channels
}
