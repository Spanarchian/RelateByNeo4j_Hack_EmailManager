Agent workflow
==============

Two deterministic agents assist people. Neither can approve or send.

Onboarding agent (``agent:onboarding``)
---------------------------------------

#. Retrieve the onboarding (validated, stored fields only).
#. Select the template for ``(onboarding type, requested kind)``.
#. Build the template context from verified fields.
#. Render the subject, plain text and HTML safely.
#. Store an ``EmailDraft`` in ``DRAFT`` with ``prepared_by = agent:onboarding`` and
   ``requested_by = <API caller>``, and link ``(Agent)-[:PREPARED_FOR]->(Onboarding)``.
#. Submit it to the review agent and move it to ``PENDING_REVIEW`` with the report attached.

Review agent (``agent:review``)
-------------------------------

Produces a ``ReviewReport``: the content hash it reviewed, a timestamp and a list of
findings ``{code, severity, message, field}``. ``ERROR`` findings block approval.

.. list-table::
   :header-rows: 1

   * - Check
     - Examples of findings
   * - Sender
     - ``sender.not_allowed`` (error), ``sender.no_allow_list`` (warning)
   * - Recipients
     - ``recipients.too_many``, ``recipients.identity_mismatch`` (errors),
       ``recipients.additional`` (warning)
   * - Identity
     - ``identity.organisation_missing``, ``identity.role_missing`` (errors),
       ``identity.name_missing`` (warning)
   * - Dates
     - ``dates.start_in_past``, ``dates.start_missing`` (errors),
       ``dates.start_far_future`` (warning)
   * - Placeholders
     - ``content.placeholder``: ``{{ }}``, ``[Insert …]``, ``TODO``, ``TBC`` (error)
   * - Content
     - ``content.unsafe_html`` (error), ``content.insecure_link``, ``content.too_short``
       (warnings)
   * - Sensitive data
     - NI numbers, SSNs, IBANs, Luhn-valid card numbers, credentials (errors); date of
       birth, salary, medical and similar topics (warnings)

Isolation and extension
-----------------------

* Agents receive only the repository, renderer and rules. They hold no reference to the
  approval service or the email providers.
* Agent ids start with ``agent:``. Configuration rejects API keys with such names, so an
  agent can never authenticate as a reviewer.
* Rules are plain callables, ``(EmailContent, ReviewContext) -> list[ReviewFinding]``.
  A future LLM-based rule can be added to ``ReviewAgent(rules=[...])`` without changing
  approval or delivery code. No LLM dependency is included today.
