# The `beforeCreate` blocking function that rejects self-service email/password sign-up
# (apps/functions). This is wired onto Identity Platform's config in modules/identity,
# not here — `google_identity_platform_config` is a per-project singleton, so a second
# declaration of it in this module would fight the one in modules/identity rather than
# add to it. This module only builds the function and hands its trigger URL back as an
# output.
#
# **1st gen, deliberately — not a 2nd gen (Cloud Run backed) function.** Identity
# Platform's blocking-functions trigger mechanism is built against the 1st gen Cloud
# Functions API and does not work with a 2nd gen one.

resource "google_service_account" "function" {
  project      = var.project_id
  account_id   = "coach-auth-blocking-fn"
  display_name = "beforeCreate blocking function — rejects self-service password sign-up"
}

# Source zip storage. A dedicated bucket rather than reusing the artifacts/uploads buckets
# in modules/storage: those are applic-data buckets with their own lifecycle rules
# (docs/07-infra-deploy.md#resources-provisioned), and this one holds build inputs instead.
resource "google_storage_bucket" "source" {
  project  = var.project_id
  name     = "${var.project_id}-coach-function-source"
  location = var.region

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = true
}

data "archive_file" "source" {
  type        = "zip"
  source_dir  = var.source_dir
  output_path = "${path.module}/.build/block-password-signup.zip"
  excludes    = ["node_modules", "lib", "coverage"]
}

# Named after the archive's own hash so a source change uploads a new object and forces
# the function to redeploy; reusing one object name would let Cloud Functions treat an
# updated zip as unchanged.
resource "google_storage_bucket_object" "source" {
  name   = "block-password-signup-${data.archive_file.source.output_md5}.zip"
  bucket = google_storage_bucket.source.name
  source = data.archive_file.source.output_path
}

resource "google_cloudfunctions_function" "block_password_signup" {
  project = var.project_id
  region  = var.region
  name    = "block-password-signup"

  runtime               = "nodejs22"
  entry_point           = "beforeCreate"
  source_archive_bucket = google_storage_bucket.source.name
  source_archive_object = google_storage_bucket_object.source.name

  trigger_http          = true
  available_memory_mb   = 256
  timeout               = 30
  service_account_email = google_service_account.function.email
}

# The unauthenticated-invocation grant `gcloud functions deploy --allow-unauthenticated`
# would make — required because, per the comment above, Identity Platform's call to a
# blocking function carries no IAM credential to check. `apps/functions`' use of
# `gcip-cloud-functions` is the actual security boundary: it verifies the signed JWT
# Identity Platform includes with the request before `blockPasswordSignUp` ever runs.
resource "google_cloudfunctions_function_iam_member" "public_invoker" {
  project        = var.project_id
  region         = var.region
  cloud_function = google_cloudfunctions_function.block_password_signup.name

  role   = "roles/cloudfunctions.invoker"
  member = "allUsers"
}
