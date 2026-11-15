Approvals
=========

States
------

``DRAFT``, ``PENDING_REVIEW``, ``APPROVED``, ``REJECTED``, ``SENDING``, ``SENT``,
``FAILED``, ``UNKNOWN`` and ``CANCELLED``. Transitions are defined in one table,
``email_service.onboarding.workflow.ALLOWED_TRANSITIONS``.

.. code-block:: text

   DRAFT → PENDING_REVIEW → APPROVED → SENDING → SENT
                 │  └→ REJECTED            ├→ FAILED  → SENDING (approval still valid)
                 │                         └→ UNKNOWN → SENT | FAILED (human reconcile)
   edit: PENDING_REVIEW / APPROVED / REJECTED / FAILED → PENDING_REVIEW (approvals invalidated)
   cancel: any unsent, non-sending state → CANCELLED

Content hash
------------

``compute_content_hash`` is SHA-256 over versioned canonical JSON of the sender,
recipients (in order), subject, plain-text body and HTML body. Changing any of these
changes the hash. Address case does not.

Approving
---------

``POST /api/v1/emails/{id}/approve`` with ``{"content_hash": "<hash you reviewed>"}``.
The approval is recorded only if all of these hold:

* the caller is a **human** with the ``reviewer`` role;
* the caller is not the identity that prepared the draft;
* with ``APPROVAL_REQUIRE_FOUR_EYES=true``, the caller did not request the draft;
* the draft is ``PENDING_REVIEW``;
* the submitted hash equals the current content hash;
* the review report matches the current content and has no ``ERROR`` findings.

The stored ``Approval`` holds the draft id, reviewer identity, timestamp, decision, approved
hash and validity. Earlier approvals are invalidated (``superseded``).

Rejecting
---------

``POST /api/v1/emails/{id}/reject`` with a reason. A rejection never authorises sending.
Edit the draft to send it back for review.

Sending
-------

``POST /api/v1/emails/{id}/send`` (human reviewer). In one atomic step the service verifies
the status, re-checks the approval against a freshly **recomputed** hash, creates a durable
``EmailDelivery`` with a unique idempotency key and moves the draft to ``SENDING``. Only
then is a provider called. Concurrent or repeated requests receive ``409
duplicate_submission``.

Editing
-------

``PATCH /api/v1/emails/{id}`` (optionally with ``If-Match: <version>``) re-runs the review
agent, invalidates every approval (``content edited``) and returns the draft to
``PENDING_REVIEW``. If content changes outside the API, the send-time hash check refuses
with ``409 approval_integrity``.
