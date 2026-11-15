Gmail Onboarding Assistant
==========================

An API-first service that prepares, reviews, approves and sends onboarding emails for
volunteers and employees. Gmail is the primary provider and Mailchimp Transactional the
secondary one, Relate by Neo4j is the embedded graph store, and **no email is ever sent
without explicit human approval**.

.. toctree::
   :maxdepth: 2
   :caption: Using the service

   installation
   configuration
   onboarding
   approvals
   api

.. toctree::
   :maxdepth: 2
   :caption: Design

   architecture
   graph_model
   mail_providers
   agent_workflow
   security
   SAD

.. toctree::
   :maxdepth: 2
   :caption: Contributing

   testing
   development
