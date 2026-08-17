terraform {
  required_version = "~> 1.15"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 7.0"
    }
    time = {
      source  = "hashicorp/time"
      version = "~> 0.13"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region

  # Required for Identity Platform. `identitytoolkit.googleapis.com` refuses a request
  # whose credentials carry no quota project, which is the normal state of user-based
  # Application Default Credentials:
  #
  #   Error 403: Your application is authenticating by using local Application Default
  #   Credentials. The identitytoolkit.googleapis.com API requires a quota project.
  #
  # These two settings make the provider send `X-Goog-User-Project`, which supplies one.
  # Doing it here rather than telling every operator to run
  # `gcloud auth application-default set-quota-project` means the fix travels with the
  # configuration instead of living in each person's local ADC state — and it is a no-op
  # for the service-account credentials CI uses, which already carry a project.
  #
  # The caller needs `serviceusage.services.use` on the billing project. Owner has it.
  user_project_override = true
  billing_project       = var.project_id
}
