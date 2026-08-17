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

# At the design value of 1 from M2 onward, and this is a correctness setting rather than a
# cost one: a scaled-to-zero instance can be reaped in the middle of a detached generation
# task, which is the exact failure the disconnect guarantee exists to prevent
# (docs/04-api-contract.md#surviving-client-disconnects). It surfaces as an occasional
# lost stream, which is a miserable thing to trace back to a scaling number.
#
# It was 0 through M1, when there was no streaming to lose and an idle environment billed
# essentially nothing. That trade is no longer available.
min_instances = 1
