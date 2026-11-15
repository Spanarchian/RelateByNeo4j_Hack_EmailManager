# syntax=docker/dockerfile:1.7
# Gmail Onboarding Assistant - production image.
# Relate by Neo4j is embedded (relate-db wheel), so no database server is needed.

ARG PYTHON_IMAGE=python:3.14-slim
ARG UV_VERSION=0.12.19

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# ---------------------------------------------------------------- build stage
FROM ${PYTHON_IMAGE} AS builder
COPY --from=uv /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /build

# Dependencies first (cached layer), exactly as locked; no dev/docs groups.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# -------------------------------------------------------------- runtime stage
FROM ${PYTHON_IMAGE} AS runtime
LABEL org.opencontainers.image.title="gmail-onboarding-assistant" \
      org.opencontainers.image.description="Onboarding email assistant (Gmail, Mailchimp Transactional, Relate by Neo4j)"

RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --home-dir /nonexistent --no-create-home \
            --shell /usr/sbin/nologin app \
 && mkdir -p /data/graph /secrets/gmail \
 && chown -R app:app /data /secrets \
 && chmod 0700 /data/graph /secrets/gmail

COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DO_NOT_TRACK=1 \
    APP_ENV=production \
    RELATE_STORE_PATH=/data/graph/onboarding \
    GMAIL_TOKEN_FILE=/secrets/gmail/token.json \
    GMAIL_CREDENTIALS_FILE=/secrets/gmail/credentials.json

USER app:app
WORKDIR /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"]

# Exactly one worker: the Relate store has a single writer.
CMD ["uvicorn", "--factory", "email_service.main:create_app", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "1", \
     "--proxy-headers", "--no-server-header"]
