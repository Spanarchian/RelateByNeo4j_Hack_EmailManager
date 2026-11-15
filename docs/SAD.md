# Software Architecture Document — Gmail Onboarding Assistant

| | |
|---|---|
| Version | 0.1.0 |
| Status | Implemented; live provider delivery awaiting credentials |
| Audience | Developers, operators and reviewers of the service |

## 1. Executive summary

The Gmail Onboarding Assistant is a small, API-first Python service that turns onboarding
records (volunteers and employees) into professional emails. It never sends one without
explicit human approval. A deterministic onboarding agent prepares drafts from verified data,
a deterministic review agent checks them, and a human reviewer approves the exact content,
which is bound by a SHA-256 hash. Delivery goes through Gmail, with Mailchimp Transactional
as a conservative fallback. All state lives in **Relate by Neo4j**, an embedded, in-process
Cypher graph database, so the deployment is a single container with two volumes.

## 2. Objectives

1. Prepare accurate, professional onboarding emails with minimal manual effort.
2. Make sending **safe by construction**: human approval, approval integrity, no duplicates,
   and no blind resends.
3. Keep the system minimal, typed, modular and testable, with the REST API as the only
   integration surface.
4. Make every decision auditable (who prepared, reviewed, approved and sent what, and through
   which provider).

## 3. Scope

**In scope:** the REST API; volunteer and employee onboarding; four templates; two
deterministic agents; human approval workflow; Gmail and Mailchimp Transactional delivery
with failover; graph persistence and queries; API key authentication with roles; Docker
deployment; tests, CI and documentation.

**Out of scope:** a user interface; inbound email; scheduling or bulk campaigns; LLM-based
agents (an interface seam is provided); multi-replica deployment; Single Sign-On.

## 4. System context

```
  HR / volunteer-management systems ──┐
  Coordinators & managers (submitters)┼──► REST API (X-API-Key) ──► Gmail API (OAuth 2.0)
  Human reviewers ────────────────────┘          │               └► Mailchimp Transactional
                                                 ▼
                                      Relate store (volume, embedded)
```

External dependencies:

- **Gmail API** — `POST https://gmail.googleapis.com/gmail/v1/users/me/messages/send`, scope
  `gmail.send`, OAuth 2.0 user credentials with a refresh token.
- **Mailchimp Transactional** — `POST https://mandrillapp.com/api/1.0/messages/send.json`, key
  in the body.
- **Relate by Neo4j** — no external process; the `relate-db` wheel embeds the engine.

## 5. Component architecture

| Layer | Package | Responsibility |
|---|---|---|
| API | `api/` | FastAPI routers, request validation, status codes, structured errors |
| Security | `security/` | API key authentication, roles and permissions, rate limiting, log redaction |
| Services | `services/`, `approvals/service.py` | Use cases: onboarding, draft lifecycle, approval, sending, reconciliation |
| Agents | `agents/` | Onboarding agent (prepare and submit), review agent (findings) |
| Onboarding | `onboarding/` | Templates, sandboxed renderer, draft state machine, template context |
| Approval integrity | `approvals/security.py` | Content hash, reviewer and sender verification |
| Providers | `providers/` | `EmailProvider` interface, Gmail and Mailchimp adapters, outcome classification |
| Delivery policy | `services/provider_service.py` | Provider chain, retry, safe failover, attempt recording |
| Graph | `graph/` | Relate adapter (`database.py`), schema and ids, Cypher (`queries.py`), repository |
| Composition | `dependencies.py`, `main.py`, `config.py` | Wiring, FastAPI dependencies, settings |

Dependencies point inwards. Services depend on the repository and provider *interfaces*;
infrastructure is constructed only in `build_container`. Tests inject alternative providers,
clocks and sleep functions through the same function.

## 6. Data architecture

All persistent state is in one Relate store. Stable identifiers make writes idempotent:

| Entity | Identifier |
|---|---|
| Person | `person-uuid5(email)` |
| Organisation | `org-uuid5(normalised name)` |
| Team | `team-uuid5(org id, team name)` |
| Onboarding | `onb-uuid5(type, email, org id, start date)` |
| EmailDraft | `draft-uuid4`, or `draft-uuid5(client, Idempotency-Key)` |
| Approval / EmailDelivery | `appr-uuid4` / `dlv-uuid4`; delivery `idempotency_key = <draft id>#<sequence>` |
| EmailTemplate / Agent / EmailProvider | `volunteer-welcome`, `agent:onboarding`, `gmail`, … |

Relate does not accept maps as property values, so structured values (review findings,
delivery attempts, onboarding details) are stored as JSON strings. Dates and timestamps are
native `DATE` and `ZONED DATETIME` values. No credentials are ever written to the graph.

## 7. Graph model

```
(Person)-[:UNDERGOES]->(Onboarding)-[:FOR_ORGANISATION]->(Organisation)
(Person)-[:MEMBER_OF]->(Team)-[:PART_OF]->(Organisation)
(Onboarding)-[:USES_TEMPLATE]->(EmailTemplate)
(Agent)-[:PREPARED_FOR {drafts, first_at, last_at}]->(Onboarding)
(Onboarding)-[:HAS_DRAFT]->(EmailDraft)
(EmailDraft)-[:HAS_APPROVAL]->(Approval)
(EmailDraft)-[:HAS_DELIVERY]->(EmailDelivery)
(EmailDelivery)-[:SENT_THROUGH {attempts, last_outcome, provider_message_id}]->(EmailProvider)
```

Constraints: `id IS UNIQUE` on all ten labels; `EmailDelivery.idempotency_key IS UNIQUE`;
`EmailDraft.idempotency_key IS UNIQUE`. The provided queries cover pending approvals,
volunteer and employee onboardings, unsent onboarding emails, failed or unknown deliveries,
and onboarding history. No endpoint accepts Cypher.

### Relate integration decision

Relate was researched before implementation (official documentation at relatedb.ai, PyPI
metadata and local probing). It is an embedded engine with a DB-API-style Python client. A
store has **one writer** across all processes, and a connection is **bound to its thread**.
The adapter therefore holds one connection on one dedicated worker thread and submits every
unit of work to it (`RelateDatabase.read` / `.write`, with retry on `retryable` conflicts).
This serialises in-process writes, which the duplicate-send guard relies on, and the
deployment runs exactly one process per store. Directory stores allow read-only inspection
beside the writer (`task graph-check`).

## 8. Email provider architecture

```python
class EmailProvider(ABC):
    name: str

    @property
    def is_configured(self) -> bool: ...
    async def send(self, email: OutboundEmail) -> ProviderResult: ...
```

`send` never raises for transport or provider errors. It returns one of three outcomes:

- **ACCEPTED** — the provider took responsibility for the message.
- **REJECTED** (with a `retryable` flag) — the provider definitively did not accept it.
- **UNKNOWN** — the message may or may not have been accepted.

| Signal | Outcome |
|---|---|
| 2xx (Gmail), any recipient `sent`/`queued`/`scheduled` (Mailchimp) | ACCEPTED |
| Connect error / connect timeout / pool timeout | REJECTED, retryable |
| HTTP 408 or 429 | REJECTED, retryable |
| Other 4xx; Mailchimp `Invalid_Key`, `ValidationError`, `PaymentRequired`, …; all recipients rejected; missing credentials | REJECTED |
| Read or write timeout, protocol error, 5xx, Mailchimp `GeneralError`, unparseable 2xx (Mailchimp) | UNKNOWN |

Gmail messages are RFC 5322: `text/plain`, or `multipart/alternative` (plain, then HTML),
with a deterministic `Message-ID` and an `X-Onboarding-Delivery-ID` header derived from the
delivery id, so a message can be found when reconciling. Mailchimp messages carry the same
headers and `metadata.delivery_id`, use `preserve_recipients: true` (one message to all, like
Gmail) and have tracking disabled.

Observed behaviour that shaped the parser: with an invalid key, Mailchimp Transactional
returned HTTP **401** with `{"status":"error","name":"Invalid_Key"}`, where older
documentation describes HTTP 500. The adapter therefore classifies by response body, not
status code.

## 9. Failover strategy

`ProviderService.deliver` walks the chain `[primary, secondary]`. The secondary is included
only if failover is enabled.

1. Skip an unconfigured provider, recording a `provider_not_configured` attempt.
2. Call `send`. **Persist the attempt durably** before deciding anything else.
3. ACCEPTED → SENT. UNKNOWN → stop immediately (no retry, no failover).
4. REJECTED and retryable → back off exponentially and retry the same provider, up to
   `PROVIDER_MAX_RETRIES` times.
5. REJECTED definitively → move to the next provider. If none is left, the delivery is FAILED.

An unexpected exception inside a provider is classified as UNKNOWN, so a bug can never cause
a resend. If the process dies mid-delivery, the draft is left in SENDING. On the next start,
`recover_interrupted_sends` moves it to UNKNOWN.

**Limitations.** Neither provider offers a send idempotency key, so exactly-once delivery
across an ambiguous network failure is impossible. The system chooses *at most once
automatically*. UNKNOWN deliveries are resolved by a human
(`POST /emails/{id}/reconcile`), who checks the Gmail Sent folder (searchable by
`Message-ID`) or the Mailchimp activity log. A reconciled FAILED delivery may be sent again;
it gets a new sequence number and idempotency key.

## 10. Agentic workflow

```
POST /onboarding/{id}/draft
  └─ OnboardingAgent (agent:onboarding)
       1 load onboarding (verified, validated fields only)
       2 select template by (type, kind)
       3 render in a sandbox (StrictUndefined, autoescape HTML, placeholder scan)
       4 store EmailDraft (DRAFT); Agent-[:PREPARED_FOR]->Onboarding
       5 ReviewAgent (agent:review) → ReviewReport (findings: ERROR | WARNING | INFO)
       6 DRAFT → PENDING_REVIEW
```

Review rules are pure functions `(EmailContent, ReviewContext) -> list[ReviewFinding]`. They
check sender (allow-list), recipients (count, identity match, extras), identity
(organisation, role, name), dates (past, far future, present in body), placeholders,
content (unsafe HTML, insecure links, length) and sensitive data (NI numbers, SSNs, IBANs,
Luhn-valid card numbers, credentials as errors; personal topics as warnings). An LLM-backed
rule can be added to the rule list without touching approval or delivery code.

Agents have **no reference** to the approval service or providers, and agent ids
(`agent:*`) can never be API principals (enforced by configuration validation). An agent
therefore cannot approve or send, structurally as well as by policy.

## 11. Approval process

States: `DRAFT, PENDING_REVIEW, APPROVED, REJECTED, SENDING, SENT, FAILED, UNKNOWN,
CANCELLED`. Allowed transitions are a single table (`onboarding/workflow.py`).

```
DRAFT → PENDING_REVIEW → APPROVED → SENDING → SENT
          ↑   │  ↘ REJECTED            ├→ FAILED ─→ SENDING (retry, approval still valid)
   (edit) └───┘                        └→ UNKNOWN ─(human reconcile)→ SENT | FAILED
any non-terminal unsent state → CANCELLED ; any edit → PENDING_REVIEW (approvals invalidated)
```

**Approval** records the draft id, reviewer identity, timestamp, decision and the approved
**content hash**: SHA-256 over versioned canonical JSON of sender, recipients, subject,
plain-text body and HTML body. The reviewer must submit the hash they reviewed. Approval is
refused if it does not equal the current hash, if the review has blocking findings, if the
reviewer is not a human with the reviewer role, if the reviewer prepared the draft, or (with
four-eyes on) if the reviewer requested it.

**Sending** (`GraphRepository.claim_for_sending`, one transaction on the single graph thread):

1. Verify the human sender's authority (route permission and service check).
2. Verify the status is APPROVED, or FAILED with a still-valid approval.
3. Recompute the hash from stored content and compare it with the approval and the stored hash.
4. Allocate `sequence = delivery_count + 1` and create an `EmailDelivery` (PENDING) with the
   unique `idempotency_key`.
5. Move the draft to SENDING.

Only then is a provider called. A concurrent second request sees SENDING and is refused (409).
This is verified with 10 concurrent claims in the integration tests and 4 concurrent HTTP
sends in the E2E tests.

## 12. Security

- **Authentication:** `X-API-Key`; only SHA-256 digests are configured; constant-time
  comparison.
- **Authorisation:** roles `submitter`, `reviewer`, `admin`. APPROVE, SEND and RECONCILE
  require a *human* principal. Service clients cannot hold the reviewer role, and the admin
  role cannot approve or send.
- **Input validation:** Pydantic models; email validation; header-injection checks; recipient
  limits; template sandbox with auto-escaping. Validation errors never echo input values.
- **Secrets:** `SecretStr`; OAuth token file written atomically with mode `0600`; Mailchimp
  key only in request bodies. Nothing is written to the graph, logs or responses.
- **Logging:** JSON lines; bearer tokens, Google tokens, API keys and `key=`/`password=`
  values are redacted; email addresses are reduced to their domain.
- **Rate limiting:** in-process fixed window per API key or IP address, plus reverse-proxy
  limits in deployment.
- **Transport:** loopback-bound container port; TLS terminated at a reverse proxy.
- **Container:** non-root uid 10001, read-only root filesystem, `cap_drop: ALL`,
  `no-new-privileges`, resource limits.

## 13. Deployment

One container (`python:3.14-slim`, uv-installed locked dependencies, single uvicorn worker)
with two named volumes: `relate-data` (graph) and `gmail-token` (OAuth token). The health
check calls `/health`, which pings the graph. Restart policy is `unless-stopped`. Scaling is
explicitly unsupported because of Relate's single-writer model; a second container on the
same volume fails fast with `storage locked`, which was verified during implementation.

## 14. Testing

| Suite | What it covers |
|---|---|
| unit | Models, configuration, hashing, permissions, templates, state machine, review rules, MIME, Gmail and Mailchimp adapters (respx), failover policy, logging redaction |
| integration | Real Relate engine: schema, seeding, graph shape, persistence across reopen, single-writer lock, read-only reader, atomic claims, constraints, recovery; API contract; adapters with delivery records |
| e2e | Volunteer and employee flows, rejection, approval, delivery, Gmail failure → Mailchimp, Gmail timeout → UNKNOWN → reconcile, duplicate and concurrent sends, edit invalidation, tampering detection, role and four-eyes checks |
| external | Real Gmail and Mailchimp sends; opt-in via `RUN_EXTERNAL_TESTS=1` |

The coverage gate is 85% (branch coverage on).

## 15. Architectural decisions

| # | Decision | Rationale | Consequence |
|---|---|---|---|
| AD-1 | Use Relate's official Python client (`relate-db`) in-process | It is the documented integration; no server needed | Single writer: one process per store |
| AD-2 | One connection on one worker thread | Relate connections are thread-bound; this serialises writes | Graph throughput is bounded by one thread (ample for this workload) |
| AD-3 | Gmail via REST and `google-auth`, not `google-api-python-client` | Smaller dependency set, async `httpx`, same official endpoint | Small amount of request code to maintain |
| AD-4 | Three-valued provider outcome with UNKNOWN | A timeout is not proof of failure | Human reconciliation for ambiguous sends |
| AD-5 | Approve-by-hash supplied by the reviewer | Binds approval to what was actually seen | Clients must pass the hash |
| AD-6 | Deterministic agents behind a rule interface | Predictable, testable, no LLM dependency | LLM rules can be added later without touching approvals |
| AD-7 | Submissions via `POST /emails` become drafts | One safety path for all email | Direct sending is impossible by design |
| AD-8 | JSON strings for nested values in the graph | Relate rejects map properties | Nested data is not queryable by Cypher |
| AD-9 | API key auth with hashed keys and roles | Minimal, dependency-free | No SSO; keys are rotated by configuration |

## 16. Acceptance criteria

| Criterion | Evidence |
|---|---|
| `POST /api/v1/emails` validates and records submissions; plain text always present | `tests/integration/test_api.py`, `tests/unit/test_models_and_config.py` |
| Gmail MIME multipart, OAuth refresh, message id recorded | `tests/unit/test_providers.py`, E2E flows |
| Mailchimp Transactional send, errors, timeouts, disabled without key | `tests/unit/test_providers.py`, `tests/integration/test_provider_failover.py` |
| Failover only after definitive failure; UNKNOWN on timeout; no blind resend | `tests/unit/test_provider_service.py`, `tests/e2e/test_delivery_safety.py` |
| Relate persistence, constraints, idempotent seed, single writer | `tests/integration/test_graph.py`, Docker restart test |
| Agents never approve or send; human approval mandatory | E2E role tests; agents hold no approval or provider references |
| Edits invalidate approval; tampering detected by hash | `test_edited_draft_invalidates_approval`, `test_out_of_band_content_change_blocks_send` |
| Duplicate and concurrent sends deliver once | `test_duplicate_submission_prevention`, `test_concurrent_send_requests_deliver_once`, `test_claim_is_atomic_under_concurrency` |
| Docker image builds, runs non-root, healthy, persists across restart | Verified with `docker compose up --wait` during implementation |
| Coverage ≥ 85%, Ruff, mypy strict, docs build | CI workflow `.github/workflows/tests.yml` |
