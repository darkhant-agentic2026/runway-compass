# The Cloud Run service. docs/07-infra-deploy.md#cloud-run-configuration-the-settings-that-matter.
#
# Three settings here are correctness requirements rather than tuning, and each is
# commented where it is set.

resource "google_cloud_run_v2_service" "this" {
  project  = var.project_id
  name     = var.service_name
  location = var.region

  deletion_protection = var.deletion_protection

  # The service is not public for /internal/*; Cloud Run IAM below is what enforces that,
  # in addition to the OIDC audience and caller-email checks in the app.
  ingress = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = var.service_account_email

    # Long WebSocket connections and long turns (docs/04-api-contract.md).
    timeout = "3600s"

    # WebSocket connections are cheap; agent runs are not. A per-instance asyncio
    # semaphore caps concurrent agent runs so background work cannot starve interactive
    # turns.
    max_instance_request_concurrency = var.max_concurrency

    # Reconnects prefer the owning instance, which is what makes the same-instance resume
    # path the common case rather than the exception.
    session_affinity = true

    scaling {
      # A warm instance; also avoids scale-to-zero mid-generation. Costs a little idle
      # money and buys no cold start on the scheduler tick, warm ADK/Vertex clients, and
      # a stable instance for session affinity.
      min_instance_count = var.min_instances
      max_instance_count = var.max_instances
    }

    containers {
      image = var.image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "2"
          memory = "2Gi"
        }

        # CPU ALWAYS ALLOCATED. This is not an optimization, it is a correctness
        # requirement: with request-based CPU allocation a container is throttled to
        # near-zero CPU outside request processing, which would stall a detached
        # generation task the moment its client disconnects — exactly the scenario the
        # design must survive (docs/04-api-contract.md#surviving-client-disconnects).
        cpu_idle = false

        startup_cpu_boost = true
      }

      dynamic "env" {
        for_each = var.env
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = var.secret_env
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = env.value
              version = "latest"
            }
          }
        }
      }

      # Liveness only — /healthz must not touch a dependency, or a Firestore blip gets
      # healthy instances killed. /readyz is the one that reaches Firestore, and it is
      # used by the deploy smoke test rather than by the platform.
      startup_probe {
        http_get {
          path = "/healthz"
        }
        initial_delay_seconds = 5
        period_seconds        = 5
        failure_threshold     = 12
        timeout_seconds       = 3
      }

      liveness_probe {
        http_get {
          path = "/healthz"
        }
        period_seconds    = 30
        failure_threshold = 3
        timeout_seconds   = 3
      }
    }
  }

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }

  lifecycle {
    # The deploy workflow moves traffic with `gcloud run services update-traffic` after a
    # smoke test, so Terraform must not fight it back to LATEST on the next apply.
    ignore_changes = [traffic, client, client_version]
  }
}

# Public for the SPA and /api/*. The /internal/* paths are protected by the OIDC
# verification in the app (issuer, audience, and caller email), not by this.
resource "google_cloud_run_v2_service_iam_member" "public" {
  count = var.allow_unauthenticated ? 1 : 0

  project  = google_cloud_run_v2_service.this.project
  location = google_cloud_run_v2_service.this.location
  name     = google_cloud_run_v2_service.this.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# The two internal callers get run.invoker explicitly. They are separate members, not one
# group, because collapsing them would let Cloud Scheduler invoke the executor or the
# reverse (docs/07-infra-deploy.md).
resource "google_cloud_run_v2_service_iam_member" "invokers" {
  for_each = toset(var.invoker_service_account_emails)

  project  = google_cloud_run_v2_service.this.project
  location = google_cloud_run_v2_service.this.location
  name     = google_cloud_run_v2_service.this.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${each.value}"
}
