# Environment bootstrap runbook

Everything in this stack is `terraform apply` from zero **except the steps marked
HUMAN below**. Those are the caveat on the M0 exit criterion in
[docs/09-roadmap.md](../../docs/09-roadmap.md), and they belong here rather than in
tribal memory.

Do this once per environment (`coach-dev`, then `coach-prod`).

> **Nothing in this file was executed before it was written.** The Terraform in this
> directory is authored and checked locally — `terraform fmt -check`, `terraform
> validate`, and `tflint` all pass — but those are weaker than they look.
>
> In particular **`terraform validate` is not a substitute for `terraform plan`.** Several
> failure modes are invisible to it and only appear when the graph is walked: a root
> output carrying a value the provider marks sensitive, a resource that already exists
> and needs importing, and an API that is enabled but not yet propagated. Every one of
> those was hit on the first real run through this runbook. `plan` needs credentials and
> a state backend, so PR CI cannot run it — which is what the nightly Terraform drift
> check in [docs/08-testing.md](../../docs/08-testing.md#ci-wiring) is for.
>
> Treat the step ordering below as reasoned and now partly exercised, not as
> battle-tested end to end.

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

### Enable the APIs up front (recommended)

Terraform enables every API it needs, and waits 60 s afterwards for the enablement to
propagate. That is usually enough, but API enablement is eventually consistent and a busy
project can take longer, which shows up as:

```
Error 403: <Some> API has not been used in project X before or it is disabled.
```

Enabling them yourself first removes the race entirely — the `google_project_service`
resources then just record state:

```bash
gcloud services enable \
  run.googleapis.com firestore.googleapis.com cloudtasks.googleapis.com \
  cloudscheduler.googleapis.com aiplatform.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com \
  storage.googleapis.com identitytoolkit.googleapis.com \
  iam.googleapis.com iamcredentials.googleapis.com \
  cloudresourcemanager.googleapis.com sts.googleapis.com \
  youtube.googleapis.com monitoring.googleapis.com logging.googleapis.com \
  cloudtrace.googleapis.com \
  --project=coach-dev
```

That list is the `local.services` list in `envs/<env>/main.tf`; if you add an API there,
add it here.

**If you hit the error anyway, just run `terraform apply` again.** Everything in this
stack is idempotent, the enablement has been accepted by then, and the second run picks
up where the first stopped. That is a normal part of a first apply, not a sign anything
is broken.
---

## 1. Enable Identity Platform via the Cloud Marketplace (HUMAN)

**Why this is not Terraformable:** the provider documentation for
`google_identity_platform_default_supported_idp_config` states the product must be enabled
in the marketplace before the resource can be used.

1. Open <https://console.cloud.google.com/customer-identity> for the project.
2. Click **Enable Identity Platform**.

**Then adopt the config it just created.** `google_identity_platform_config` is a
singleton, and enabling the product *is* what creates it — so Terraform tries to create a
thing that already exists and stops with:

```
Error 400: INVALID_PROJECT_ID : Identity Platform has already been enabled for this project.
```

This is not a failure so much as a handover. Import it once, before the first apply:

```bash
cd infra/terraform/envs/dev
terraform init
terraform import -var-file=dev.tfvars \
  module.identity.google_identity_platform_config.this YOUR_PROJECT_ID
```

The import id for this resource is just the project id.

After the import, Terraform manages the existing config and reconciles
`authorized_domains` into it instead of trying to create a second one. If you hit the
error before importing, nothing is damaged — import and re-apply.

**Still worth testing on the next environment:** `google_project_service.identitytoolkit`,
which this stack already enables, may be sufficient on its own — in which case the
marketplace click goes away and the import with it, because Terraform would be creating
the config rather than adopting one. On a fresh project, try the apply *before* doing any
of the above; if `google_identity_platform_config.this` creates cleanly, delete this
section and say so in
[docs/07-infra-deploy.md](../../docs/07-infra-deploy.md#manual-bootstrap-steps-two-both-one-time-per-environment).

---

## 2. Create the OAuth 2.0 web client and consent screen (HUMAN)

**Why this is not Terraformable:** `google_oauth_client` has not stabilised. Re-check
whether it has before accepting this permanently.

1. **APIs & Services → OAuth consent screen.** External, fill in the app name, support
   email, and developer contact. Add the scopes `openid`, `email`, `profile`.
2. **APIs & Services → Credentials → Create credentials → OAuth client ID**, type
   **Web application**.
3. Authorized origins and redirect URIs. **These are two different values, and the
   redirect URI is not the Cloud Run URL.** Getting this wrong produces a Google popup
   reading "Access blocked: This app's request is invalid", which names neither field.

   | Field | Value | Why |
   | --- | --- | --- |
   | Authorized **JavaScript origins** | `https://<cloud-run-host>` | Where the SPA is served from |
   | Authorized **redirect URIs** | `https://<auth-domain>/__/auth/handler` | Where Google sends the user back |

   `signInWithPopup` does not return to your app directly. It returns to Identity
   Platform's own handler on the **auth domain** — `<something>.firebaseapp.com`, the
   value of the `identity_auth_domain` output — which then posts the result back to the
   page that opened the popup. Google validates the redirect against the OAuth client, so
   it is the handler URL that has to be registered, not the app's.

   Both values need resources that do not exist on a brand-new project, so leave these
   empty for now and fill them in at step 5.
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

### 3a. Create the registry, then put an image in it

`var.image` has no default and Cloud Run cannot start without a real image, but the
Artifact Registry repository that image lives in is created by this same stack. Break the
loop with a targeted apply — this creates the repository and the API enablement it depends
on, and nothing else:

```bash
cd infra/terraform/envs/dev
terraform init
terraform apply -var-file=dev.tfvars -target=google_artifact_registry_repository.images
```

Then build and push from the repo root:

```bash
PROJECT=YOUR_PROJECT_ID
IMAGE="us-central1-docker.pkg.dev/$PROJECT/coach/coach-api:bootstrap"

gcloud auth configure-docker us-central1-docker.pkg.dev --quiet
docker build -t "$IMAGE" .
docker push "$IMAGE"
```

The SPA in this bootstrap image is built without the Identity Platform values, which do
not exist yet — sign-in will not work in it. That is fine and expected: it exists so the
Cloud Run service can start and hand you a URL. The deploy workflow rebuilds the image
properly with those values once step 6 has wired them up.

### 3b. Apply the rest

```bash
terraform apply -var-file=dev.tfvars -var="image=$IMAGE"
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
terraform output -raw service_url;          echo   # https://coach-api-abc123-uc.a.run.app
terraform output -raw identity_auth_domain; echo   # coach-dev-xyz.firebaseapp.com
```

Then, with those two values:

1. Update `dev.tfvars` with the **Cloud Run host**:
   - `authorized_domains` → `["coach-api-abc123-uc.a.run.app"]` (bare hostname)
   - `authorized_domains_urls` → `["https://coach-api-abc123-uc.a.run.app"]`
   - `service_url_hint` → `"https://coach-api-abc123-uc.a.run.app"`

2. Update the **OAuth client** (APIs & Services → Credentials → your web client). Two
   fields, two different values — see the table in step 2.3:

   - Authorized **JavaScript origins**: `https://coach-api-abc123-uc.a.run.app`
   - Authorized **redirect URIs**: `https://coach-dev-xyz.firebaseapp.com/__/auth/handler`

   The redirect URI is built from `identity_auth_domain`, **not** from the service URL.
   Using the service URL here is the single most likely reason sign-in fails with
   "Access blocked: This app's request is invalid".

   Note that there are now *three* separate allow-lists in play, and they take different
   forms — Identity Platform's `authorized_domains` (bare hostnames, in tfvars), the OAuth
   client's JS origins (full origins), and its redirect URIs (a full URL with a path).
   All three must be right; each one fails differently.

3. `terraform apply -var-file=dev.tfvars -var="image=$IMAGE"` again (the same
   `$IMAGE` from step 3a; `var.image` has no default).

   OAuth client changes take effect within a minute or so, but Google caches them — if
   sign-in still fails immediately after editing, retry in a fresh tab before assuming the
   value is wrong.

---

## 6. Wire up GitHub Actions

```bash
terraform output -raw workload_identity_provider; echo
terraform output -raw deployer_service_account;   echo
terraform output -raw identity_api_key;           echo
terraform output -raw identity_auth_domain;       echo
```

`-raw` prints the bare string with no quotes, which is what these fields want pasted
into them.

Set these as **repository variables** (not secrets — all four are public values, and the
deploy workflow reads them from `vars.*`):

| Variable | From |
| --- | --- |
| `GCP_PROJECT_ID` | `coach-dev` |
| `WIF_PROVIDER` | `terraform output -raw workload_identity_provider` |
| `DEPLOYER_SERVICE_ACCOUNT` | `terraform output -raw deployer_service_account` |
| `IDENTITY_API_KEY` | `terraform output -raw identity_api_key` |
| `IDENTITY_AUTH_DOMAIN` | `terraform output -raw identity_auth_domain` |

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
served from one origin, and the SPA catch-all is not shadowing `/api/*`.

If `/healthz` fails on the very first request, try it once more before investigating — the
service has just started and the first caller can beat it to the door. Cloud Run's startup
probe hits `/healthz`, so an apply that succeeded is itself evidence the endpoint answers.

### Sign-in — the other half of the exit criterion

Open the service URL and sign in with Google. Seeing your own email on the page is what M0
is for.

The bootstrap image from step 3a **cannot** do this: its SPA was built with no Identity
Platform values. Rebuild with them first:

```bash
API_KEY=$(terraform output -raw identity_api_key)
AUTH_DOMAIN=$(terraform output -raw identity_auth_domain)
IMAGE="us-central1-docker.pkg.dev/$PROJECT/coach/coach-api:$(git rev-parse --short HEAD)"

docker build \
  --build-arg VITE_AUTH_MODE=identity-platform \
  --build-arg VITE_IDENTITY_API_KEY="$API_KEY" \
  --build-arg VITE_IDENTITY_AUTH_DOMAIN="$AUTH_DOMAIN" \
  --build-arg VITE_IDENTITY_PROJECT_ID="$PROJECT" \
  -t "$IMAGE" .          # from the repo root
docker push "$IMAGE"
terraform apply -var-file=dev.tfvars -var="image=$IMAGE"
```

If the popup says **"Access blocked: This app's request is invalid"**, the OAuth client's
**redirect URI** is wrong or missing. It must be
`https://<identity_auth_domain>/__/auth/handler` — not the Cloud Run URL. See step 5.2.
Google hides the specific code behind a "Learn more" or an expandable detail on that page;
`redirect_uri_mismatch` confirms the diagnosis.

Two other things that block the popup rather than letting it work:

- **The consent screen is still in Testing.** Add your account under *Audience → Test
  users*, or publish the app.
- **The client secret is still the placeholder** (step 4). That breaks the token exchange
  rather than the redirect, so it tends to surface a step later — but check it before
  chasing anything exotic.

---

## Teardown (dev only)

```bash
terraform destroy -var-file=dev.tfvars
```

`prod` sets `deletion_protection`, Firestore delete protection, and non-force-destroyable
buckets, so a `destroy` there fails on purpose. Turning those off is a deliberate,
reviewed change rather than a flag to flip in passing.
