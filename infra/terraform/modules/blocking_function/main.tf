# The `beforeCreate` blocking function that rejects self-service email/password sign-up
# (apps/functions). This is wired onto Identity Platform's config in modules/identity,
# not here — `google_identity_platform_config` is a per-project singleton, so a second
# declaration of it in this module would fight the one in modules/identity rather than
# add to it. This module only builds the function and hands its trigger URL back as an
# output.
#
# Identity Platform's own support for 2nd-gen blocking functions has a documented rough
# edge: the console (and sometimes `terraform apply` itself) can report the function as
# "deleted or no longer exists" for a while right after the trigger is first wired up,
# before it settles. Confirm in the Cloud Console that the beforeCreate trigger shows the
# function as active after applying this, rather than trusting a clean `apply` alone
# (infra/terraform/RUNBOOK.md).

data "google_project" "this" {
  project_id = var.project_id
}

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

resource "google_cloudfunctions2_function" "block_password_signup" {
  project  = var.project_id
  name     = "block-password-signup"
  location = var.region

  build_config {
    runtime     = "nodejs22"
    entry_point = "beforeCreate"
    source {
      storage_source {
        bucket = google_storage_bucket.source.name
        object = google_storage_bucket_object.source.name
      }
    }
  }

  service_config {
    max_instance_count    = 4
    available_memory      = "256M"
    timeout_seconds       = 30
    service_account_email = google_service_account.function.email
  }
}

# 2nd-gen functions run on Cloud Run under the hood, and
# `google_cloudfunctions2_function_iam_member` does not reliably accept `roles/run.invoker`
# for them (hashicorp/terraform-provider-google#15264) — the grant has to go on the
# underlying Cloud Run service instead, which `service_config[0].service` names once the
# function exists.
#
# The identitytoolkit service agent is the caller: Identity Platform invokes the blocking
# function's HTTPS trigger itself, synchronously, on every `beforeCreate` event.
resource "google_cloud_run_service_iam_member" "identitytoolkit_invokes_function" {
  project  = var.project_id
  location = var.region
  service  = google_cloudfunctions2_function.block_password_signup.service_config[0].service
  role     = "roles/run.invoker"
  member   = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-identitytoolkit.iam.gserviceaccount.com"
}
