"""Deterministic review agent.

Produces structured findings. ``ERROR`` findings block approval; ``WARNING``
and ``INFO`` findings are shown to the human reviewer. The agent never changes
draft status beyond attaching its report and never approves.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

from email_service.agents.base import REVIEW_AGENT, AgentIdentity, ReviewContext, ReviewRule
from email_service.approvals.security import compute_content_hash
from email_service.models.approval import ReviewFinding, ReviewReport, Severity
from email_service.models.email import EmailContent

E, W, INFO = Severity.ERROR, Severity.WARNING, Severity.INFO

PLACEHOLDER = re.compile(
    r"\{\{.*?\}\}|\{%.*?%\}|\[\[.*?\]\]|<<[^<>]{1,40}>>|\{[A-Za-z_]+\}"
    r"|\[(?:insert|name|date|role|organisation|organization)[^\]]*\]"
    r"|\b(?:TODO|TBC|TBD|FIXME|XXX|LOREM IPSUM)\b",
    re.IGNORECASE | re.DOTALL,
)
UNSAFE_HTML = re.compile(r"<\s*(script|iframe|object|embed|form)\b|javascript:|\son\w+\s*=", re.I)
INSECURE_LINK = re.compile(r"http://[^\s\"'<>]+", re.I)

# Sensitive data that should never be in an onboarding email.
SENSITIVE_ERRORS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "sensitive.uk_ni_number",
        re.compile(r"\b[A-CEGHJ-PR-TW-Z][A-CEGHJ-NPR-TW-Z]\s?\d{2}\s?\d{2}\s?\d{2}\s?[A-D]\b"),
        "looks like a UK National Insurance number",
    ),
    (
        "sensitive.us_ssn",
        re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
        "looks like a US Social Security number",
    ),
    (
        "sensitive.iban",
        re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}(?:\s?[A-Z0-9]{1,3})?\b"),
        "looks like a bank account number (IBAN)",
    ),
    (
        "sensitive.credential",
        re.compile(r"\b(?:password|passcode|passwd|pin|api[_ ]?key|secret)\s*[:=]\s*\S+", re.I),
        "appears to contain a password or credential",
    ),
)
# Topics that may be legitimate but deserve a human's attention.
SENSITIVE_WARNINGS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "sensitive.personal_detail",
        re.compile(
            r"\b(?:date of birth|DOB|passport number|driving licen[cs]e number|sort code"
            r"|account number|salary|medical|diagnos\w*|disabilit\w*|criminal record"
            r"|home address)\b",
            re.I,
        ),
        "mentions personal or sensitive information; confirm it is necessary",
    ),
)
CARD_CANDIDATE = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def _luhn(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _text_of(content: EmailContent) -> str:
    return "\n".join([content.subject, content.body, content.html_body or ""])


def check_sender(content: EmailContent, ctx: ReviewContext) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    sender = content.sender.lower()
    if ctx.allowed_senders:
        if sender not in {s.lower() for s in ctx.allowed_senders}:
            findings.append(
                ReviewFinding(
                    code="sender.not_allowed",
                    severity=E,
                    message="sender is not in EMAIL_ALLOWED_SENDERS",
                    field="sender",
                )
            )
    else:
        findings.append(
            ReviewFinding(
                code="sender.no_allow_list",
                severity=W,
                message="no sender allow-list is configured; verify the sender manually",
                field="sender",
            )
        )
    if sender in {r.lower() for r in content.recipients}:
        findings.append(
            ReviewFinding(
                code="sender.is_recipient",
                severity=W,
                message="the sender is also a recipient",
                field="recipients",
            )
        )
    return findings


def check_recipients(content: EmailContent, ctx: ReviewContext) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    if len(content.recipients) > ctx.max_recipients:
        findings.append(
            ReviewFinding(
                code="recipients.too_many",
                severity=E,
                message=f"{len(content.recipients)} recipients exceeds the limit of {ctx.max_recipients}",
                field="recipients",
            )
        )
    if ctx.expected_recipient:
        lowered = [r.lower() for r in content.recipients]
        if ctx.expected_recipient.lower() not in lowered:
            findings.append(
                ReviewFinding(
                    code="recipients.identity_mismatch",
                    severity=E,
                    message="the onboarding person's email address is not a recipient",
                    field="recipients",
                )
            )
        if len(lowered) > 1:
            findings.append(
                ReviewFinding(
                    code="recipients.additional",
                    severity=W,
                    message="the email has recipients besides the onboarding person",
                    field="recipients",
                )
            )
    return findings


def check_identity(content: EmailContent, ctx: ReviewContext) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    text = _text_of(content)
    if ctx.person_name:
        first = ctx.person_name.split()[0]
        if first.lower() not in content.body.lower():
            findings.append(
                ReviewFinding(
                    code="identity.name_missing",
                    severity=W,
                    message="the recipient's name does not appear in the body",
                    field="body",
                )
            )
    if ctx.organisation_name and ctx.organisation_name.lower() not in text.lower():
        findings.append(
            ReviewFinding(
                code="identity.organisation_missing",
                severity=E,
                message="the organisation name does not appear in the email",
                field="body",
            )
        )
    if ctx.role_title and ctx.role_title.lower() not in content.body.lower():
        findings.append(
            ReviewFinding(
                code="identity.role_missing",
                severity=E,
                message="the role does not appear in the body",
                field="body",
            )
        )
    return findings


def check_dates(content: EmailContent, ctx: ReviewContext) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    if ctx.start_date is None:
        return findings
    if ctx.start_date < ctx.today:
        findings.append(
            ReviewFinding(
                code="dates.start_in_past",
                severity=E,
                message="the start date is in the past",
                field="start_date",
            )
        )
    elif ctx.start_date > ctx.today + timedelta(days=366):
        findings.append(
            ReviewFinding(
                code="dates.start_far_future",
                severity=W,
                message="the start date is more than a year away",
                field="start_date",
            )
        )
    if ctx.formatted_start_date and ctx.formatted_start_date not in content.body:
        findings.append(
            ReviewFinding(
                code="dates.start_missing",
                severity=E,
                message="the verified start date does not appear in the body",
                field="body",
            )
        )
    return findings


def check_placeholders(content: EmailContent, ctx: ReviewContext) -> list[ReviewFinding]:
    findings = []
    for field_name, value in (
        ("subject", content.subject),
        ("body", content.body),
        ("html_body", content.html_body or ""),
    ):
        match = PLACEHOLDER.search(value)
        if match:
            findings.append(
                ReviewFinding(
                    code="content.placeholder",
                    severity=E,
                    message=f"unresolved placeholder or marker: {match.group(0)[:40]!r}",
                    field=field_name,
                )
            )
    return findings


def check_content(content: EmailContent, ctx: ReviewContext) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    if content.html_body and UNSAFE_HTML.search(content.html_body):
        findings.append(
            ReviewFinding(
                code="content.unsafe_html",
                severity=E,
                message="HTML contains scripts, forms, embedded objects or event handlers",
                field="html_body",
            )
        )
    if len(content.body) < 40:
        findings.append(
            ReviewFinding(
                code="content.too_short",
                severity=W,
                message="the plain-text body is very short",
                field="body",
            )
        )
    if len(content.body) > 20_000:
        findings.append(
            ReviewFinding(
                code="content.too_long",
                severity=W,
                message="the plain-text body is unusually long",
                field="body",
            )
        )
    if INSECURE_LINK.search(_text_of(content)):
        findings.append(
            ReviewFinding(
                code="content.insecure_link",
                severity=W,
                message="the email contains a non-HTTPS link",
                field="body",
            )
        )
    if content.html_body is None:
        findings.append(
            ReviewFinding(
                code="content.plain_text_only",
                severity=INFO,
                message="no HTML version; the plain-text body will be sent",
                field="html_body",
            )
        )
    return findings


def check_sensitive(content: EmailContent, ctx: ReviewContext) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    text = _text_of(content)
    for code, pattern, message in SENSITIVE_ERRORS:
        if pattern.search(text):
            findings.append(ReviewFinding(code=code, severity=E, message=message))
    for match in CARD_CANDIDATE.finditer(text):
        digits = re.sub(r"\D", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            findings.append(
                ReviewFinding(
                    code="sensitive.payment_card",
                    severity=E,
                    message="looks like a payment card number",
                )
            )
            break
    for code, pattern, message in SENSITIVE_WARNINGS:
        hit = pattern.search(text)
        if hit:
            findings.append(
                ReviewFinding(code=code, severity=W, message=f"{message} ({hit.group(0)!r})")
            )
    return findings


DEFAULT_RULES: tuple[ReviewRule, ...] = (
    check_sender,
    check_recipients,
    check_identity,
    check_dates,
    check_placeholders,
    check_content,
    check_sensitive,
)


class ReviewAgent:
    def __init__(
        self,
        rules: Sequence[ReviewRule] = DEFAULT_RULES,
        *,
        identity: AgentIdentity = REVIEW_AGENT,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.identity = identity
        self._rules = tuple(rules)
        self._now = clock

    def review(self, content: EmailContent, context: ReviewContext) -> ReviewReport:
        findings: list[ReviewFinding] = []
        for rule in self._rules:
            findings.extend(rule(content, context))
        return ReviewReport(
            agent_id=self.identity.id,
            content_hash=compute_content_hash(content),
            reviewed_at=self._now(),
            findings=findings,
        )
