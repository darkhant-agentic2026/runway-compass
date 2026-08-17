# Uptime check, a log-based error metric, and the alert policies.
#
# docs/07-infra-deploy.md: "Alert policies from docs/05-autonomous-runs.md, uptime check
# on /livez, log-based error metric."

# --- Uptime check -------------------------------------------------------------------

# The liveness path moved from /healthz to /livez, and this resource was renamed with it.
# Without this block Terraform reads that as "destroy one, create another", and the
# destroy fails:
#
#   Error 400: Request contains an invalid argument.
#   - please ensure all associated Alert Policies are deleted.
#
# Cloud Monitoring refuses to delete an uptime check while an alert policy references it,
# and `google_monitoring_alert_policy.uptime` does. `moved` tells Terraform this is the
# same object under a new address, so the change becomes an in-place update of the path
# and nothing is destroyed at all.
#
# Safe to delete once every environment has applied it once.
moved {
  from = google_monitoring_uptime_check_config.healthz
  to   = google_monitoring_uptime_check_config.livez
}

resource "google_monitoring_uptime_check_config" "livez" {
  project      = var.project_id
  display_name = "coach-api /livez"
  timeout      = "10s"
  period       = "300s"

  http_check {
    path         = "/livez"
    port         = 443
    use_ssl      = true
    validate_ssl = true
  }

  monitored_resource {
    type = "uptime_url"
    labels = {
      project_id = var.project_id
      host       = var.service_host
    }
  }

  lifecycle {
    # For a *genuine* replacement later — changing a field this resource cannot update in
    # place — the same deletion block applies: the alert policy below would still
    # reference the old check when Terraform tried to remove it. Creating the replacement
    # first lets the policy be repointed before the old one goes.
    create_before_destroy = true
  }
}

# --- Log-based error metric ----------------------------------------------------------

resource "google_logging_metric" "api_errors" {
  project = var.project_id
  name    = "coach_api_errors"
  filter  = <<-EOT
    resource.type="cloud_run_revision"
    resource.labels.service_name="${var.service_name}"
    severity>=ERROR
  EOT

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

# --- Alert policies -------------------------------------------------------------------

resource "google_monitoring_alert_policy" "error_rate" {
  project      = var.project_id
  display_name = "coach-api error rate"
  combiner     = "OR"

  conditions {
    display_name = "More than ${var.error_threshold} errors in 5 minutes"

    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.api_errors.name}\" AND resource.type=\"cloud_run_revision\""
      duration        = "300s"
      comparison      = "COMPARISON_GT"
      threshold_value = var.error_threshold

      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_DELTA"
      }
    }
  }

  notification_channels = var.notification_channels

  documentation {
    content   = "coach-api is logging errors above the threshold. Check the Cloud Run logs for the failing revision; if a deploy just landed, roll back with `gcloud run services update-traffic --to-revisions <previous>=100`."
    mime_type = "text/markdown"
  }
}

resource "google_monitoring_alert_policy" "uptime" {
  project      = var.project_id
  display_name = "coach-api uptime"
  combiner     = "OR"

  conditions {
    display_name = "/livez failing"

    condition_threshold {
      filter          = "metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\" AND resource.type=\"uptime_url\" AND metric.labels.check_id=\"${google_monitoring_uptime_check_config.livez.uptime_check_id}\""
      duration        = "600s"
      comparison      = "COMPARISON_LT"
      threshold_value = 1

      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_NEXT_OLDER"
        cross_series_reducer = "REDUCE_COUNT_FALSE"
        group_by_fields      = ["resource.label.host"]
      }

      trigger {
        count = 1
      }
    }
  }

  notification_channels = var.notification_channels
}
