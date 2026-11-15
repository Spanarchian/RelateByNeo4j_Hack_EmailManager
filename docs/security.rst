Security
========

Authentication and authorisation
--------------------------------

* ``X-API-Key`` on every ``/api/v1`` request. Only SHA-256 digests are configured
  (``uv run task api-key``), and comparison is constant-time over all entries.
* Roles ``submitter``, ``reviewer`` and ``admin``. Approve, send and reconcile require a
  **human** principal with the reviewer role. Service clients cannot be reviewers, and
  ``admin`` cannot approve or send.
* Agents (``agent:*``) are internal identities, distinct from every reviewer, and can never
  hold an API key.
* Approval integrity: approval is bound to a SHA-256 content hash, re-verified immediately
  before sending, and invalidated by any edit.

Secrets
-------

* ``.env``, ``secrets/``, ``credentials.json`` and ``token.json`` are excluded by
  ``.gitignore`` and ``.dockerignore`` and omitted from the distribution ZIP.
* Settings use ``SecretStr``. The Mailchimp key appears only in outbound request bodies.
* The Gmail token is written atomically with mode ``0600``. In Docker it lives in the
  ``gmail-token`` volume owned by uid 10001.
* No credentials are stored in the graph.

Logging
-------

Logs are JSON lines. Bearer tokens, Google access and refresh tokens, API keys, and
``key``/``password``/``secret``/``token`` values are redacted. Email addresses are reduced to
``***@domain``. Only allow-listed structured fields are emitted. ``httpx`` request logging is
silenced.

Input handling
--------------

Pydantic validation for every request; RFC-compliant email validation; header injection
(CR/LF in subjects) rejected; recipient limits; Jinja2 sandbox with auto-escaped HTML; the
review agent flags unsafe HTML. There is no endpoint that accepts Cypher, and all queries
use bound parameters.

HTTPS deployment
----------------

The container publishes ``127.0.0.1:8000`` only. Terminate TLS in a reverse proxy, for
example nginx (certificate paths shown as placeholders):

.. code-block:: nginx

   limit_req_zone $binary_remote_addr zone=onboarding:10m rate=5r/s;

   server {
       listen 443 ssl;
       server_name onboarding.example.org;
       ssl_certificate     /etc/ssl/certs/onboarding.pem;
       ssl_certificate_key /etc/ssl/private/onboarding.key;

       location / {
           limit_req zone=onboarding burst=20 nodelay;
           proxy_pass http://127.0.0.1:8000;
           proxy_set_header Host $host;
           proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
           proxy_set_header X-Forwarded-Proto $scheme;
       }
   }

Set ``FORWARDED_ALLOW_IPS`` (read by uvicorn) to the proxy's address if client IP
addresses matter.

Rate limiting
-------------

An in-process fixed-window limiter (``RATE_LIMIT_PER_MINUTE``) keys on the API key, or the
client IP address without one; ``/health`` is exempt. Because the service is single-process
by design, the in-process limiter is authoritative. Add proxy limits as defence in depth.

Persistent data protection
--------------------------

The Relate store holds personal data (names and email addresses of people being
onboarded). Keep the ``relate-data`` volume on encrypted storage, restrict host access, and
back it up by stopping the container and copying the volume, or by reading it with a
read-only connection. The container runs non-root with a read-only root filesystem,
``cap_drop: ALL`` and ``no-new-privileges``.
