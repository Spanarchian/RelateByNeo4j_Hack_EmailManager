"""Email content, draft and delivery models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from email_service.models.approval import Approval, ReviewReport

#: Hard upper bound; the configurable limit (EMAIL_MAX_RECIPIENTS) is enforced by services.
ABSOLUTE_MAX_RECIPIENTS = 100
MAX_SUBJECT_LENGTH = 200
MAX_BODY_LENGTH = 100_000


class DraftStatus(StrEnum):
    DRAFT = "DRAFT"
    PENDING_REVIEW = "PENDING_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SENDING = "SENDING"
    SENT = "SENT"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    CANCELLED = "CANCELLED"


class DeliveryStatus(StrEnum):
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class AttemptOutcome(StrEnum):
    """Result of one provider call.

    ``REJECTED`` is a *definitive* failure: the provider did not accept the
    message. ``UNKNOWN`` means the message may or may not have been accepted
    (for example a read timeout) and must never trigger an automatic resend.
    """

    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


def _no_header_injection(value: str) -> str:
    if "\r" in value or "\n" in value:
        raise ValueError("must not contain line breaks")
    return value


class EmailContent(BaseModel):
    """The exact content a reviewer approves. Immutable."""

    model_config = ConfigDict(frozen=True)

    sender: EmailStr
    recipients: list[EmailStr] = Field(min_length=1, max_length=ABSOLUTE_MAX_RECIPIENTS)
    subject: str = Field(min_length=1, max_length=MAX_SUBJECT_LENGTH)
    body: str = Field(min_length=1, max_length=MAX_BODY_LENGTH)
    html_body: str | None = Field(default=None, max_length=MAX_BODY_LENGTH)

    @field_validator("subject")
    @classmethod
    def _subject(cls, value: str) -> str:
        value = _no_header_injection(value.strip())
        if not value:
            raise ValueError("subject must not be blank")
        return value

    @field_validator("body")
    @classmethod
    def _body(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a plain-text body is always required")
        return value

    @field_validator("html_body")
    @classmethod
    def _html(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _unique_recipients(self) -> Self:
        lowered = [address.lower() for address in self.recipients]
        if len(lowered) != len(set(lowered)):
            raise ValueError("recipients must be unique")
        return self


class EmailRequest(BaseModel):
    """Body of ``POST /api/v1/emails``."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "title": "Welcome to our organisation",
                "body": "Welcome to our team.",
                "html_body": "<h1>Welcome</h1>",
                "sender": "sender@gmail.com",
                "recipients": ["person1@example.com", "person2@example.com"],
            }
        }
    )

    title: str = Field(min_length=1, max_length=MAX_SUBJECT_LENGTH)
    body: str = Field(min_length=1, max_length=MAX_BODY_LENGTH)
    html_body: str | None = Field(default=None, max_length=MAX_BODY_LENGTH)
    sender: EmailStr
    recipients: list[EmailStr] = Field(min_length=1, max_length=ABSOLUTE_MAX_RECIPIENTS)

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        return _no_header_injection(value)

    def to_content(self) -> EmailContent:
        return EmailContent(
            sender=self.sender,
            recipients=self.recipients,
            subject=self.title,
            body=self.body,
            html_body=self.html_body,
        )


class DraftUpdate(BaseModel):
    """Body of ``PATCH /api/v1/emails/{id}``. Any change invalidates prior approval."""

    title: str | None = Field(default=None, min_length=1, max_length=MAX_SUBJECT_LENGTH)
    body: str | None = Field(default=None, min_length=1, max_length=MAX_BODY_LENGTH)
    html_body: str | None = Field(default=None, max_length=MAX_BODY_LENGTH)
    sender: EmailStr | None = None
    recipients: list[EmailStr] | None = Field(
        default=None, min_length=1, max_length=ABSOLUTE_MAX_RECIPIENTS
    )

    @field_validator("title")
    @classmethod
    def _title(cls, value: str | None) -> str | None:
        return None if value is None else _no_header_injection(value)

    def apply(self, content: EmailContent) -> EmailContent:
        fields = self.model_fields_set
        return EmailContent(
            sender=self.sender if "sender" in fields and self.sender else content.sender,
            recipients=(
                self.recipients
                if "recipients" in fields and self.recipients
                else content.recipients
            ),
            subject=self.title if "title" in fields and self.title else content.subject,
            body=self.body if "body" in fields and self.body else content.body,
            html_body=self.html_body if "html_body" in fields else content.html_body,
        )


class EmailDraft(BaseModel):
    id: str
    status: DraftStatus
    content: EmailContent
    content_hash: str
    onboarding_id: str | None = None
    template_id: str | None = None
    prepared_by: str
    requested_by: str
    created_at: datetime
    updated_at: datetime
    version: int = 1
    delivery_count: int = 0
    review: ReviewReport | None = None
    idempotency_key: str | None = None


class DeliveryAttempt(BaseModel):
    provider: str
    attempt: int
    outcome: AttemptOutcome
    retryable: bool = False
    provider_message_id: str | None = None
    provider_status: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    started_at: datetime
    finished_at: datetime


class EmailDelivery(BaseModel):
    id: str
    draft_id: str
    idempotency_key: str
    sequence: int
    status: DeliveryStatus
    content_hash: str
    initiated_by: str
    provider: str | None = None
    provider_message_id: str | None = None
    attempts: list[DeliveryAttempt] = Field(default_factory=list)
    created_at: datetime
    completed_at: datetime | None = None
    reconciled_by: str | None = None
    reconciliation_note: str | None = None


class DraftDetail(BaseModel):
    draft: EmailDraft
    approvals: list[Approval]
    deliveries: list[EmailDelivery]


class ReconcileRequest(BaseModel):
    """A human's confirmation of the true outcome of an UNKNOWN delivery."""

    outcome: DeliveryStatus = Field(description="SENT or FAILED")
    note: str = Field(min_length=3, max_length=1000)

    @field_validator("outcome")
    @classmethod
    def _definitive(cls, value: DeliveryStatus) -> DeliveryStatus:
        if value not in {DeliveryStatus.SENT, DeliveryStatus.FAILED}:
            raise ValueError("outcome must be SENT or FAILED")
        return value


class SendResponse(BaseModel):
    draft_id: str
    draft_status: DraftStatus
    delivery: EmailDelivery
