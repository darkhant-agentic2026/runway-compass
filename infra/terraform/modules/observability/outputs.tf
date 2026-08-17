output "error_metric_name" {
  value = google_logging_metric.api_errors.name
}

output "uptime_check_id" {
  value = google_monitoring_uptime_check_config.livez.uptime_check_id
}
