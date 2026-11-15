"""Graph model: labels, relationship types, constraints and stable identifiers."""

from __future__ import annotations

import uuid
from datetime import date
from enum import StrEnum

#: Namespace for deterministic (uuid5) identifiers. Never change it: ids are persisted.
ID_NAMESPACE = uuid.UUID("6f1c5d1e-2b8a-4c55-9a3f-7d5e0c9b1a42")


class Label(StrEnum):
    PERSON = "Person"
    ORGANISATION = "Organisation"
    TEAM = "Team"
    ONBOARDING = "Onboarding"
    EMAIL_TEMPLATE = "EmailTemplate"
    EMAIL_DRAFT = "EmailDraft"
    AGENT = "Agent"
    APPROVAL = "Approval"
    EMAIL_DELIVERY = "EmailDelivery"
    EMAIL_PROVIDER = "EmailProvider"


class Rel(StrEnum):
    UNDERGOES = "UNDERGOES"  # Person -> Onboarding
    FOR_ORGANISATION = "FOR_ORGANISATION"  # Onboarding -> Organisation
    MEMBER_OF = "MEMBER_OF"  # Person -> Team
    PART_OF = "PART_OF"  # Team -> Organisation
    USES_TEMPLATE = "USES_TEMPLATE"  # Onboarding -> EmailTemplate
    PREPARED_FOR = "PREPARED_FOR"  # Agent -> Onboarding
    HAS_DRAFT = "HAS_DRAFT"  # Onboarding -> EmailDraft
    HAS_APPROVAL = "HAS_APPROVAL"  # EmailDraft -> Approval
    HAS_DELIVERY = "HAS_DELIVERY"  # EmailDraft -> EmailDelivery
    SENT_THROUGH = "SENT_THROUGH"  # EmailDelivery -> EmailProvider


#: One uniqueness constraint per label on ``id``, plus delivery idempotency keys.
CONSTRAINTS: tuple[str, ...] = (
    *(
        f"CREATE CONSTRAINT {label.value.lower()}_id IF NOT EXISTS "
        f"FOR (n:{label.value}) REQUIRE n.id IS UNIQUE"
        for label in Label
    ),
    "CREATE CONSTRAINT emaildelivery_idempotency IF NOT EXISTS "
    "FOR (n:EmailDelivery) REQUIRE n.idempotency_key IS UNIQUE",
    "CREATE CONSTRAINT emaildraft_idempotency IF NOT EXISTS "
    "FOR (n:EmailDraft) REQUIRE n.idempotency_key IS UNIQUE",
)


def _stable(prefix: str, *parts: str) -> str:
    return f"{prefix}-{uuid.uuid5(ID_NAMESPACE, '|'.join(parts))}"


def person_id(email: str) -> str:
    return _stable("person", email.strip().lower())


def organisation_id(name: str) -> str:
    return _stable("org", " ".join(name.split()).casefold())


def team_id(org_id: str, name: str) -> str:
    return _stable("team", org_id, " ".join(name.split()).casefold())


def onboarding_id(onboarding_type: str, email: str, org_id: str, start: date) -> str:
    return _stable("onb", onboarding_type, email.strip().lower(), org_id, start.isoformat())


def draft_id_for_idempotency_key(subject: str, key: str) -> str:
    return _stable("draft", subject, key)


def delivery_idempotency_key(draft_id: str, sequence: int) -> str:
    return f"{draft_id}#{sequence}"


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4()}"
