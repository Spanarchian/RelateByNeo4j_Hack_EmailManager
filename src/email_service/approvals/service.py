"""Human approval workflow."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from email_service.approvals.security import verify_reviewer
from email_service.exceptions import NotFoundError
from email_service.graph.repositories import GraphRepository
from email_service.graph.schema import new_id
from email_service.models.approval import Approval, ApprovalDecision
from email_service.models.email import EmailDraft
from email_service.security.authentication import Principal


class ApprovalService:
    def __init__(
        self,
        repository: GraphRepository,
        *,
        four_eyes: bool,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repo = repository
        self._four_eyes = four_eyes
        self._now = clock

    async def _draft(self, draft_id: str) -> EmailDraft:
        draft = await self._repo.get_draft(draft_id)
        if draft is None:
            raise NotFoundError("email draft not found", details={"draft_id": draft_id})
        return draft

    async def approve(
        self, draft_id: str, reviewer: Principal, reviewed_hash: str, comment: str | None
    ) -> EmailDraft:
        """Record approval of exactly the content the reviewer saw (``reviewed_hash``)."""
        draft = await self._draft(draft_id)
        verify_reviewer(reviewer, draft, four_eyes=self._four_eyes)
        return await self._repo.record_decision(
            Approval(
                id=new_id("appr"),
                draft_id=draft_id,
                decision=ApprovalDecision.APPROVED,
                reviewer=reviewer.subject,
                content_hash=reviewed_hash,
                decided_at=self._now(),
                comment=comment,
            )
        )

    async def reject(self, draft_id: str, reviewer: Principal, reason: str) -> EmailDraft:
        draft = await self._draft(draft_id)
        verify_reviewer(reviewer, draft, four_eyes=self._four_eyes)
        return await self._repo.record_decision(
            Approval(
                id=new_id("appr"),
                draft_id=draft_id,
                decision=ApprovalDecision.REJECTED,
                reviewer=reviewer.subject,
                content_hash=draft.content_hash,
                decided_at=self._now(),
                comment=reason,
                valid=False,  # a rejection never authorises sending
            )
        )
