from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from email_service.agents.base import AgentIdentity, ReviewContext
from email_service.agents.review_agent import ReviewAgent
from email_service.models.approval import Severity
from email_service.models.email import EmailContent

pytestmark = [pytest.mark.unit, pytest.mark.workflow]

GOOD_BODY = (
    "Dear Alex,\n\nWelcome to Riverside Food Bank! You start as a Warehouse Volunteer on "
    "Monday 7 December 2026. Priya will meet you.\n"
)


def ctx(**overrides: Any) -> ReviewContext:
    data: dict[str, Any] = {
        "today": date(2026, 11, 13),
        "max_recipients": 3,
        "allowed_senders": ("sender@example.org",),
        "expected_recipient": "alex@example.org",
        "person_name": "Alex Morgan",
        "organisation_name": "Riverside Food Bank",
        "role_title": "Warehouse Volunteer",
        "start_date": date(2026, 12, 7),
        "formatted_start_date": "Monday 7 December 2026",
    }
    data.update(overrides)
    return ReviewContext(**data)


def content(**overrides: Any) -> EmailContent:
    data: dict[str, Any] = {
        "sender": "sender@example.org",
        "recipients": ["alex@example.org"],
        "subject": "Welcome to Riverside Food Bank!",
        "body": GOOD_BODY,
        "html_body": "<p>Welcome</p>",
    }
    data.update(overrides)
    return EmailContent(**data)


def codes(
    report_content: EmailContent, context: ReviewContext | None = None
) -> dict[str, Severity]:
    agent = ReviewAgent(clock=lambda: datetime(2026, 11, 13, tzinfo=UTC))
    report = agent.review(report_content, context or ctx())
    return {f.code: f.severity for f in report.findings}


def test_clean_draft_passes() -> None:
    agent = ReviewAgent()
    report = agent.review(content(), ctx())
    assert report.passed, report.findings
    assert report.agent_id == "agent:review"
    assert len(report.content_hash) == 64


@pytest.mark.parametrize(
    ("change", "context_change", "code", "severity"),
    [
        ({"sender": "other@example.org"}, {}, "sender.not_allowed", Severity.ERROR),
        ({}, {"allowed_senders": ()}, "sender.no_allow_list", Severity.WARNING),
        ({"recipients": ["bob@example.org"]}, {}, "recipients.identity_mismatch", Severity.ERROR),
        (
            {"recipients": ["alex@example.org", "boss@example.org"]},
            {},
            "recipients.additional",
            Severity.WARNING,
        ),
        (
            {"recipients": [f"p{i}@example.org" for i in range(4)]},
            {"expected_recipient": None},
            "recipients.too_many",
            Severity.ERROR,
        ),
        (
            {"recipients": ["alex@example.org", "sender@example.org"]},
            {},
            "sender.is_recipient",
            Severity.WARNING,
        ),
        ({"body": GOOD_BODY.replace("Alex", "Sam")}, {}, "identity.name_missing", Severity.WARNING),
        (
            {"body": GOOD_BODY.replace("Riverside Food Bank", "Hilltop"), "subject": "Hi"},
            {},
            "identity.organisation_missing",
            Severity.ERROR,
        ),
        (
            {"body": GOOD_BODY.replace("Warehouse Volunteer", "helper")},
            {},
            "identity.role_missing",
            Severity.ERROR,
        ),
        ({}, {"start_date": date(2026, 11, 1)}, "dates.start_in_past", Severity.ERROR),
        ({}, {"start_date": date(2028, 1, 1)}, "dates.start_far_future", Severity.WARNING),
        (
            {"body": GOOD_BODY.replace("Monday 7 December 2026", "next week")},
            {},
            "dates.start_missing",
            Severity.ERROR,
        ),
        ({"subject": "Welcome {{ name }}"}, {}, "content.placeholder", Severity.ERROR),
        ({"body": GOOD_BODY + " [Insert parking info]"}, {}, "content.placeholder", Severity.ERROR),
        ({"body": GOOD_BODY + " Details TBC."}, {}, "content.placeholder", Severity.ERROR),
        (
            {"html_body": "<p onclick='x()'>Hi</p>"},
            {},
            "content.unsafe_html",
            Severity.ERROR,
        ),
        ({"html_body": "<script>alert(1)</script>"}, {}, "content.unsafe_html", Severity.ERROR),
        (
            {"body": GOOD_BODY + " http://example.org"},
            {},
            "content.insecure_link",
            Severity.WARNING,
        ),
        ({"html_body": None}, {}, "content.plain_text_only", Severity.INFO),
        ({"body": GOOD_BODY + " NI: AB 12 34 56 C"}, {}, "sensitive.uk_ni_number", Severity.ERROR),
        ({"body": GOOD_BODY + " SSN 123-45-6789"}, {}, "sensitive.us_ssn", Severity.ERROR),
        (
            {"body": GOOD_BODY + " Pay to GB82 WEST 1234 5698 7654 32"},
            {},
            "sensitive.iban",
            Severity.ERROR,
        ),
        ({"body": GOOD_BODY + " password: hunter2"}, {}, "sensitive.credential", Severity.ERROR),
        (
            {"body": GOOD_BODY + " Card 4111 1111 1111 1111"},
            {},
            "sensitive.payment_card",
            Severity.ERROR,
        ),
        (
            {"body": GOOD_BODY + " Please bring your date of birth certificate."},
            {},
            "sensitive.personal_detail",
            Severity.WARNING,
        ),
    ],
)
def test_rules(
    change: dict[str, Any], context_change: dict[str, Any], code: str, severity: Severity
) -> None:
    found = codes(content(**change), ctx(**context_change))
    assert found.get(code) == severity, found


def test_non_luhn_numbers_are_not_cards() -> None:
    assert "sensitive.payment_card" not in codes(
        content(body=GOOD_BODY + " Ref 1234 5678 9012 3456")
    )


def test_short_and_long_bodies_warn() -> None:
    short = codes(content(body="Hi Alex, Warehouse Volunteer, Monday 7 December 2026"))
    assert short.get("content.too_short") is None  # 52 chars: not short
    tiny_ctx = ctx(person_name=None, organisation_name=None, role_title=None, start_date=None)
    assert codes(content(body="Hi there"), tiny_ctx)["content.too_short"] == Severity.WARNING
    assert codes(content(body="x " * 10_001), tiny_ctx)["content.too_long"] == Severity.WARNING


def test_custom_rules_are_pluggable() -> None:
    from email_service.models.approval import ReviewFinding

    def no_exclamations(c: EmailContent, _: ReviewContext) -> list[ReviewFinding]:
        return (
            [ReviewFinding(code="style.exclamation", severity=Severity.INFO, message="!")]
            if "!" in c.subject
            else []
        )

    agent = ReviewAgent([no_exclamations])
    assert [f.code for f in agent.review(content(), ctx()).findings] == ["style.exclamation"]


def test_agent_identity_must_be_namespaced() -> None:
    with pytest.raises(ValueError, match="agent:"):
        AgentIdentity(id="reviewer", name="x", kind="review", description="x")
