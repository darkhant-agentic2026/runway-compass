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

# The variable's default is 4 (dev's value); prod was hardcoded at 10 in main.tf before
# main.tf started reading this variable instead, so this preserves that value rather than
# silently dropping to the default.
max_instances = 10

# Deliberately unset, unlike dev, which pins `vertex_location = "global"` because
# `gemini-3.7-flash` was not served in `us-central1` there (probed 2026-08-17).
#
# Do not copy that line across on faith. Two reasons:
#
# 1. Availability is per project and moves as models roll out, so re-run the probe in
#    RUNBOOK.md section 8.5 against *this* project rather than inheriting dev's answer.
# 2. `global` routes the request to any region Google chooses, which is a **data
#    residency** decision rather than a configuration detail. Dev handles no real learner
#    work; production does. If residency matters, the answer is a region that serves the
#    model, not `global` — and if none does, the model choice itself has to change.
#
# vertex_location = "global"
