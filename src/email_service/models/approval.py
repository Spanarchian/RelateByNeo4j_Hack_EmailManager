"""Review findings and human approval records."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"  # blocks approval


class ReviewFinding(BaseModel):
    code: str
    severity: Severity
    message: str
    field: str | None = None


class ReviewReport(BaseModel):
    agent_id: str
    content_hash: str
    reviewed_at: datetime
    findings: list[ReviewFinding] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not any(f.severity is Severity.ERROR for f in self.findings)

    @property
    def blocking(self) -> list[ReviewFinding]:
        return [f for f in self.findings if f.severity is Severity.ERROR]


class ApprovalDecision(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class Approval(BaseModel):
    id: str
    draft_id: str
    decision: ApprovalDecision
    reviewer: str
    content_hash: str
    decided_at: datetime
    comment: str | None = None
    valid: bool = True
    invalidated_at: datetime | None = None
    invalidated_reason: str | None = None


class ApproveRequest(BaseModel):
    content_hash: str = Field(
        pattern=r"^[0-9a-f]{64}$",
        description="Hash of the content the reviewer actually reviewed (from GET /emails/{id}).",
    )
    comment: str | None = Field(default=None, max_length=1000)


class RejectRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)
