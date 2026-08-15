# coach-dev. Committed on purpose: these are project ids and the public OAuth client id.
# The client *secret* lives in Secret Manager, never here (see .gitignore).

project_id        = "verted-ai-veo3-88760"
region            = "us-central1"
github_repository = "darkhant-agentic2026/self-study-coach"

# RUNBOOK.md step 2 produces this.
oauth_client_id = "896342225227-atua9h8bfr03dv5u8eb915rl15tloih1.apps.googleusercontent.com"

# On a brand-new project the Cloud Run URL is not known yet. Apply once with a placeholder,
# then replace both lists with the real URL from `terraform output service_url` and apply
# again. RUNBOOK.md walks through it.
authorized_domains      = ["coach-api-pwh2ad5axa-uc.a.run.app"]
authorized_domains_urls = ["https://coach-api-pwh2ad5axa-uc.a.run.app"]
service_url_hint        = "https://coach-api-pwh2ad5axa-uc.a.run.app"

notification_channels = []
