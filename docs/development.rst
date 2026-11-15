Development
===========

.. code-block:: bash

   uv sync --all-groups
   uv run task dev        # reload on change
   uv run task quality    # ruff check + ruff format --check + mypy (strict) + tests
   uv run task docs       # Sphinx, warnings are errors

Layout
------

.. code-block:: text

   src/email_service/   application package (see Architecture)
   tests/unit|integration|e2e
   scripts/             seed_graph.py, check_graph.py, generate_api_key.py
   data/samples/        fictional onboarding records
   docs/                Sphinx sources, SAD.md

Conventions
-----------

* Type hints everywhere; ``mypy --strict`` must pass.
* Business rules live in services, agents and ``approvals/``. Cypher lives only in
  ``graph/queries.py``. HTTP lives only in ``providers/`` and ``api/``.
* New provider: implement ``EmailProvider``, map responses to ACCEPTED / REJECTED /
  UNKNOWN conservatively, and register it in ``build_providers``.
* New review rule: write a function ``(EmailContent, ReviewContext) -> list[ReviewFinding]``
  and add it to ``DEFAULT_RULES``.
* Schema changes: add constraints to ``graph/schema.py``. They run idempotently at start-up.

Upgrading Relate
----------------

``relate-db`` is pinned (``==1.0.1``). Before upgrading, read the Relate changelog, then run
``uv run task test-graph``. The integration tests exercise the behaviour the adapter relies on
(constraints, locking, read-only readers, transactions and value types).

Continuous integration
----------------------

``.github/workflows/tests.yml`` installs Python 3.14 and uv, syncs the locked environment,
and runs Ruff, mypy, the unit, integration and E2E suites, the coverage gate and the Sphinx
build. No Gmail or Mailchimp credentials are needed.
