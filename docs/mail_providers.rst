Mail providers
==============

Both providers implement :class:`email_service.providers.base.EmailProvider`. A provider
never raises for transport or provider errors. It returns a ``ProviderResult`` whose
``outcome`` is ``ACCEPTED``, ``REJECTED`` (definitive, possibly ``retryable``) or
``UNKNOWN``.

Gmail (primary)
---------------

* Endpoint: ``POST https://gmail.googleapis.com/gmail/v1/users/me/messages/send`` with
  ``{"raw": <base64url RFC 5322 message>}``.
* Authentication: OAuth 2.0 user credentials (``google-auth``). The only scope is
  ``https://www.googleapis.com/auth/gmail.send``.
* Tokens: ``GMAIL_TOKEN_FILE`` is loaded once, refreshed when expired and rewritten
  atomically with mode ``0600``. A 401 response drops the cached credentials so they are
  reloaded on the next attempt.
* MIME: ``text/plain`` only, or ``multipart/alternative`` with the plain part first and the
  HTML part second. Headers include a deterministic ``Message-ID`` and
  ``X-Onboarding-Delivery-ID``.
* The ``From`` address must be the authorised account or one of its *Send mail as* aliases.

Mailchimp Transactional (secondary)
-----------------------------------

* Endpoint: ``POST https://mandrillapp.com/api/1.0/messages/send.json``. This is the
  Transactional (formerly Mandrill) API, not the Marketing API.
* Authentication: ``MAILCHIMP_TRANSACTIONAL_API_KEY`` in the JSON body (``key``). It is
  never logged, and the provider stays disabled while the key is empty.
* Payload: ``subject``, ``from_email``, optional ``from_name``, ``to`` (``type: to``),
  ``text``, optional ``html``, ``headers`` (``Message-Id``, ``X-Onboarding-Delivery-ID``),
  ``metadata.delivery_id``, ``preserve_recipients: true``, tracking off.
* Responses: an array of per-recipient results. ``sent``, ``queued`` and ``scheduled`` are
  accepted; ``rejected`` and ``invalid`` are refused. If at least one recipient is accepted,
  the delivery counts as accepted (``provider_status = partial``), because failing over would
  duplicate the message for the others. Per-recipient statuses are kept in the attempt
  record.
* Errors: ``{"status": "error", "name": …}``. ``Invalid_Key``, ``ValidationError``,
  ``PaymentRequired``, ``Unknown_Subaccount`` and ``Unknown_Template`` are definitive. Other
  names are classified by HTTP status, with server errors treated as UNKNOWN. During
  implementation an invalid key returned **HTTP 401** (older documentation shows HTTP 500),
  so the adapter classifies by response body.

Activation checklist:

#. Enable Transactional Email in the Mailchimp account (it is a paid add-on).
#. Add the sending domain and publish the DKIM and SPF records; wait for *verified*.
   Unverified domains produce ``rejected`` results with the reason ``unsigned``.
#. Create an API key and set ``MAILCHIMP_TRANSACTIONAL_API_KEY``.

Outcome classification
----------------------

.. list-table::
   :header-rows: 1

   * - Signal
     - Outcome
     - Retry / failover
   * - 2xx accepted
     - ACCEPTED
     - done
   * - Connect error, connect or pool timeout
     - REJECTED, retryable
     - retry, then failover
   * - HTTP 408 / 429
     - REJECTED, retryable
     - retry, then failover
   * - Other 4xx, definitive provider error, missing credentials
     - REJECTED
     - failover
   * - Read or write timeout, protocol error, 5xx
     - UNKNOWN
     - **none**

Safe failover and the UNKNOWN state
-----------------------------------

A timeout after the request was sent does not prove that the provider rejected the message.
Sending the same message through the other provider could deliver it twice. The provider
service therefore stops at the first UNKNOWN outcome. The draft becomes ``UNKNOWN``, and
both further sends and failover are blocked until a reviewer reconciles it:

.. code-block:: bash

   curl -X POST /api/v1/emails/$ID/reconcile -H "X-API-Key: $REVIEWER" \
        -d '{"outcome": "SENT", "note": "Found in Gmail Sent folder (Message-ID dlv-…)"}'

``FAILED`` reopens the draft for a new delivery, with a new sequence number and idempotency
key. Neither Gmail nor Mailchimp Transactional provides a send idempotency key, so these
application-level controls (atomic claim, unique delivery key, UNKNOWN state, reconciliation)
are the duplicate-delivery guarantee: *at most one automatic submission per approved
delivery*.

API reference
-------------

.. automodule:: email_service.providers.base
   :members: EmailProvider, OutboundEmail, classify_transport_error, classify_http_status

.. automodule:: email_service.services.provider_service
   :members: ProviderService, RetryPolicy
