output "function_uri" {
  value       = google_cloudfunctions2_function.block_password_signup.url
  description = "Passed to modules/identity as the beforeCreate blocking-function trigger."
}
