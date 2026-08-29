output "function_uri" {
  value       = google_cloudfunctions_function.block_password_signup.https_trigger_url
  description = "Passed to modules/identity as the beforeCreate blocking-function trigger."
}
