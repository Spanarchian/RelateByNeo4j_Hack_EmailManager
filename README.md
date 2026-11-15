# Gmail Onboarding Assistant

An API-first service that **prepares, reviews, approves and sends onboarding emails** for
volunteers joining an organisation and employees joining a company.

- **Gmail API** (OAuth 2.0, `gmail.send` scope only) is the primary email provider.
- **Mailchimp Transactional** (formerly Mandrill) is the secondary provider, with *safe* failover.
- **Relate by Neo4j**, an embedded, in-process Cypher graph database, stores people,
  organisations, onboardings, drafts, approvals and deliveries.
- Two deterministic **agents** prepare and check drafts. **A human must approve every email**,
  and only a human can send it.

> Status: implemented and tested. Real Gmail and Mailchimp delivery has **not** been exercised
> in this repository because no credentials were available. Those integrations are complete,
> unit- and integration-tested against mocked HTTP, and have opt-in live tests
> (`pytest -m external`). See [Limitations](#limitations).

---

## Contents

1. [Features](#features)
2. [Architecture](#architecture)
3. [Prerequisites](#prerequisites)
4. [Installation](#installation)
5. [Gmail OAuth setup](#gmail-oauth-setup)
6. [Mailchimp Transactional setup](#mailchimp-transactional-setup)
7. [Relate setup](#relate-setup)
8. [Environment configuration](#environment-configuration)
9. [Running locally](#running-locally)
10. [Docker deployment](#docker-deployment)
11. [API usage](#api-usage)
12. [Volunteer onboarding](#volunteer-onboarding)
13. [Employee onboarding](#employee-onboarding)
14. [Email approval](#email-approval)
15. [Provider failover](#provider-failover)
16. [Testing](#testing)
17. [Taskipy](#taskipy)
18. [Troubleshooting](#troubleshooting)
19. [Security](#security)
20. [Limitations](#limitations)

---

## Features

| Area | What you get |
|---|---|
| Email API | `POST /api/v1/emails` with plain text (always) plus optional HTML, validated senders and recipients, configurable recipient limit, `Idempotency-Key` support. Submitted emails become drafts; they are never sent directly. |
| Onboarding | Volunteer and employee onboarding records, stored as a graph; creation is idempotent. |
| Templates | Four professional templates (volunteer welcome, volunteer induction, employee welcome, employee first day), each in plain text and HTML, rendered in a Jinja2 sandbox that rejects unresolved placeholders. |
| Agents | The **onboarding agent** selects a template, fills it with verified data and submits the draft for review. The **review agent** runs deterministic checks on sender, recipients, identity, dates, placeholders, content and sensitive data. Neither agent can approve or send. |
| Approval | Role-based human approval bound to a SHA-256 **content hash**. Any edit invalidates the approval, and the hash is recomputed and re-checked immediately before sending. |
| Delivery | Gmail primary, Mailchimp secondary. Failover happens only after a *definitive* rejection. Timeouts produce an **UNKNOWN** state that is never resent automatically. Every attempt is recorded durably. |
| Duplicate prevention | An atomic "claim for sending" step, a unique per-delivery idempotency key in the graph, one active draft per onboarding template, and idempotent onboarding and email submission. |
| Audit | Onboarding history built from the graph, and queries for pending approvals, unsent emails and failed deliveries. |
| Ops | Docker image (non-root, read-only root filesystem, health check), Compose with persistent volumes, Taskipy tasks, GitHub Actions CI, Sphinx docs for Read the Docs. |

## Architecture

```
            ┌─────────────── FastAPI (REST, OpenAPI at /docs) ───────────────┐
 clients ──►│ API key auth · role checks · rate limit · structured errors      │
            └───────┬──────────────────┬────────────────────┬────────────────┘
                    │                  │                    │
           OnboardingService     EmailService         ApprovalService
                    │            │          │               │
           OnboardingAgent ──► ReviewAgent  ProviderService (retry, safe failover)
                    │                       │         │
                    │                  GmailProvider  MailchimpTransactionalProvider
                    ▼                       ▼         ▼
            GraphRepository ──► RelateDatabase ──► relate-db (embedded, in-process)
                                 (one connection, one worker thread, one writer)
```

Business logic (services, agents, approval rules) is separated from infrastructure (Relate
adapter, provider adapters, HTTP). All wiring happens in one composition root
(`email_service/dependencies.py`). For the full design and its rationale, see
[docs/SAD.md](docs/SAD.md).

## Prerequisites

- Python **3.14** and [uv](https://docs.astral.sh/uv/) (tested with uv 0.12.19)
- Docker 24+ with Compose v2 (for container deployment)
- A Google Cloud project with the Gmail API enabled (for real sending)
- Optional: a Mailchimp Transactional account with a verified sending domain

Relate needs **no separate installation**: the `relate-db` wheel contains the engine.

## Installation

```bash
git clone <this repository> gmail-onboarding-assistant
cd gmail-onboarding-assistant
uv sync --all-groups          # installs locked runtime, dev and docs dependencies
cp .env.example .env          # then edit .env (see below)
uv run task api-key -- riley.reviewer reviewer   # repeat per client
```

## Gmail OAuth setup

The app uses the official Gmail REST API (`users.messages.send`) with **only** the
`https://www.googleapis.com/auth/gmail.send` scope.

1. In Google Cloud Console, create a project and **enable the Gmail API**.
2. Configure the **OAuth consent screen**. While in *Testing* status, add the sending
   account as a test user. Google expires refresh tokens for apps in *Testing* status after
   7 days, so publish the app (or use a Workspace *Internal* app) for long-running use.
3. Create **Credentials → OAuth client ID → Desktop app**, download the JSON and save it as
   `secrets/credentials.json` (gitignored).
4. Run the one-off authorisation on a machine with a browser:

   ```bash
   uv run task auth                    # opens a browser
   uv run task auth -- --no-browser    # prints the URL instead
   ```

   This writes `secrets/token.json` (mode `0600`) containing a refresh token.
5. The `From` address must be the authorised account, or a *Send mail as* alias configured
   in that Gmail account. List it in `EMAIL_ALLOWED_SENDERS`.

**Headless and Docker use.** Never run the browser flow in the container. Provision the token
you created in step 4 into the `gmail-token` volume:

```bash
uv run task docker-token
# = docker compose run --rm --no-deps -T app sh -c 'umask 077 && cat > /secrets/gmail/token.json' < secrets/token.json
```

The token is streamed over stdin and written by the container's own non-root user, so the
file ends up owned by uid 10001 with mode `0600`, with no bind mount and no root shell.

The app refreshes the access token automatically and writes it back to the volume. If the
volume is read-only, the refreshed token is kept in memory.

## Mailchimp Transactional setup

This uses **Mailchimp Transactional** (`https://mandrillapp.com/api/1.0/messages/send.json`),
not the Mailchimp Marketing campaigns API.

1. Activate Transactional Email in your Mailchimp account (it is a paid add-on).
2. **Add and verify your sending domain** (DKIM and SPF DNS records) under
   *Transactional → Domains*. Mail from unverified domains is rejected.
3. Create an API key under *Transactional → Settings → API keys*.
4. Set `MAILCHIMP_TRANSACTIONAL_API_KEY` in `.env`. While it is empty, Mailchimp stays
   **disabled** and is never contacted.

The key travels only in the request body. It is never logged and never returned by the API.

## Relate setup

[Relate by Neo4j](https://relatedb.ai) is an open-source, embedded Cypher graph database that
runs **inside the application process**. It was verified for this project from the official
documentation (verified against relate **1.0.1**), and its behaviour was checked locally.

| Fact | Consequence here |
|---|---|
| Python package `relate-db` (`import relate`), abi3 wheels for macOS and Linux glibc | Pinned `relate-db==1.0.1`; works on Python 3.14 and `python:3.14-slim` |
| `relate.connect(path)` opens a directory store, a `.rl` file or `:memory:` | Default store: the directory `data/graph/onboarding` |
| One writable connection per store, across all processes ("storage locked") | **One app process per store.** The Docker image runs a single uvicorn worker, and Compose runs a single container. |
| A connection belongs to its opening thread | All graph calls run on one dedicated worker thread |
| Optimistic MVCC with retryable write conflicts; uniqueness constraints enforced | Constraints on every `id`, plus unique delivery idempotency keys |
| The embedded engine sends no telemetry | Nothing to disable (`DO_NOT_TRACK=1` is set anyway) |

Schema constraints and reference data (templates, agents, providers) are created idempotently
on every start-up. To load the fictional sample onboardings:

```bash
uv run task graph-seed     # stop the API first: single writer
uv run task graph-check    # read-only; works while the API is running
```

The optional `relate` CLI (`curl -sSfL https://relatedb.ai/install.sh | bash`) can inspect
the same store read-only, for example
`relate --database data/graph/onboarding --read-only "MATCH (n) RETURN labels(n), count(*)"`.
The application itself does not need the CLI.

## Environment configuration

All settings come from environment variables or `.env`. See [.env.example](.env.example)
for the complete list.

| Variable | Default | Purpose |
|---|---|---|
| `APP_ENV` | `development` | `production` requires `API_KEYS` and a persistent store |
| `API_KEYS` | `[]` | JSON array of `{name, key_sha256, roles, kind}` |
| `APPROVAL_REQUIRE_FOUR_EYES` | `true` | The requester of a draft cannot approve it |
| `RATE_LIMIT_PER_MINUTE` | `120` | Per API key (or IP address); `0` disables |
| `RELATE_STORE_PATH` | `data/graph/onboarding` | Relate store location |
| `EMAIL_PRIMARY_PROVIDER` | `gmail` | `gmail` or `mailchimp` |
| `EMAIL_SECONDARY_PROVIDER` | `mailchimp` | Provider name, or `none` |
| `EMAIL_FAILOVER_ENABLED` | `true` | Allow failover after a definitive failure |
| `EMAIL_DEFAULT_SENDER` | — | Sender for agent-prepared drafts (required to prepare drafts) |
| `EMAIL_ALLOWED_SENDERS` | — | Comma-separated allow-list checked by the review agent |
| `EMAIL_MAX_RECIPIENTS` | `20` | Per email (hard maximum 100) |
| `PROVIDER_TIMEOUT_SECONDS` / `PROVIDER_MAX_RETRIES` / `PROVIDER_RETRY_BACKOFF_SECONDS` | `15` / `2` / `1.0` | Delivery tuning |
| `GMAIL_CREDENTIALS_FILE` / `GMAIL_TOKEN_FILE` | `secrets/…` | OAuth client file and token |
| `MAILCHIMP_TRANSACTIONAL_API_KEY` | empty | Enables Mailchimp when set |

## Running locally

```bash
uv run task dev        # http://127.0.0.1:8000, interactive docs at /docs
```

## Docker deployment

```bash
cp .env.example .env               # set APP_ENV=production, API_KEYS, senders, Mailchimp key
uv run task deploy                 # docker compose up -d --build --wait
curl http://127.0.0.1:8000/health
uv run task logs
uv run task stop                   # volumes are kept
```

- The image is `python:3.14-slim` with uv, locked dependencies, **non-root** uid 10001, a
  health check and a single uvicorn worker.
- Compose runs the container with a read-only root filesystem, `cap_drop: ALL` and
  `no-new-privileges`. It mounts two named volumes, **`relate-data`** (the graph) and
  **`gmail-token`** (OAuth token), and publishes the port on `127.0.0.1:8000` only.
- Put a TLS-terminating reverse proxy (Caddy, nginx, Traefik) in front. See
  [docs/security.rst](docs/security.rst).
- **Never scale beyond one replica** per `relate-data` volume.

## API usage

Every `/api/v1` endpoint needs `X-API-Key`. The full OpenAPI specification is at `/docs` and
`/openapi.json`.

| Method | Path | Role |
|---|---|---|
| GET | `/health` | none |
| POST | `/api/v1/emails` | submitter |
| GET | `/api/v1/emails/pending-review` · `/unsent` · `/failed-deliveries` | any |
| GET / PATCH | `/api/v1/emails/{id}` | any / submitter, reviewer |
| POST | `/api/v1/emails/{id}/approve` · `/reject` | **human reviewer** |
| POST | `/api/v1/emails/{id}/send` · `/reconcile` | **human reviewer** |
| POST | `/api/v1/emails/{id}/cancel` | submitter, reviewer |
| POST | `/api/v1/onboarding` | submitter |
| GET | `/api/v1/onboarding?type=volunteer` · `/api/v1/onboarding/{id}` · `/{id}/history` | any |
| POST | `/api/v1/onboarding/{id}/draft` | submitter |

Errors always look like `{"error": {"code": "...", "message": "...", "details": {...}}}`.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/emails \
  -H "X-API-Key: $SUBMITTER_KEY" -H "Idempotency-Key: import-2027-001" \
  -H "Content-Type: application/json" \
  -d '{"title":"Welcome to our organisation","body":"Welcome to our team.",
       "html_body":"<h1>Welcome</h1>","sender":"sender@gmail.com",
       "recipients":["person1@example.com","person2@example.com"]}'
# -> 201 {"id":"draft-…","status":"PENDING_REVIEW","content_hash":"…","review":{…}}
```

## Volunteer onboarding

```bash
curl -X POST http://127.0.0.1:8000/api/v1/onboarding -H "X-API-Key: $SUBMITTER_KEY" \
  -H "Content-Type: application/json" -d '{
  "type":"volunteer","name":"Alex Morgan","email":"alex.morgan@example.org",
  "organisation":"Riverside Community Food Bank","volunteer_role":"Warehouse Volunteer",
  "coordinator":"Priya Shah","team":"Saturday Warehouse","start_date":"2027-02-06",
  "induction_details":"Saturday 30 January 2027, 10:00-12:30 at the Riverside warehouse."}'

curl -X POST http://127.0.0.1:8000/api/v1/onboarding/$ONBOARDING_ID/draft \
  -H "X-API-Key: $SUBMITTER_KEY" -H "Content-Type: application/json" \
  -d '{"template":"welcome"}'        # or "induction"
```

## Employee onboarding

The same flow with `"type":"employee"` and the fields `job_title`, `department`, `manager`
and `first_day_arrangements`. The templates are `welcome` and `first_day`.

## Email approval

1. `GET /api/v1/emails/pending-review`, then `GET /api/v1/emails/{id}`. Read the content,
   the review findings and the `content_hash`.
2. `POST /api/v1/emails/{id}/approve` with `{"content_hash": "<the hash you reviewed>"}`.
   If the draft changed since you read it, the approval is refused (409). Drafts with
   `ERROR` findings cannot be approved; edit them first.
3. `POST /api/v1/emails/{id}/send`. The response is **200** (SENT), **202** (UNKNOWN) or
   **502** (FAILED).
4. Any `PATCH` to the draft invalidates its approvals and returns it to `PENDING_REVIEW`.

Rules: reviewers must be humans with the `reviewer` role, must not be the identity that
prepared the draft, and, when four-eyes is enabled, must not be its requester.

## Provider failover

| Primary outcome | Action |
|---|---|
| Accepted (2xx) | SENT. Stop. |
| Connection refused / connect timeout / 429 / 408 | Retry the same provider with exponential backoff (`PROVIDER_MAX_RETRIES`). |
| Other 4xx, missing credentials, all recipients rejected | Definitive failure: fail over to the secondary provider (if enabled). |
| **Read timeout, 5xx, broken connection** | **UNKNOWN.** No retry, no failover. A human checks the Gmail *Sent* folder or the Mailchimp activity log, then calls `POST /emails/{id}/reconcile` with `SENT` or `FAILED`. |

Neither Gmail nor Mailchimp Transactional offers a send idempotency key, so the application
guarantees *at most one automatic submission per approved delivery*. The trade-off is that an
UNKNOWN delivery needs a human decision. See [docs/mail_providers.rst](docs/mail_providers.rst).

## Testing

```bash
uv run task test-all          # all tests + coverage gate (85%)
uv run task test-unit | test-integration | test-e2e
uv run task test-graph | test-providers | test-workflow
RUN_EXTERNAL_TESTS=1 EXTERNAL_TEST_RECIPIENT=you@example.com \
  EXTERNAL_TEST_SENDER=you@gmail.com uv run pytest -m external   # real sends, opt-in
```

Graph tests use the **real Relate engine** (in-memory and temporary directory stores). Gmail
and Mailchimp are mocked at the HTTP layer with respx.

## Taskipy

| Task | Does |
|---|---|
| `dev` / `run` | API with reload / production-style |
| `auth` | Gmail OAuth authorisation |
| `api-key` | Generate an API key and its `API_KEYS` entry |
| `test-unit`, `test-integration`, `test-e2e`, `test-all`, `test-graph`, `test-providers`, `test-workflow` | Test selections |
| `lint`, `format`, `typecheck`, `quality` | Ruff, Ruff format, mypy (strict), everything |
| `graph-seed`, `graph-check` | Seed / inspect the Relate store |
| `docs` | Build the Sphinx HTML documentation (warnings are errors) |
| `docker-build`, `deploy`, `logs`, `stop` | Docker Compose lifecycle |
| `docker-token` | Copy `secrets/token.json` into the `gmail-token` volume |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `storage locked: …/.cypherlite.writer` | Another process has the store open for writing (a running API, `task graph-seed`, a second container). Stop it. Use `task graph-check` for read-only access. |
| `/health` returns 503 | The graph store could not be opened; check the logs and volume permissions (uid 10001). |
| Send returns 502 with `provider_not_configured` | No `token.json` and no Mailchimp key. Provision credentials. |
| Gmail `credentials_unavailable` / refresh failed | The refresh token was revoked or expired (the consent screen is in *Testing* status). Re-run `task auth` and re-provision. |
| Gmail 403 `insufficientPermissions` | The token was created with other scopes. Delete `token.json` and re-run `task auth`. |
| Mailchimp `Invalid_Key` / rejected `unsigned` | Wrong key, or the sending domain is not verified. |
| Approve returns 409 "changed since it was reviewed" | Reload the draft and approve the new `content_hash`. |
| Draft creation 422 `EMAIL_DEFAULT_SENDER` | Set the sender in `.env`. |
| Send returns 202 | Delivery outcome UNKNOWN. Check the provider, then `POST /reconcile`. |

## Security

- API keys are stored only as SHA-256 digests and compared in constant time. Roles are
  `submitter`, `reviewer` and `admin`. Only **human** reviewers can approve, send or
  reconcile. Agents and service accounts never can.
- Secrets are `SecretStr` and never logged. Logs are JSON with tokens, keys and email
  addresses redacted.
- OAuth uses the minimal `gmail.send` scope. Token files are written atomically with mode `0600`.
- Templates render in a Jinja2 sandbox with HTML auto-escaping. Header injection is rejected.
  No endpoint accepts Cypher.
- `.env`, `secrets/`, `credentials.json` and `token.json` are excluded from Git, Docker
  builds and the distribution ZIP.

Details: [docs/security.rst](docs/security.rst).

## Limitations

- **Live provider delivery has not been tested here** because no Gmail or Mailchimp
  credentials were available. The adapters follow the official APIs, were checked against
  the providers' real unauthenticated error responses, and are fully tested with mocked HTTP.
- Neither provider supports send idempotency keys, so UNKNOWN deliveries need human
  reconciliation (by design).
- Relate allows a single writer per store, so the service runs as **one process**.
  Horizontal scaling would need a different storage topology, such as Relate's own
  `relate serve` server, which is outside this project's scope.
- The rate limiter is in-process. Add limits at the reverse proxy as well.
- Relate 1.0.1 is new (released November 2026). The adapter is isolated in
  `email_service/graph/` so upgrades stay contained.
