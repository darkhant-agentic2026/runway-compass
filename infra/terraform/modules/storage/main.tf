# The two buckets. docs/07-infra-deploy.md#resources-provisioned.

# ADK artifacts: images and PDFs referenced as `types.Part` file parts, written by
# GcsArtifactService (docs/03-agent-design.md#artifacts).
resource "google_storage_bucket" "artifacts" {
  project  = var.project_id
  name     = "${var.project_id}-coach-artifacts"
  location = var.location

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = var.force_destroy

  versioning {
    enabled = true
  }

  # CMEK-ready: the key is left unset here, so turning on customer-managed encryption
  # later is a one-line change rather than a bucket migration.
  dynamic "encryption" {
    for_each = var.kms_key_name == null ? [] : [var.kms_key_name]
    content {
      default_kms_key_name = encryption.value
    }
  }
}

# Uploads: the browser PUTs here directly with a V4 signed URL, so this bucket needs CORS
# and the artifacts bucket does not.
resource "google_storage_bucket" "uploads" {
  project  = var.project_id
  name     = "${var.project_id}-coach-uploads"
  location = var.location

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = var.force_destroy

  cors {
    origin          = var.cors_origins
    method          = ["GET", "PUT", "HEAD"]
    response_header = ["Content-Type", "Content-Range", "Range", "x-goog-resumable"]
    max_age_seconds = 3600
  }

  # This deletes EVERY object here at one day of age, finalized or not — a GCS lifecycle
  # rule has no way to express "unfinalized". That is why this bucket is staging and
  # `finalize` copies the verified bytes into the artifacts bucket through
  # GcsArtifactService (docs/02-data-model.md#collection-map). Referencing an object here
  # from a transcript works for a day and then silently stops.
  lifecycle_rule {
    condition {
      age = 1
    }
    action {
      type = "Delete"
    }
  }
}

# `storage.objectAdmin` scoped to the two buckets rather than granted project-wide.
resource "google_storage_bucket_iam_member" "api_artifacts" {
  bucket = google_storage_bucket.artifacts.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${var.api_service_account_email}"
}

resource "google_storage_bucket_iam_member" "api_uploads" {
  bucket = google_storage_bucket.uploads.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${var.api_service_account_email}"
}
