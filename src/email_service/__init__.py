"""Gmail Onboarding Assistant.

An API-first service that prepares, reviews, approves and sends onboarding
emails for volunteers and employees. Graph data is stored in Relate by Neo4j,
mail is delivered through Gmail (primary) or Mailchimp Transactional
(secondary), and no onboarding email is ever sent without human approval.
"""

__version__ = "0.1.0"
