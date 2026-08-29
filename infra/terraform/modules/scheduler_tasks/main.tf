# Cloud Scheduler + Cloud Tasks. docs/05-autonomous-runs.md#trigger-chain.
#
#   Cloud Scheduler (*/15 min, OIDC) -> POST /internal/tick
#     -> Cloud Tasks queue `autonomous-runs`
#       -> POST /internal/runs/{runId}/execute
#
# /internal/tick deliberately does no agent work: it is a fast, bounded planner. All LLM
# work happens in Cloud Tasks deliveries, which gives per-job retry, backoff, dispatch
# rate limiting, and dedup for free, and keeps any single failure from poisoning the tick.

resource "google_cloud_scheduler_job" "tick" {
  project  = var.project_id
  region   = var.region
  name     = "coach-tick"
  schedule = var.schedule

  # The user's own timezone governs quiet hours; the tick itself is timezone-agnostic and
  # is easier to reason about in UTC.
  time_zone = "Etc/UTC"

  # The tick is specified as cheap and bounded (<= 30 s). This deadline is generous
  # enough not to abort a slow-but-working tick and short enough that a wedged one is
  # visibly wedged.
  attempt_deadline = "720s"

  retry_config {
    retry_count = 1
  }

  http_target {
    http_method = "POST"
    uri         = "${var.service_url}/internal/tick"

    oidc_token {
      service_account_email = var.scheduler_service_account_email
      # Verified by the app for issuer, audience, and caller email.
      audience = var.service_url
    }
  }
}

resource "google_cloud_tasks_queue" "autonomous_runs" {
  project  = var.project_id
  location = var.region
  name     = "autonomous-runs"

  rate_limits {
    # Deliberately slow. Background research competes with request serving for CPU on the
    # same instances, and excess work should stay queued here rather than be admitted.
    max_dispatches_per_second = 1
    max_concurrent_dispatches = 5
  }

  retry_config {
    max_attempts       = 3
    min_backoff        = "30s"
    max_backoff        = "600s"
    max_doublings      = 4
    max_retry_duration = "3600s"
  }
}
