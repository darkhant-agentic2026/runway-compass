output "queue_id" {
  value       = google_cloud_tasks_queue.autonomous_runs.id
  description = "For the API's TASKS_QUEUE setting."
}

output "queue_name" {
  value = google_cloud_tasks_queue.autonomous_runs.name
}

output "scheduler_job_name" {
  value = google_cloud_scheduler_job.tick.name
}
