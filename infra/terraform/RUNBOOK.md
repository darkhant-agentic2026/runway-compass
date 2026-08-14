# Environment bootstrap runbook

Everything in this stack is `terraform apply` from zero **except the steps marked
HUMAN below**. Those are the caveat on the M0 exit criterion in
[docs/09-roadmap.md](../../docs/09-roadmap.md), and they belong here rather than in
tribal memory.

Do this once per environment (`coach-dev`, then `coach-prod`).

> **Nothing in this file has been executed.** The Terraform in this directory is authored
> and validated locally — `terraform fmt -check`, `terraform validate`, and `tflint` all
> pass — but no `apply` has ever run against a GCP project, and no `gcloud auth` has been
> performed. Treat the step ordering below as carefully reasoned, not as battle-tested.

---

## 0. Prerequisites (HUMAN)

- A GCP project (`coach-dev` / `coach-prod`) with **billing enabled**.
  `google_identity_platform_config` requires a billing-enabled project and will fail
  without it.
- `gcloud auth login` and `gcloud auth application-default login` as a principal with
  Owner (or at least Project IAM Admin + Service Account Admin + the service-specific
  admin roles) on that project.
- A GCS state bucket, created by hand — a state bucket cannot be managed by the state it
  holds:

  ```bash
  gsutil mb -p coach-dev -l us-central1 gs://coach-dev-tfstate
  gsutil versioning set on gs://coach-dev-tfstate
  ```

  The bucket name is hard-coded in `envs/dev/backend.tf`; change both together.

---

## 1. Enable Identity Platform via the Cloud Marketplace (HUMAN)

**Why this is not Terraformable:** the provider documentation for
`google_identity_platform_default_supported_idp_config` states the product must be enabled
in the marketplace before the resource can be used.

1. Open <https://console.cloud.google.com/customer-identity> for the project.
2. Click **Enable Identity Platform**.

**Worth testing first:** it is possible that `google_project_service.identitytoolkit`,
which this stack already enables, is sufficient on its own — in which case this step
disappears. Try `terraform apply` without doing it; if
`google_identity_platform_config.this` succeeds, delete this section and say so in
[docs/07-infra-deploy.md](../../docs/07-infra-deploy.md#manual-bootstrap-steps-two-both-one-time-per-environment).
That check is cheap and has never been run.

---

## 2. Create the OAuth 2.0 web client and consent screen (HUMAN)

**Why this is not Terraformable:** `google_oauth_client` has not stabilised. Re-check
whether it has before accepting this permanently.

1. **APIs & Services → OAuth consent screen.** External, fill in the app name, support
   email, and developer contact. Add the scopes `openid`, `email`, `profile`.
2. **APIs & Services → Credentials → Create credentials → OAuth client ID**, type
   **Web application**.
3. Authorized JavaScript origins and redirect URIs: the Cloud Run service URL. On a
   brand-new project that URL does not exist yet — leave these empty for now and come back
   in step 5.
4. Copy the **client ID** into `envs/<env>/<env>.tfvars` as `oauth_client_id`. It is a
   public value and is committed on purpose.
5. Put the **client secret** into Secret Manager. It never goes in a tfvars file:

   ```bash
   gcloud secrets create oauth-client-secret --project=coach-dev --replication-policy=automatic
   printf '%s' 'THE_SECRET' | gcloud secrets versions add oauth-client-secret \
     --project=coach-dev --data-file=-
   ```

   Terraform reads it back with `data.google_secret_manager_secret_version_access`, so the
   secret is never in the configuration and only ever in state.

---

## 3. Seed the other secrets (HUMAN, one command each)

Terraform owns the secret *containers*; the values are set out of band.

```bash
printf '%s' "$YOUTUBE_API_KEY" | gcloud secrets versions add youtube-api-key \
  --project=coach-dev --data-file=-

# dev only — production authenticates to Vertex AI as the service account and has no key
printf '%s' "$GEMINI_API_KEY" | gcloud secrets versions add gemini-api-key \
  --project=coach-dev --data-file=-
```

If the secret does not exist yet, run `terraform apply` once first: it creates the
containers, and `gcloud secrets versions add` then has something to add to.

---

## 4. First apply

```bash
cd infra/terraform/envs/dev
terraform init
terraform apply -var-file=dev.tfvars
```

**Expect to run this twice on a brand-new project.** The Identity Platform
`authorized_domains` list and `TASKS_TARGET_URL` need the Cloud Run URL, which does not
exist until the service is created. The `.tfvars` file ships with placeholder values for
exactly this reason.

---

## 5. Reconcile the URL and re-apply

```bash
terraform output service_url     # e.g. https://coach-api-abc123-uc.a.run.app
```

Then, with that value:

1. Update `dev.tfvars`:
   - `authorized_domains` → `["coach-api-abc123-uc.a.run.app"]` (bare hostname)
   - `authorized_domains_urls` → `["https://coach-api-abc123-uc.a.run.app"]`
   - `service_url_hint` → `"https://coach-api-abc123-uc.a.run.app"`
2. Add the same URL to the OAuth client's authorized origins and redirect URIs (step 2.3).
3. `terraform apply -var-file=dev.tfvars` again.

---

## 6. Wire up GitHub Actions

```bash
terraform output workload_identity_provider
terraform output deployer_service_account
terraform output identity_api_key
terraform output identity_auth_domain
```

Set these as **repository variables** (not secrets — all four are public values, and the
deploy workflow reads them from `vars.*`):

| Variable | From |
| --- | --- |
| `GCP_PROJECT_ID` | `coach-dev` |
| `WIF_PROVIDER` | `terraform output workload_identity_provider` |
| `DEPLOYER_SERVICE_ACCOUNT` | `terraform output deployer_service_account` |
| `IDENTITY_API_KEY` | `terraform output identity_api_key` |
| `IDENTITY_AUTH_DOMAIN` | `terraform output identity_auth_domain` |

Create a GitHub Environment named `prod` with a required-reviewer protection rule; the
deploy workflow's `environment:` key is what makes it take effect.

---

## 7. Verify

```bash
curl -fsS "$(terraform output -raw service_url)/healthz"   # {"status":"ok"}
curl -fsS "$(terraform output -raw service_url)/readyz"    # {"status":"ok"} — proves Firestore
curl -si  "$(terraform output -raw service_url)/api/me" | head -1   # HTTP/2 401
curl -fsS "$(terraform output -raw service_url)/" | head -1         # <!doctype html>
```

The last two together are the M0 exit criterion's structural half: the SPA and the API are
served from one origin, and the SPA catch-all is not shadowing `/api/*`. The other half —
a signed-in user seeing their email — needs a browser and steps 1 and 2 above.

---

## Teardown (dev only)

```bash
terraform destroy -var-file=dev.tfvars
```

`prod` sets `deletion_protection`, Firestore delete protection, and non-force-destroyable
buckets, so a `destroy` there fails on purpose. Turning those off is a deliberate,
reviewed change rather than a flag to flip in passing.
