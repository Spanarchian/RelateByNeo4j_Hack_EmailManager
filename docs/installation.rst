Installation
============

Requirements
------------

* Python 3.14 and `uv <https://docs.astral.sh/uv/>`_
* Docker with Compose v2 (for container deployment)
* Gmail API OAuth client credentials (for real sending)
* Optional: a Mailchimp Transactional API key with a verified sending domain

Relate by Neo4j needs no separate installation: the ``relate-db`` wheel (pinned to 1.0.1)
contains the embedded engine.

Local installation
------------------

.. code-block:: bash

   uv sync --all-groups
   cp .env.example .env
   uv run task api-key -- sam.submitter submitter
   uv run task api-key -- riley.reviewer reviewer
   # paste the printed JSON objects into API_KEYS in .env
   uv run task dev

Open http://127.0.0.1:8000/docs for the interactive API.

Gmail authorisation
-------------------

1. Enable the Gmail API in a Google Cloud project and configure the OAuth consent screen.
2. Create an *OAuth client ID* of type *Desktop app* and save it as
   ``secrets/credentials.json``.
3. Run ``uv run task auth`` (or ``uv run task auth -- --no-browser``). This writes
   ``secrets/token.json`` with only the ``gmail.send`` scope.

Apps whose consent screen is in *Testing* status receive refresh tokens that expire after
seven days. Publish the app, or use an *Internal* Workspace app, for continuous operation.

Docker
------

.. code-block:: bash

   uv run task deploy          # build, start, wait for the health check
   uv run task docker-token    # copy secrets/token.json into the gmail-token volume
   uv run task logs
   uv run task stop

The container listens on ``127.0.0.1:8000``. Put a TLS-terminating reverse proxy in front
of it (see :doc:`security`). Run exactly **one** container per ``relate-data`` volume.
