Onboarding
==========

Volunteer onboarding
--------------------

Fields: ``name``, ``email``, ``organisation``, ``volunteer_role``, ``coordinator``,
``start_date``, ``induction_details`` and an optional ``team`` (default ``Volunteers``).
Templates: ``welcome`` and ``induction``.

.. code-block:: json

   {"type": "volunteer", "name": "Alex Morgan", "email": "alex.morgan@example.org",
    "organisation": "Riverside Community Food Bank", "volunteer_role": "Warehouse Volunteer",
    "coordinator": "Priya Shah", "team": "Saturday Warehouse", "start_date": "2027-02-06",
    "induction_details": "Saturday 30 January 2027, 10:00-12:30 at the Riverside warehouse."}

Employee onboarding
-------------------

Fields: ``name``, ``email``, ``organisation``, ``job_title``, ``department``, ``manager``,
``start_date``, ``first_day_arrangements``. The department becomes the person's team.
Templates: ``welcome`` and ``first_day``.

Templates
---------

.. list-table::
   :header-rows: 1

   * - Id
     - Subject
   * - ``volunteer-welcome``
     - ``Welcome to {{ organisation_name }}!``
   * - ``volunteer-induction``
     - ``Your volunteer induction – {{ organisation_name }}``
   * - ``employee-welcome``
     - ``Welcome to {{ organisation_name }} – your new role``
   * - ``employee-first-day``
     - ``Your first day at {{ organisation_name }}``

Each template has a plain-text and an HTML body. Rendering is safe:

* Jinja2 ``SandboxedEnvironment``: templates cannot reach Python internals.
* ``StrictUndefined``: a missing value is an error, never an empty string.
* Every declared ``required_fields`` value must be non-blank.
* Values containing template syntax are rejected, so no second-order injection is possible.
* HTML is auto-escaped; plain text is not.
* After rendering, any remaining ``{{``, ``{%`` or ``{#`` is rejected.

Only validated onboarding fields reach the template context; the person's email address is
used only as the recipient. Dates are rendered unambiguously, for example
``Monday 7 December 2026``.

Lifecycle
---------

``POST /api/v1/onboarding`` is idempotent: the same type, email, organisation and start date
return the existing onboarding (200 instead of 201). An onboarding may have at most one
non-cancelled draft per template. Cancel a draft to prepare a fresh one.

Sample data in ``data/samples/onboardings.json`` is fictional and uses reserved
``example.org`` and ``example.com`` addresses.
