"""Agent identity and the isolated interfaces agents implement.

Agents are deterministic today. The ``ReviewRule`` and ``AgentIdentity``
interfaces are the seams for adding an LLM-backed rule or composer later
without touching the approval or delivery code: agents receive only the data
they need and hold no reference to the approval service or email providers,
so they cannot approve or send anything.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from email_service.models.approval import ReviewFinding
from email_service.models.email import EmailContent
from email_service.models.onboarding import Onboarding

AGENT_PREFIX = "agent:"


@dataclass(frozen=True, slots=True)
class AgentIdentity:
    id: str
    name: str
    kind: str
    description: str

    def __post_init__(self) -> None:
        if not self.id.startswith(AGENT_PREFIX):
            raise ValueError("agent ids must start with 'agent:' to stay distinct from humans")


ONBOARDING_AGENT = AgentIdentity(
    id="agent:onboarding",
    name="Onboarding agent",
    kind="onboarding",
    description="Selects a template, populates verified onboarding data and prepares drafts.",
)
REVIEW_AGENT = AgentIdentity(
    id="agent:review",
    name="Review agent",
    kind="review",
    description="Runs deterministic quality and safety checks; never approves.",
)
ALL_AGENTS = (ONBOARDING_AGENT, REVIEW_AGENT)


@dataclass(frozen=True, slots=True)
class ReviewContext:
    """What a review is checked against. ``None`` fields skip the related checks."""

    today: date
    max_recipients: int
    allowed_senders: tuple[str, ...] = ()
    expected_recipient: str | None = None
    person_name: str | None = None
    organisation_name: str | None = None
    role_title: str | None = None
    start_date: date | None = None
    formatted_start_date: str | None = None


#: A review rule: pure function from content and context to findings.
type ReviewRule = Callable[[EmailContent, ReviewContext], list[ReviewFinding]]


def build_review_context(
    onboarding: Onboarding | None,
    *,
    today: date,
    max_recipients: int,
    allowed_senders: tuple[str, ...],
) -> ReviewContext:
    """Context for reviewing a draft; onboarding drafts get identity and date checks."""
    if onboarding is None:
        return ReviewContext(
            today=today, max_recipients=max_recipients, allowed_senders=allowed_senders
        )
    from email_service.onboarding.workflow import format_date

    return ReviewContext(
        today=today,
        max_recipients=max_recipients,
        allowed_senders=allowed_senders,
        expected_recipient=onboarding.person_email,
        person_name=onboarding.person_name,
        organisation_name=onboarding.organisation_name,
        role_title=onboarding.role_title,
        start_date=onboarding.start_date,
        formatted_start_date=format_date(onboarding.start_date),
    )
