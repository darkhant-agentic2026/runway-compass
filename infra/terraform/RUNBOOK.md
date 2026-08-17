# Environment bootstrap runbook

Everything in this stack is `terraform apply` from zero **except the steps marked
HUMAN below**. Those are the caveat on the M0 exit criterion in
[docs/09-roadmap.md](../../docs/09-roadmap.md), and they belong here rather than in
tribal memory.

Do this once per environment (`coach-dev`, then `coach-prod`).

> **Status: steps 0–7 have now been run end to end against a real dev project**, and the
> exit criterion reached — a signed-in user seeing their own email on a deployed Cloud Run
> URL. Everything below reflects what actually happened, including seven corrections made
> while doing it. Still unexercised: the `prod` environment, the teardown section, and
> whether `terraform apply` reproduces an environment from scratch.
>
> Worth keeping in mind, because it shaped most of those corrections: **`terraform
> validate` is not a substitute for `terraform plan`.** Several failure modes are
> invisible to it and appear only when the graph is walked — a root output carrying a
> value the provider marks sensitive, a resource that already exists and needs importing,
> an API enabled but not yet propagated. `plan` needs credentials and a state backend, so
> PR CI cannot run it, which is what the nightly Terraform drift check in
> [docs/08-testing.md](../../docs/08-testing.md#ci-wiring) is for.
>
> The other recurring shape: **anything this runbook tells you to create by hand,
> Terraform will then try to create again.** That produced two of the seven (the OAuth
> client secret container, and the Identity Platform config). If you add a manual step,
> check it against the resource list first.

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
  # GCS bucket names are globally unique, so pick one nobody has taken.
  gsutil mb -p coach-dev -l us-central1 gs://YOUR-UNIQUE-tfstate
  gsutil versioning set on gs://YOUR-UNIQUE-tfstate
  ```

  Put that name in **`envs/dev/backend.hcl`**, not in `backend.tf`:

  ```hcl
  bucket = "YOUR-UNIQUE-tfstate"
  ```

  A backend block cannot reference variables — it is configured before the rest of the
  configuration exists — so this is supplied at init time with `-backend-config`. It also
  cannot come from `dev.tfvars`, because the GCS backend rejects keys it does not
  recognise and that file is full of them. Every `terraform init` below therefore reads
  `backend.hcl`, and `backend.tf` stays generic.

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
terraform init -backend-config=backend.hcl
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

> **Already initialised against a hard-coded bucket?** Earlier revisions of this repo put
> the bucket in `backend.tf`. Moving it to `backend.hcl` changes how the backend is
> configured but not *which* bucket is used, so re-initialise in place — no state is
> moved:
>
> ```bash
> terraform init -reconfigure -backend-config=backend.hcl
> ```


### 3a. Create the registry, then put an image in it

`var.image` has no default and Cloud Run cannot start without a real image, but the
Artifact Registry repository that image lives in is created by this same stack. Break the
loop with a targeted apply — this creates the repository and the API enablement it depends
on, and nothing else:

```bash
cd infra/terraform/envs/dev
terraform init -backend-config=backend.hcl
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

**Use `printf '%s'`, not `echo`.** `echo` appends a newline, and a client secret with a
trailing `\n` is rejected by Google as `invalid_client` — the same error you get from not
seeding it at all, which makes it an expensive mistake to diagnose.

Check the values before moving on. This prints a prefix and a byte count, because both
failure modes are invisible otherwise:

```bash
for s in youtube-api-key oauth-client-secret gemini-api-key; do
  v=$(gcloud secrets versions access latest --secret="$s" --project=coach-dev 2>/dev/null)
  printf '%-22s %-14s %s bytes\n' "$s" "$(printf '%s' "$v" | head -c 12)" \
    "$(gcloud secrets versions access latest --secret="$s" --project=coach-dev | wc -c)"
done
```

- A prefix of `REPLACE_ME_V` means it has not been seeded.
- A Google OAuth client secret looks like `GOCSPX-…` and is 35 bytes. **36 means a
  trailing newline** — re-add it with `printf '%s'`.

Then re-apply, so the Identity Platform provider config picks up the new value:

```bash
terraform apply -var-file=dev.tfvars -var="image=$IMAGE"
```

The data source reads `latest`, so a new version is enough; nothing needs rebuilding,
because this is server-side configuration and not part of the image.

**Symptom if you skip this:** sign-in gets all the way through Google's consent screen and
then fails as the popup closes, with
`auth/invalid-credential … error=invalid_client&error_description=The provided client
secret is invalid.` The redirect URI in that message being correct is a good sign — it
means everything up to the token exchange is right.

> **Then wait before you debug.** Identity Platform caches the provider configuration, and
> after the apply the *old* secret keeps being used for several minutes. The failure is
> byte-identical to a genuinely wrong secret, so there is nothing in the error to tell you
> which one you are looking at.
>
> This was observed on the first real run: the secret was correct, `terraform plan`
> reported "No changes" — meaning the value had already been pushed — and sign-in still
> failed with `invalid_client` for some minutes before starting to work on its own with no
> further changes.
>
> So: once `terraform plan` says "No changes" and the client id and secret belong to the
> same OAuth client, **stop and retry in a few minutes in a fresh tab.** Continuing to
> rotate secrets at that point only adds variables.

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

These are **variables, not secrets.** The deploy workflow contains no `secrets.*`
reference at all — authentication is Workload Identity Federation, so there is no key to
store, and the other four values are public by design.

| Variable | From |
| --- | --- |
| `GCP_PROJECT_ID` | the project id, e.g. `coach-dev` |
| `WIF_PROVIDER` | `terraform output -raw workload_identity_provider` |
| `DEPLOYER_SERVICE_ACCOUNT` | `terraform output -raw deployer_service_account` |
| `IDENTITY_API_KEY` | `terraform output -raw identity_api_key` |
| `IDENTITY_AUTH_DOMAIN` | `terraform output -raw identity_auth_domain` |

### Repository or environment scope?

**All five values differ between `coach-dev` and `coach-prod`** — different projects,
different WIF pools, different Identity Platform configs. A repository variable holds one
value, so it cannot serve both.

`vars.X` resolves environment scope first and falls back to repository scope, which gives
two workable arrangements:

- **One environment only (now).** Put the dev values at **repository** scope and stop.
  This is enough to deploy dev and is what the next step assumes.
- **Both environments (before any prod deploy).** Create GitHub Environments named `dev`
  and `prod` under *Settings → Environments*, and set all five variables **inside each**.
  Leaving the dev values at repository scope while adding a `prod` environment also works,
  but then prod's values live in one place and dev's in another, which is easy to
  misread later.

**Do not deploy prod with only repository-scoped variables.** It would authenticate to the
dev project's WIF pool and deploy the prod tag into `coach-dev` — a failure that succeeds
loudly in the wrong place.

### Deployment protection for prod

*Settings → Environments → prod → **Deployment protection rules** → tick **Required
reviewers***, and add yourself or a team. The workflow's `environment:` key is what makes
it apply; a run targeting `prod` then pauses until someone approves it.

**If you cannot find that section, check the repository's visibility and plan.**
Environment deployment protection rules are available on public repositories on any plan,
but on **private** repositories they require GitHub Pro, Team, or Enterprise. On a private
Free repository the Environments page exists and accepts variables, but the protection
rules are simply not offered. That is a billing boundary, not a setting you have missed.

If protection rules are unavailable, prod is still gated — just differently. The workflow
only selects prod for a `v*` tag or a manual dispatch, so nothing reaches prod without a
deliberate act. To harden it further without paying: protect the tag pattern under
*Settings → Rules → Rulesets* (tag ruleset, restrict who can create `v*`), which limits
who can trigger a prod deploy at all.

Note that GitHub creates an environment implicitly the first time a workflow references
one, so a `dev` environment will appear on its own after the first deploy. An implicitly
created environment has **no** protection rules — the existence of the environment is not
the gate; the rules on it are.

### What actually triggers a deploy

| Event | Environment deployed |
| --- | --- |
| Push / merge to `main` | **`dev`** |
| Push of a `v*` tag | `prod` |
| Manual *Run workflow* | whichever you pick (defaults to `dev`) |

So **merging to `main` deploys dev, never prod.** Promotion to prod is a separate,
deliberate act: tag a release, or dispatch the workflow by hand. That is the mapping
docs/07-infra-deploy.md#deploy-cloudrunyml specifies, and it is why prod can be left
entirely unconfigured until you actually want one.

---

## 7. Verify

```bash
./scripts/smoke.sh "$(terraform output -raw service_url)"
```

That is the same script the deploy workflow runs before it shifts any traffic, so a green
result here and a green deploy mean the same thing. It checks `/livez`, `/readyz`, that
an unauthenticated `/api/me` is a 401 in `problem+json`, and that `/` serves the SPA.

The last two together are the M0 exit criterion's structural half: the SPA and the API are
served from one origin, and the SPA catch-all is not shadowing `/api/*`.

If `/livez` fails on the very first request, try it once more before investigating — the
service has just started and the first caller can beat it to the door. Cloud Run's startup
probe hits `/livez`, so an apply that succeeded is itself evidence the endpoint answers.

**Liveness is `/livez`, not `/healthz`.** Google's frontend intercepts `/healthz` on Cloud
Run and answers it with its own HTML 404 without ever forwarding the request to the
container. Cloud Run's own probes bypass the frontend, so `/healthz` satisfies them and
the revision reports healthy; the only things that notice are an external smoke test and
the uptime check, and the latter fails silently. If you see a Google-branded 404 for a
path the app definitely serves, this is why.

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

## 8. Closing the M2 exit criterion (HUMAN)

M2's last exit criterion is *"a user can chat with the coach about an uploaded screenshot
on the deployed dev environment"* ([docs/09-roadmap.md](../../docs/09-roadmap.md)). It
cannot be closed locally, and not for want of tests: four things on that path have **no
local equivalent at all**, so nothing before this point has ever executed them.

| Unproven until this step | Why no test covers it |
| --- | --- |
| Vertex AI as the model backend | Local runs a scripted model, e2e runs `MODEL_BACKEND=stub` |
| V4 signed upload URLs | Signing needs a real IAM SignBlob call; the local store returns a fake URL |
| `GcsArtifactService` | Local uses `InMemoryArtifactService` |
| Vertex resolving the `gs://` artifact URI a turn attaches | Needs all three of the above at once |

Run steps 8.1–8.4 in order. **8.4 is the actual criterion** — the rest is getting a
current image in front of you.

### 8.1 Apply the M2 infrastructure settings

The Cloud Run settings M2 depends on are in Terraform but are not live until applied.
`cpu_idle = false` and `min_instances = 1` are correctness settings here, not tuning: a
scaled-to-zero instance can be reaped mid-generation, and request-based CPU allocation
throttles a detached generation task to near zero the moment its client disconnects.

```bash
cd infra/terraform/envs/dev
terraform init -backend-config=backend.hcl      # no-op if already initialised
terraform plan  -var-file=dev.tfvars
terraform apply -var-file=dev.tfvars
```

**Read the plan before applying.** Expect changes to `google_cloud_run_v2_service.coach_api`
only. If it proposes to replace the Firestore database, a bucket, or a service account,
stop and send me the plan — that is drift or a state problem, not this change.

Then confirm the four settings are actually live, rather than trusting the apply:

```bash
gcloud run services describe coach-api --region=us-central1 \
  --format='yaml(spec.template.metadata.annotations, spec.template.spec.containerConcurrency, spec.template.spec.timeoutSeconds)'
```

Send me the output if anything below is missing:

- `autoscaling.knative.dev/minScale: '1'`
- `run.googleapis.com/cpu-throttling: 'false'`  ← this is `cpu_idle = false`
- `run.googleapis.com/sessionAffinity: 'true'`
- `timeoutSeconds: 3600`

### 8.2 Deploy the M2 image

The branch is `m2-sessions-streaming` and is **not** pushed. Pick one:

**Either** merge it and let CI deploy (a push to `main` triggers `deploy-cloudrun.yml`,
which builds, pushes, deploys with `--no-traffic --tag=candidate`, smoke-tests the
candidate URL, and only then shifts traffic):

```bash
git push -u origin m2-sessions-streaming
gh pr create --fill --base main
# merge it, then watch:
gh run watch
```

**Or** deploy the branch directly without merging, which is the better choice if you want
to see it working before it reaches `main`:

```bash
cd infra/terraform/envs/dev
# There is no `project_id` output; the tfvars file is the source of truth for it.
PROJECT=$(grep -oP 'project_id\s*=\s*"\K[^"]+' dev.tfvars)
API_KEY=$(terraform output -raw identity_api_key)
AUTH_DOMAIN=$(terraform output -raw identity_auth_domain)
cd ../../../..                                   # back to the repo root

IMAGE="us-central1-docker.pkg.dev/$PROJECT/coach/coach-api:$(git rev-parse --short HEAD)"
docker build \
  --build-arg VITE_AUTH_MODE=identity-platform \
  --build-arg VITE_IDENTITY_API_KEY="$API_KEY" \
  --build-arg VITE_IDENTITY_AUTH_DOMAIN="$AUTH_DOMAIN" \
  --build-arg VITE_IDENTITY_PROJECT_ID="$PROJECT" \
  -t "$IMAGE" .
docker push "$IMAGE"

gcloud run deploy coach-api --region=us-central1 --image="$IMAGE" \
  --no-traffic --tag=candidate
./scripts/smoke.sh "$(./scripts/candidate_url.sh coach-api us-central1)"
gcloud run services update-traffic coach-api --region=us-central1 --to-tags candidate=100
```

`VITE_AUTH_MODE=identity-platform` matters: a build without it ships the SPA in dev-auth
mode, whose `dev:<uid>` tokens the server refuses for any `ENV` but `local`. The symptom is
a sign-in screen that appears to work and an API that 401s everything.

### 8.3 Confirm the deployed environment is not stubbed

One command, because the failure it catches is silent — a revision serving canned answers
replies, updates the board, and looks entirely healthy:

```bash
gcloud run services describe coach-api --region=us-central1 \
  --format='value(spec.template.spec.containers[0].env)' | tr ',' '\n' | grep -iE 'MODEL_BACKEND|ENV|ARTIFACT_BUCKET|UPLOAD_BUCKET'
```

Expect `ENV=dev`, `MODEL_BACKEND=vertex`, and both buckets set. If `MODEL_BACKEND` is
`stub` the container will refuse to boot (`Settings` rejects it for any non-`local` `ENV`),
so you would see a failed revision rather than bad answers — but check anyway.

### 8.4 The criterion itself

1. Open the service URL and sign in with Google.
2. Create a project, add a task, open it (click the task title on the board).
3. Type a message and send it. **Expect streamed text**, arriving progressively.
4. Attach a screenshot — drag it onto the composer, paste it, or use the paperclip — and
   send a message asking about it, e.g. *"what do you make of this screenshot?"*
5. The reply must show the coach has actually seen the image, not merely acknowledged a
   file.
6. **Then reload the page.** The whole conversation, including your attachment, must still
   be there. This is the half that proves the events were durably written.
7. **Then send a second message in the same session** (anything, e.g. *"say more"*). This
   is the step that exercises the artifact fix: a session's history is replayed to the
   model on every turn, so a broken attachment reference fails *here* rather than at
   step 4.

### 8.5 If a turn fails with `NOT_FOUND: Publisher model … was not found`

The service is fine; the model name or its location is not. Vertex model availability is
per project **and** per location, so a name that the design decided
([docs/00-overview.md](../../docs/00-overview.md#model-configuration) picks
`gemini-3.7-flash`) can still 404 here. Nothing catches this before a turn runs, because
nothing calls the model until a user does.

Probe what this project can actually reach. A `GET` on the publisher-model resource is
the same check the failing call makes, costs no tokens, and answers for both endpoints:

```bash
PROJECT=$(gcloud config get-value project)
TOKEN=$(gcloud auth print-access-token)

for LOC in us-central1 global; do
  HOST=$([ "$LOC" = global ] && echo aiplatform.googleapis.com || echo "$LOC-aiplatform.googleapis.com")
  for M in gemini-3.7-flash gemini-3-flash gemini-3-pro gemini-2.5-flash gemini-2.5-pro; do
    CODE=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TOKEN" \
      "https://$HOST/v1/projects/$PROJECT/locations/$LOC/publishers/google/models/$M")
    printf '%-12s %-20s %s\n' "$LOC" "$M" "$CODE"
  done
done
```

`200` means reachable, `404` means not. Send me the table — the choice of model is a
cost-and-quality decision and a change to a value `docs/00-overview.md` decided, so it is
yours to make rather than mine to guess.

Applying whichever answer it gives, in `envs/dev/dev.tfvars`:

```hcl
# Only if a different name is reachable:
model_name = "<the name that returned 200>"

# Only if the name is reachable at `global` but not at us-central1:
vertex_location = "global"
```

Then `terraform apply -var-file=dev.tfvars`. Both are plain environment variables on the
Cloud Run service, so the apply is a new revision and needs no image rebuild — the
running image is unchanged and CI still owns it.

`vertex_location` defaults to `var.region` and exists precisely because the two need not
agree: a new Gemini model is often reachable only on the `global` endpoint for a while
after release, and before this variable the region served both purposes.

### What to send me

Whatever happened, plus these — they are what I would need to diagnose anything:

```bash
# The last 200 log lines, errors first
gcloud run services logs read coach-api --region=us-central1 --limit=200 \
  | grep -iE 'error|traceback|exception|refused|denied' || echo "no errors logged"

# The turn documents this session wrote (status must be `complete`, not `running`)
gcloud firestore documents list --collection-ids=turns --limit=5 2>/dev/null \
  || echo "list unsupported on this gcloud; skip"
```

Known things that will bite, so you can tell a real failure from a configuration one:

- **The upload PUT fails with a CORS error in the browser console.** The uploads bucket's
  CORS origins come from `authorized_domains_urls` in `dev.tfvars`. If the service URL
  there is stale, the signed PUT is blocked by the browser before it ever reaches GCS.
  Fix the tfvar and re-apply (step 5 covers the same reconciliation for sign-in).
- **The upload PUT returns 403 `SignatureDoesNotMatch` or the API 500s on
  `POST /api/uploads`.** `coach-api-sa` needs `roles/iam.serviceAccountTokenCreator`
  **on itself** to sign V4 URLs through IAM SignBlob. Terraform grants it
  (`modules/identity/main.tf`), so this means the apply in 8.1 did not go through.
- **The reply is text-only and never mentions the image.** That is the interesting
  failure: it means Vertex did not resolve the `gs://` artifact URI. Send me the
  `artifactUri` from the `uploads/{uploadId}` document and I will trace it.

Once 8.4 passes, M2 is closed. Tell me and I will update the status section in
[docs/09-roadmap.md](../../docs/09-roadmap.md) and mark the milestone met.

---

## Teardown (dev only)

```bash
terraform destroy -var-file=dev.tfvars
```

`prod` sets `deletion_protection`, Firestore delete protection, and non-force-destroyable
buckets, so a `destroy` there fails on purpose. Turning those off is a deliberate,
reviewed change rather than a flag to flip in passing.
