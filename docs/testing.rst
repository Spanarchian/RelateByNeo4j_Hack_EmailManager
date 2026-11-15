Testing
=======

.. code-block:: bash

   uv run task test-all           # everything + coverage gate (fail under 85 %)
   uv run task test-unit          # -m unit
   uv run task test-integration   # -m integration
   uv run task test-e2e           # -m e2e
   uv run task test-graph         # -m graph
   uv run task test-providers     # -m providers
   uv run task test-workflow      # -m workflow

Markers
-------

``unit``, ``integration``, ``e2e``, ``external``, plus the topic markers ``graph``,
``providers`` and ``workflow``. ``--strict-markers`` is on.

What is real and what is mocked
-------------------------------

* **Relate is real** in every test: ``:memory:`` stores for speed, and temporary directory
  stores for persistence, reopen and single-writer locking tests.
* **Gmail and Mailchimp are mocked at the HTTP layer** with `respx`, using response shapes
  taken from the official APIs and from real error responses observed during implementation.
  The adapters, MIME generation and classification code all run for real.
* Gmail token refresh is tested by patching ``Credentials.refresh``, so no network is used.
* The application is exercised through FastAPI's ``TestClient``, including its lifespan
  (schema creation, seeding, recovery).

External tests
--------------

Real sends are opt-in and never run in CI:

.. code-block:: bash

   RUN_EXTERNAL_TESTS=1 EXTERNAL_TEST_RECIPIENT=you@example.com \
   EXTERNAL_TEST_SENDER=you@gmail.com GMAIL_TOKEN_FILE=secrets/token.json \
   MAILCHIMP_TRANSACTIONAL_API_KEY=… uv run pytest -m external

Key scenarios
-------------

Complete volunteer and employee onboarding; rejection; approval; delivery; Gmail failure
with Mailchimp failover; Gmail timeout producing UNKNOWN (with no retry and no failover)
followed by reconciliation; duplicate and concurrent send prevention; edited-draft approval
invalidation; out-of-band tampering detection; role, agent and four-eyes restrictions.
