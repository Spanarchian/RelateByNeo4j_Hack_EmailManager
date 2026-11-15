Architecture
============

The full Software Architecture Document is :doc:`SAD`. This page summarises it.

Layers
------

.. code-block:: text

   api/            FastAPI routers, status codes, structured errors
   security/       API keys, roles, rate limiting, log redaction
   services/       onboarding, email drafts and sending, provider selection
   approvals/      human approval workflow and integrity checks
   agents/         onboarding agent, review agent, agent interfaces
   onboarding/     templates, sandboxed renderer, state machine
   providers/      EmailProvider interface, Gmail and Mailchimp adapters
   graph/          Relate adapter, schema, Cypher queries, repository
   dependencies.py composition root; main.py application factory

Dependency injection
--------------------

``build_container(settings, providers=..., http=..., clock=..., sleep=...)`` constructs
every object once. FastAPI dependencies read from the container stored on
``app.state``. Tests use the same function to inject mocked HTTP clients, fixed clocks and
no-op sleeps, so the production wiring is what gets tested.

Request flow: sending an approved draft
---------------------------------------

.. code-block:: text

   POST /emails/{id}/send
     → require(SEND)                      human reviewer only
     → EmailService.send
         → GraphRepository.claim_for_sending   (one transaction, single graph thread)
              status ∈ {APPROVED, FAILED}? valid approval? recomputed hash matches?
              create EmailDelivery(PENDING, idempotency_key=<draft>#<n>), draft → SENDING
         → ProviderService.deliver             (Gmail → Mailchimp, retry, safe failover)
              each attempt → GraphRepository.record_attempt  (durable before next step)
         → GraphRepository.complete_delivery   draft → SENT | FAILED | UNKNOWN
     ← 200 SENT | 202 UNKNOWN | 502 FAILED

Concurrency model
-----------------

The API is asynchronous. All graph work runs on a single dedicated thread that owns the
only Relate connection, because Relate connections are thread-bound and a store accepts one
writer. Provider HTTP calls are asynchronous (``httpx.AsyncClient``) and do not block the
graph thread. The Gmail token refresh, which is synchronous in ``google-auth``, runs in a
worker thread.
