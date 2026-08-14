output "artifact_bucket" {
  value       = google_storage_bucket.artifacts.name
  description = "For the API's ARTIFACT_BUCKET setting."
}

output "upload_bucket" {
  value       = google_storage_bucket.uploads.name
  description = "For the API's UPLOAD_BUCKET setting."
}
