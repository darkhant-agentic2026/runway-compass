output "database_name" {
  description = "The database id, for the API's FIRESTORE_DATABASE setting."
  value       = google_firestore_database.this.name
}

output "database_id" {
  description = "Fully qualified database resource id."
  value       = google_firestore_database.this.id
}
