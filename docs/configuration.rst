Configuration
=============

Settings are read from the environment and an optional ``.env`` file by
``email_service.config.Settings`` (pydantic-settings). Secrets are ``SecretStr`` values and
never appear in logs or representations.

.. list-table::
   :header-rows: 1
   :widths: 32 18 50

   * - Variable
     - Default
     - Meaning
   * - ``APP_ENV``
     - ``development``
     - ``production`` refuses an in-memory store and requires ``API_KEYS``.
   * - ``API_KEYS``
     - ``[]``
     - JSON array of ``{"name", "key_sha256", "roles", "kind"}``. ``kind`` is ``human``
       (default) or ``service``. Names starting with ``agent:`` are rejected, as are
       service clients with the reviewer role.
   * - ``APPROVAL_REQUIRE_FOUR_EYES``
     - ``true``
     - The requester of a draft may not approve it.
   * - ``RATE_LIMIT_PER_MINUTE``
     - ``120``
     - Requests per minute per API key (or per IP address without a key); ``0`` disables.
   * - ``RELATE_STORE_PATH``
     - ``data/graph/onboarding``
     - Relate directory store, ``*.rl`` single-file store, or ``:memory:``.
   * - ``EMAIL_PRIMARY_PROVIDER``
     - ``gmail``
     - ``gmail`` or ``mailchimp``.
   * - ``EMAIL_SECONDARY_PROVIDER``
     - ``mailchimp``
     - A provider name, or ``none``. Must differ from the primary.
   * - ``EMAIL_FAILOVER_ENABLED``
     - ``true``
     - Allow failover after a definitive primary failure.
   * - ``EMAIL_DEFAULT_SENDER``
     - unset
     - Sender for agent-prepared drafts.
   * - ``EMAIL_ALLOWED_SENDERS``
     - empty
     - Comma-separated allow-list enforced by the review agent.
   * - ``EMAIL_MAX_RECIPIENTS``
     - ``20``
     - Recipient limit (1–100).
   * - ``PROVIDER_TIMEOUT_SECONDS``
     - ``15``
     - HTTP timeout per provider call.
   * - ``PROVIDER_MAX_RETRIES``
     - ``2``
     - Retries of *retryable* definitive failures on the same provider.
   * - ``PROVIDER_RETRY_BACKOFF_SECONDS``
     - ``1.0``
     - Base of the exponential backoff (capped at 30 s).
   * - ``GMAIL_CREDENTIALS_FILE``
     - ``secrets/credentials.json``
     - OAuth client file (used only by ``task auth``).
   * - ``GMAIL_TOKEN_FILE``
     - ``secrets/token.json``
     - Authorised user token; refreshed and rewritten automatically.
   * - ``MAILCHIMP_TRANSACTIONAL_API_KEY``
     - empty
     - Enables Mailchimp Transactional when set.
   * - ``MAILCHIMP_FROM_NAME``
     - empty
     - Optional display name for Mailchimp messages.

Example ``API_KEYS`` entry (from ``uv run task api-key -- riley.reviewer reviewer``):

.. code-block:: json

   [{"name": "riley.reviewer", "key_sha256": "ad5f…c17", "roles": ["reviewer"], "kind": "human"}]
