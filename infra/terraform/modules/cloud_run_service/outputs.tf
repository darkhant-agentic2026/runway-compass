output "service_url" {
  value       = google_cloud_run_v2_service.this.uri
  description = "The service URL: the OIDC audience, the CORS origin for uploads, and the Identity Platform authorized domain."
}

output "service_name" {
  value = google_cloud_run_v2_service.this.name
}
