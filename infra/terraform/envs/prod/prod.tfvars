# coach-prod. Committed on purpose: these are project ids and the public OAuth client id.
# The client *secret* lives in Secret Manager, never here (see .gitignore).

project_id        = "coach-prod"
region            = "us-central1"
github_repository = "REPLACE_ME/self-study-coach"

# RUNBOOK.md step 2 produces this.
oauth_client_id = "REPLACE_ME.apps.googleusercontent.com"

# On a brand-new project the Cloud Run URL is not known yet. Apply once with a placeholder,
# then replace both lists with the real URL from `terraform output service_url` and apply
# again. RUNBOOK.md walks through it.
authorized_domains      = ["coach-api-REPLACE_ME.us-central1.run.app"]
authorized_domains_urls = ["https://coach-api-REPLACE_ME.us-central1.run.app"]
service_url_hint        = "https://coach-api-REPLACE_ME.us-central1.run.app"

notification_channels = []
