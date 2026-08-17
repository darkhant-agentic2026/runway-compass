# One image, both apps. docs/07-infra-deploy.md#container.
#
# The build context is the repo root, not apps/api, because the build spans both apps —
# which is why this file lives at the root.

# ---------------------------------------------------------------------------------------
# Stage 1: the SPA.
#
# This is the ONLY build of the frontend that reaches production. CI also runs
# `vite build`, but only as a typecheck-and-lint gate; the deployable bundle is this one,
# so SPA/API version skew is structurally impossible.
# ---------------------------------------------------------------------------------------
FROM node:22-slim AS web
WORKDIR /w

COPY apps/web/package.json apps/web/package-lock.json apps/web/.npmrc ./
RUN npm ci

COPY apps/web/ ./

# Vite needs the environment's Identity Platform config at build time. These are public
# values by design — the security boundary is the authorized-domains list and server-side
# token verification, not secrecy of the API key (docs/07-infra-deploy.md#github-actions).
ARG VITE_AUTH_MODE=identity-platform
ARG VITE_IDENTITY_API_KEY=""
ARG VITE_IDENTITY_AUTH_DOMAIN=""
ARG VITE_IDENTITY_PROJECT_ID=""
ENV VITE_AUTH_MODE=$VITE_AUTH_MODE \
    VITE_IDENTITY_API_KEY=$VITE_IDENTITY_API_KEY \
    VITE_IDENTITY_AUTH_DOMAIN=$VITE_IDENTITY_AUTH_DOMAIN \
    VITE_IDENTITY_PROJECT_ID=$VITE_IDENTITY_PROJECT_ID

RUN npm run build

# ---------------------------------------------------------------------------------------
# Stage 2: Python dependencies.
#
# Split into two `uv sync` calls so the dependency layer caches independently of the
# source: editing a Python file does not re-resolve or re-download anything.
# ---------------------------------------------------------------------------------------
FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.1 /uv /bin/uv
WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

COPY apps/api/pyproject.toml apps/api/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY apps/api/src/ src/
RUN uv sync --frozen --no-dev

# ---------------------------------------------------------------------------------------
# Stage 3: runtime.
# ---------------------------------------------------------------------------------------
FROM python:3.12-slim

# There is NO `nonroot` user in python:3.12-slim — that account comes from distroless
# base images. Debian slim has `nobody` but no home directory, so the account has to be
# created explicitly or `USER` fails the build.
RUN useradd --create-home --uid 10001 coach

# REQUIRED, not cosmetic: main.py mounts StaticFiles(directory="static/assets") and
# returns FileResponse("static/index.html"), both relative. Without this WORKDIR the
# process starts in / , the mount raises at import time, and the image fails on first
# boot rather than in CI.
WORKDIR /app

COPY --from=builder --chown=coach:coach /app /app
COPY --from=web --chown=coach:coach /w/dist /app/static

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER coach
EXPOSE 8080

# A single uvicorn worker per instance: the in-process TurnRegistry and StreamBroker
# (M2) assume one process. Horizontal scaling is Cloud Run's job.
CMD ["uvicorn", "coach.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
