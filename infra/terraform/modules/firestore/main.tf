# Firestore: the database, the indexes from docs/02-data-model.md, and the TTL policies.

resource "google_firestore_database" "this" {
  project = var.project_id
  name    = var.database_name
  # Native mode, region co-located with Cloud Run (docs/02-data-model.md).
  location_id = var.location
  type        = "FIRESTORE_NATIVE"

  point_in_time_recovery_enablement = var.enable_pitr ? "POINT_IN_TIME_RECOVERY_ENABLED" : "POINT_IN_TIME_RECOVERY_DISABLED"
  delete_protection_state           = var.enable_delete_protection ? "DELETE_PROTECTION_ENABLED" : "DELETE_PROTECTION_DISABLED"
  deletion_policy                   = var.enable_delete_protection ? "ABANDON" : "DELETE"
}

# --- Composite indexes -----------------------------------------------------------------
# The table in docs/02-data-model.md#indexes. Each one is named after the query it serves;
# an index without a query behind it is a cost with no benefit.

# "Board: tasks by order, excluding hidden states"
resource "google_firestore_index" "tasks_state_order" {
  project     = var.project_id
  database    = google_firestore_database.this.name
  collection  = "tasks"
  query_scope = "COLLECTION"

  fields {
    field_path = "state"
    order      = "ASCENDING"
  }
  fields {
    field_path = "order"
    order      = "ASCENDING"
  }
}

# "Next-up selection"
resource "google_firestore_index" "tasks_next_up" {
  project     = var.project_id
  database    = google_firestore_database.this.name
  collection  = "tasks"
  query_scope = "COLLECTION"

  fields {
    field_path = "state"
    order      = "ASCENDING"
  }
  fields {
    field_path = "needsResearch"
    order      = "ASCENDING"
  }
  fields {
    field_path = "order"
    order      = "ASCENDING"
  }
}

# "Expired postponements sweep" — collection group, because the sweep in /internal/tick
# runs across every project at once.
resource "google_firestore_index" "tasks_postponement_sweep" {
  project     = var.project_id
  database    = google_firestore_database.this.name
  collection  = "tasks"
  query_scope = "COLLECTION_GROUP"

  fields {
    field_path = "state"
    order      = "ASCENDING"
  }
  fields {
    field_path = "postponedUntil"
    order      = "ASCENDING"
  }
}

# "Projects list"
resource "google_firestore_index" "projects_list" {
  project     = var.project_id
  database    = google_firestore_database.this.name
  collection  = "projects"
  query_scope = "COLLECTION"

  fields {
    field_path = "ownerUid"
    order      = "ASCENDING"
  }
  fields {
    field_path = "status"
    order      = "ASCENDING"
  }
  fields {
    field_path = "updatedAt"
    order      = "DESCENDING"
  }
}

# "Autonomous candidates"
resource "google_firestore_index" "projects_autonomous_candidates" {
  project     = var.project_id
  database    = google_firestore_database.this.name
  collection  = "projects"
  query_scope = "COLLECTION"

  fields {
    field_path = "status"
    order      = "ASCENDING"
  }
  fields {
    field_path = "lastAutonomousRunAt"
    order      = "ASCENDING"
  }
}

# "Stuck runs"
resource "google_firestore_index" "runs_stuck" {
  project     = var.project_id
  database    = google_firestore_database.this.name
  collection  = "autonomous_runs"
  query_scope = "COLLECTION"

  fields {
    field_path = "status"
    order      = "ASCENDING"
  }
  fields {
    field_path = "leaseExpiresAt"
    order      = "ASCENDING"
  }
}

# --- Single-field index overrides -------------------------------------------------------
# Automatic single-field indexes are COLLECTION-scoped only, so a collection-group query
# on one field needs an explicit override. `google_firestore_index` requires at least two
# fields, which is why these are `google_firestore_field` resources instead.

# "Task by bare id" — `GET /api/tasks/{id}` addresses a task without naming its project,
# and a collection-group query cannot filter a document key by its trailing segment.
resource "google_firestore_field" "tasks_id_collection_group" {
  project    = var.project_id
  database   = google_firestore_database.this.name
  collection = "tasks"
  field      = "id"

  index_config {
    indexes {
      order       = "ASCENDING"
      query_scope = "COLLECTION_GROUP"
    }
  }
}

# "Session by task" — resolves a task to its session without storing the reverse pointer
# twice. Used from M2.
resource "google_firestore_field" "sessions_task_id" {
  project    = var.project_id
  database   = google_firestore_database.this.name
  collection = "sessions"
  field      = "taskId"

  index_config {
    indexes {
      order       = "ASCENDING"
      query_scope = "COLLECTION_GROUP"
    }
  }
}

# "Session by project" — the fallback path in `SessionService.get_or_create_intake`, for a
# project created before `projects/{id}.intakeSessionId` existed. One indexed filter, with
# `taskId` and `appName` checked in Python: a second `where` would make this a composite
# collection-group query, which the emulator answers happily and Firestore refuses. Added
# at M3.
resource "google_firestore_field" "sessions_project_id" {
  project    = var.project_id
  database   = google_firestore_database.this.name
  collection = "sessions"
  field      = "projectId"

  index_config {
    indexes {
      order       = "ASCENDING"
      query_scope = "COLLECTION_GROUP"
    }
  }
}

# --- TTL policies -----------------------------------------------------------------------
# docs/02-data-model.md#retention.

# A Firestore TTL only fires on documents where the field is actually set, so a turn that
# never reaches a terminal state never expires. The SIGTERM drain and the ledger sweep
# both set `endedAt` when they mark a turn failed, and that is what keeps this from
# leaking.
resource "google_firestore_field" "turns_ttl" {
  project    = var.project_id
  database   = google_firestore_database.this.name
  collection = "turns"
  field      = "endedAt"

  ttl_config {}
}

# `updatedAt` is touched at every step boundary, so a run that is making progress keeps
# resetting its own clock.
resource "google_firestore_field" "runs_ttl" {
  project    = var.project_id
  database   = google_firestore_database.this.name
  collection = "autonomous_runs"
  field      = "updatedAt"

  ttl_config {}
}

# Idempotency records carry their own `expiresAt`; 24 h of retention is enough to cover a
# client's retry window (docs/04-api-contract.md).
resource "google_firestore_field" "idempotency_ttl" {
  project    = var.project_id
  database   = google_firestore_database.this.name
  collection = "idempotency"
  field      = "expiresAt"

  ttl_config {}
}
