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
- **A quota project on those credentials.** User-based Application Default Credentials
  carry no project, and `identitytoolkit.googleapis.com` refuses requests without one:

  ```
  Error 403: Your application is authenticating by using local Application Default
  Credentials. The identitytoolkit.googleapis.com API requires a quota project, which is
  not set by default.
  ```

  `envs/*/versions.tf` sets `user_project_override = true` and `billing_project` on the
  provider, which makes Terraform send the `X-Goog-User-Project` header and is normally
  enough on its own. If a `gcloud` command outside Terraform hits the same error, set it
  on the credentials too:

  ```bash
  gcloud auth application-default set-quota-project coach-dev
  ```

  Either route needs `serviceusage.services.use` on the project; Owner has it.
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
5. Keep the **client secret** somewhere safe for step 4. It never goes in a tfvars file.

   **Do not run `gcloud secrets create` for it here.** Terraform owns the container
   (`google_secret_manager_secret.oauth_client_secret`), so creating it by hand first
   makes the apply fail with `AlreadyExists`. Step 4 adds the *value* to the container
   Terraform made.

   Terraform reads the value back with `data.google_secret_manager_secret_version_access`,
   so the secret is never in the configuration — only in state.

   If you already created it by hand, either delete it
   (`gcloud secrets delete oauth-client-secret --project=coach-dev`) or adopt it:

   ```bash
   terraform import -var-file=dev.tfvars \
     google_secret_manager_secret.oauth_client_secret \
     projects/coach-dev/secrets/oauth-client-secret
   ```

   The same applies to `youtube-api-key` and `gemini-api-key`.

---

## 3. First apply

```bash
cd infra/terraform/envs/dev
terraform init
terraform apply -var-file=dev.tfvars
```

**Expect to run this twice on a brand-new project**, for two independent reasons:

- The Identity Platform `authorized_domains` list and `TASKS_TARGET_URL` need the Cloud
  Run URL, which does not exist until the service is created. The `.tfvars` file ships
  with placeholder values for exactly this reason.
- Every secret is created with a **placeholder version**, because the things that consume
  them — the Identity Platform provider config, and Cloud Run's
  `secret_key_ref { version = "latest" }` — fail outright against a secret that has no
  versions. Step 4 replaces the placeholders, and the second apply picks them up.

The apply also sleeps 60 s after enabling APIs, because an API can still reject calls for
up to a minute after enablement is accepted. That wait is why the first apply is slower
than later ones.

---

## 4. Replace the placeholder secret values (HUMAN, one command each)

Terraform creates each container and a placeholder first version; the real values are set
out of band and Terraform never reads them back into the configuration.

```bash
printf '%s' "$YOUTUBE_API_KEY" | gcloud secrets versions add youtube-api-key \
  --project=coach-dev --data-file=-

# The client secret from step 2. Identity Platform's Google provider needs the real value.
printf '%s' "$OAUTH_CLIENT_SECRET" | gcloud secrets versions add oauth-client-secret \
  --project=coach-dev --data-file=-

# dev only — production authenticates to Vertex AI as the service account and has no key
printf '%s' "$GEMINI_API_KEY" | gcloud secrets versions add gemini-api-key \
  --project=coach-dev --data-file=-
```

Check that none of them is still the placeholder before moving on:

```bash
for s in youtube-api-key oauth-client-secret gemini-api-key; do
  printf '%-22s %s\n' "$s" \
    "$(gcloud secrets versions access latest --secret="$s" --project=coach-dev | head -c 12)"
done
```

Anything printing `REPLACE_ME_V` has not been seeded.

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
