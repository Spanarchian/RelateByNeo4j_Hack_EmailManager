API
===

The service is documented by OpenAPI at ``/docs`` (Swagger UI) and ``/openapi.json``.
Authenticate with the ``X-API-Key`` header.

Endpoints
---------

.. list-table::
   :header-rows: 1
   :widths: 10 42 18 30

   * - Method
     - Path
     - Permission
     - Notes
   * - GET
     - ``/health``
     - none
     - 200 ok / 503 if the graph is unavailable
   * - POST
     - ``/api/v1/emails``
     - submit_email
     - 201 draft (``PENDING_REVIEW``); 200 on ``Idempotency-Key`` replay
   * - GET
     - ``/api/v1/emails/pending-review``
     - read
     -
   * - GET
     - ``/api/v1/emails/unsent``
     - read
     - onboarding drafts not yet sent
   * - GET
     - ``/api/v1/emails/failed-deliveries``
     - read
     - FAILED and UNKNOWN deliveries
   * - GET
     - ``/api/v1/emails/{id}``
     - read
     - draft, approvals, deliveries
   * - PATCH
     - ``/api/v1/emails/{id}``
     - edit_draft
     - invalidates approvals; ``If-Match: <version>``
   * - POST
     - ``/api/v1/emails/{id}/approve``
     - approve (human)
     - body ``{"content_hash", "comment"}``
   * - POST
     - ``/api/v1/emails/{id}/reject``
     - approve (human)
     - body ``{"reason"}``
   * - POST
     - ``/api/v1/emails/{id}/send``
     - send (human)
     - 200 SENT, 202 UNKNOWN, 502 FAILED
   * - POST
     - ``/api/v1/emails/{id}/reconcile``
     - reconcile (human)
     - body ``{"outcome": "SENT"|"FAILED", "note"}``
   * - POST
     - ``/api/v1/emails/{id}/cancel``
     - cancel_draft
     -
   * - POST
     - ``/api/v1/onboarding``
     - create_onboarding
     - 201 created / 200 existing
   * - GET
     - ``/api/v1/onboarding?type=volunteer|employee``
     - read
     -
   * - GET
     - ``/api/v1/onboarding/{id}``
     - read
     -
   * - POST
     - ``/api/v1/onboarding/{id}/draft``
     - prepare_draft
     - body ``{"template": "welcome"|"induction"|"first_day"}``
   * - GET
     - ``/api/v1/onboarding/{id}/history``
     - read
     - chronological audit events

Roles
-----

=============  ===================================================================
Role           Permissions
=============  ===================================================================
submitter      read, create_onboarding, prepare_draft, submit_email, edit, cancel
reviewer       read, edit, cancel, approve, send, reconcile (human principals only)
admin          everything except approve and send
=============  ===================================================================

Errors
------

All errors share one shape. Validation errors list locations and messages but never echo
input values.

.. code-block:: json

   {"error": {"code": "approval_integrity",
              "message": "draft content no longer matches the approved content; re-approval is required",
              "details": {"approved_hash": "…", "current_hash": "…"}}}

Codes: ``unauthenticated`` (401), ``forbidden`` (403), ``not_found`` (404), ``conflict``,
``invalid_state``, ``duplicate_submission``, ``approval_integrity`` (409),
``validation_failed``, ``template_error`` (422), ``rate_limited`` (429),
``graph_unavailable`` (503), ``internal_error`` (500).
