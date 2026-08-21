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

    # The IAM family, and the reason it is three entries rather than one. These are
    # genuinely different APIs and enabling one does not enable the others:
    #
    #   iam                   service accounts, and the Workload Identity pool/provider
    #   iamcredentials        SignBlob, for the V4 signed upload URLs
    #   cloudresourcemanager  projects.setIamPolicy, which every google_project_iam_member
    #                         goes through
    #
    # `iamcredentials` reads like it covers the first one and does not: without `iam`,
    # the very first `google_service_account` fails with "Identity and Access Management
    # (IAM) API has not been used in project X before or it is disabled".
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "cloudresourcemanager.googleapis.com",

    # Not used by any resource here — it is what GitHub Actions calls at deploy time to
    # exchange its OIDC token for a GCP one. Enabling it with everything else means the
    # first deploy does not fail on a missing API long after this stack looked finished.
    "sts.googleapis.com",
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

# Enabling an API and using it in the same apply is a race: `google_project_service`
# returns as soon as the enablement is *accepted*, and the API can still reject calls for
# up to a minute afterwards with "has not been used in project X before or it is
# disabled". Everything below waits this out, so `terraform apply` works from zero on a
# brand-new project instead of only on the second attempt.
resource "time_sleep" "api_enablement" {
  depends_on      = [google_project_service.enabled]
  create_duration = "60s"
}

resource "google_artifact_registry_repository" "images" {
  project       = var.project_id
  location      = var.region
  repository_id = "coach"
  format        = "DOCKER"
  description   = "coach-api images, tagged with the commit SHA."

  depends_on = [time_sleep.api_enablement]
}

# --- Secrets ----------------------------------------------------------------------------
#
# Terraform owns each container AND a placeholder first version; the real values are added
# afterwards with `gcloud secrets versions add` (RUNBOOK.md step 3).
#
# The placeholder is not tidiness, it is what makes a first apply possible at all. Two
# things here read a secret *version*, not just a container:
#
#   * `data.google_secret_manager_secret_version_access` below, which feeds the Identity
#     Platform Google provider, and
#   * Cloud Run's `secret_key_ref { version = "latest" }`, which fails to create a
#     revision when the secret it names has no versions.
#
# So a container with no version makes the first apply fail, while the runbook step that
# adds the version was documented as running *after* that apply — a cycle. A placeholder
# version breaks it: apply once, replace the values, apply again (which the URL
# reconciliation already requires).

locals {
  # Deliberately not a plausible-looking value: if this ever reaches a real API call, the
  # failure should be obviously "nobody seeded this secret" rather than a puzzling 401.
  secret_placeholder = "REPLACE_ME_VIA_GCLOUD_SEE_RUNBOOK"
}

resource "google_secret_manager_secret" "youtube_api_key" {
  project   = var.project_id
  secret_id = "youtube-api-key"

  replication {
    auto {}
  }

  depends_on = [time_sleep.api_enablement]
}

# Dev only: local development uses the Gemini API with a developer key, production uses
# Vertex AI with the service account and has no key to rotate.
resource "google_secret_manager_secret" "gemini_api_key" {
  project   = var.project_id
  secret_id = "gemini-api-key"

  replication {
    auto {}
  }

  depends_on = [time_sleep.api_enablement]
}

resource "google_secret_manager_secret" "oauth_client_secret" {
  project   = var.project_id
  secret_id = "oauth-client-secret"

  replication {
    auto {}
  }

  depends_on = [time_sleep.api_enablement]
}

# `ignore_changes` on `secret_data` so that re-applying after the real value has been
# added does not try to reinstate the placeholder. Terraform manages only this first
# version; `gcloud secrets versions add` creates later ones it never looks at.
resource "google_secret_manager_secret_version" "youtube_api_key_placeholder" {
  secret      = google_secret_manager_secret.youtube_api_key.id
  secret_data = local.secret_placeholder

  lifecycle {
    ignore_changes = [secret_data]
  }
}

resource "google_secret_manager_secret_version" "gemini_api_key_placeholder" {
  secret      = google_secret_manager_secret.gemini_api_key.id
  secret_data = local.secret_placeholder

  lifecycle {
    ignore_changes = [secret_data]
  }
}

resource "google_secret_manager_secret_version" "oauth_client_secret_placeholder" {
  secret      = google_secret_manager_secret.oauth_client_secret.id
  secret_data = local.secret_placeholder

  lifecycle {
    ignore_changes = [secret_data]
  }
}

# Reads `latest`: the placeholder on the first apply, the real client secret on every
# apply after RUNBOOK.md step 3. `depends_on` defers the read to apply time, so it cannot
# run before the version it needs exists.
data "google_secret_manager_secret_version_access" "oauth_client_secret" {
  project = var.project_id
  secret  = google_secret_manager_secret.oauth_client_secret.secret_id

  depends_on = [google_secret_manager_secret_version.oauth_client_secret_placeholder]
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

  depends_on = [time_sleep.api_enablement]
}

module "firestore" {
  source = "../../modules/firestore"

  project_id               = var.project_id
  location                 = var.region
  enable_pitr              = false
  enable_delete_protection = false

  depends_on = [time_sleep.api_enablement]
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

  min_instances       = var.min_instances
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
    VERTEX_LOCATION               = coalesce(var.vertex_location, var.region)
    FIRESTORE_DATABASE            = module.firestore.database_name
    ADK_FIRESTORE_ROOT_COLLECTION = "adk-session"
    ARTIFACT_BUCKET               = module.storage.artifact_bucket
    UPLOAD_BUCKET                 = module.storage.upload_bucket
    TASKS_QUEUE                   = module.scheduler_tasks.queue_id
    # The **service** URL, not a path under it. The app appends
    # `/internal/runs/{runId}/execute` itself, and — the reason it has to be the bare
    # origin — it is also the OIDC *audience* both internal endpoints verify against.
    # Cloud Scheduler mints its token for `var.service_url`, so a path here would mean
    # two audiences for one check and the tick would be rejected as forged.
    TASKS_TARGET_URL = var.service_url_hint
    TASKS_INVOKER_SA = module.identity.tasks_service_account_email
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

  depends_on = [time_sleep.api_enablement]
}

module "observability" {
  source = "../../modules/observability"

  project_id            = var.project_id
  service_name          = module.cloud_run.service_name
  service_host          = replace(replace(module.cloud_run.service_url, "https://", ""), "/", "")
  notification_channels = var.notification_channels
}
