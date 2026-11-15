Graph model
===========

Relate by Neo4j
---------------

`Relate <https://relatedb.ai>`_ is an embedded, in-process Cypher graph database. The
application uses its official Python client, ``relate-db`` 1.0.1 (``import relate``). The
following behaviour was verified from the documentation and by probing the installed engine:

* ``relate.connect(path, read_only=False)`` opens a directory store, a single ``.rl`` file
  or ``:memory:``.
* Statements run synchronously, and a connection belongs to the thread that opened it.
* A store accepts **one writable connection across all processes**. A second open fails with
  ``storage locked``. Directory stores can be opened ``read_only=True`` beside the writer.
* ``conn.autocommit = False`` gives explicit transactions. Write-write conflicts raise an
  error with ``retryable = True``.
* ``CREATE CONSTRAINT … IF NOT EXISTS … REQUIRE n.id IS UNIQUE`` is supported and enforced.
* Map values cannot be stored as properties (``InvalidPropertyType``); lists, dates and
  zoned datetimes can.

Nodes
-----

.. list-table::
   :header-rows: 1

   * - Label
     - Key properties
   * - ``Person``
     - ``id``, ``name``, ``email``
   * - ``Organisation``
     - ``id``, ``name``
   * - ``Team``
     - ``id``, ``name``
   * - ``Onboarding``
     - ``id``, ``type``, ``status``, ``start_date``, ``role_title``, ``contact_name``,
       ``team_id``, ``details_json``, ``created_by``, ``created_at``
   * - ``EmailTemplate``
     - ``id``, ``version``, ``kind``, ``onboarding_type``, ``subject``, ``text``, ``html``,
       ``required_fields``
   * - ``EmailDraft``
     - ``id``, ``status``, ``sender``, ``recipients``, ``subject``, ``body``, ``html_body``,
       ``content_hash``, ``version``, ``delivery_count``, ``review_json``, ``prepared_by``,
       ``requested_by``
   * - ``Agent``
     - ``id`` (``agent:onboarding``, ``agent:review``), ``name``, ``kind``
   * - ``Approval``
     - ``id``, ``decision``, ``reviewer``, ``content_hash``, ``decided_at``, ``valid``,
       ``invalidated_at``, ``invalidated_reason``
   * - ``EmailDelivery``
     - ``id``, ``idempotency_key``, ``sequence``, ``status``, ``provider``,
       ``provider_message_id``, ``attempts_json``, ``reconciled_by``
   * - ``EmailProvider``
     - ``id`` (``gmail``, ``mailchimp``), ``role``, ``enabled``

Relationships
-------------

.. code-block:: text

   (Person)-[:UNDERGOES]->(Onboarding)
   (Onboarding)-[:FOR_ORGANISATION]->(Organisation)
   (Person)-[:MEMBER_OF]->(Team)-[:PART_OF]->(Organisation)
   (Onboarding)-[:USES_TEMPLATE]->(EmailTemplate)
   (Agent)-[:PREPARED_FOR]->(Onboarding)
   (Onboarding)-[:HAS_DRAFT]->(EmailDraft)
   (EmailDraft)-[:HAS_APPROVAL]->(Approval)
   (EmailDraft)-[:HAS_DELIVERY]->(EmailDelivery)
   (EmailDelivery)-[:SENT_THROUGH]->(EmailProvider)

Stable identifiers and idempotency
----------------------------------

People, organisations, teams and onboardings use ``uuid5`` identifiers derived from their
natural keys, so creating the same onboarding twice returns the existing node. Templates,
agents and providers are ``MERGE``\ d on every start-up. ``task graph-seed`` can therefore be
run repeatedly without changing anything.

Queries
-------

All Cypher lives in ``email_service/graph/queries.py`` and uses ``$parameters`` only.
Examples:

.. code-block:: cypher

   // Pending approvals
   MATCH (d:EmailDraft) WHERE d.status IN ['PENDING_REVIEW'] RETURN properties(d) AS d

   // Failed or unknown deliveries
   MATCH (:EmailDraft)-[:HAS_DELIVERY]->(x:EmailDelivery)
   WHERE x.status IN ['FAILED', 'UNKNOWN'] RETURN properties(x) AS x

Inspecting the store
--------------------

``uv run task graph-check`` opens the store read-only and prints counts, constraints,
pending approvals, unsent emails and failed deliveries. It works while the API is running.
The optional ``relate`` CLI can also read the store:
``relate --database data/graph/onboarding --read-only "MATCH (n) RETURN labels(n), count(*)"``.
