"""Email drafts: submission, editing, cancellation, sending and reconciliation."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from email_service.agents.base import build_review_context
from email_service.agents.review_agent import ReviewAgent
from email_service.approvals.security import compute_content_hash, verify_sender_authority
from email_service.exceptions import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationFailedError,
)
from email_service.graph.repositories import GraphRepository
from email_service.graph.schema import draft_id_for_idempotency_key, new_id
from email_service.models.approval import ReviewReport
from email_service.models.email import (
    DeliveryAttempt,
    DeliveryStatus,
    DraftDetail,
    DraftStatus,
    DraftUpdate,
    EmailContent,
    EmailDelivery,
    EmailDraft,
    EmailRequest,
    SendResponse,
)
from email_service.onboarding.workflow import EDITABLE
from email_service.providers.base import OutboundEmail
from email_service.security.authentication import Principal
from email_service.security.permissions import Permission
from email_service.services.provider_service import ProviderService

logger = logging.getLogger(__name__)


class EmailService:
    def __init__(
        self,
        repository: GraphRepository,
        reviewer: ReviewAgent,
        providers: ProviderService,
        *,
        max_recipients: int,
        allowed_senders: tuple[str, ...],
        message_id_domain: str = "onboarding.invalid",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repo = repository
        self._reviewer = reviewer
        self._providers = providers
        self._max_recipients = max_recipients
        self._allowed_senders = allowed_senders
        self._message_id_domain = message_id_domain
        self._now = clock

    # --- queries -----------------------------------------------------------------

    async def get(self, draft_id: str) -> DraftDetail:
        detail = await self._repo.get_draft_detail(draft_id)
        if detail is None:
            raise NotFoundError("email draft not found", details={"draft_id": draft_id})
        return detail

    async def pending_review(self) -> list[EmailDraft]:
        return await self._repo.list_drafts([DraftStatus.PENDING_REVIEW])

    async def unsent_onboarding(self) -> list[EmailDraft]:
        return await self._repo.list_unsent_onboarding_drafts()

    async def failed_deliveries(self) -> list[EmailDelivery]:
        return await self._repo.list_failed_deliveries()

    # --- commands ----------------------------------------------------------------

    def _check_limits(self, content: EmailContent) -> None:
        if len(content.recipients) > self._max_recipients:
            raise ValidationFailedError(
                f"at most {self._max_recipients} recipients are allowed",
                details={"recipients": len(content.recipients)},
            )

    async def _review(self, content: EmailContent, onboarding_id: str | None) -> ReviewReport:
        onboarding = await self._repo.get_onboarding(onboarding_id) if onboarding_id else None
        context = build_review_context(
            onboarding,
            today=self._now().date(),
            max_recipients=self._max_recipients,
            allowed_senders=self._allowed_senders,
        )
        return self._reviewer.review(content, context)

    async def submit(
        self, request: EmailRequest, principal: Principal, idempotency_key: str | None
    ) -> tuple[EmailDraft, bool]:
        """Create a draft for review. Nothing is sent until a human approves and sends it."""
        content = request.to_content()
        self._check_limits(content)
        content_hash = compute_content_hash(content)
        draft_id = (
            draft_id_for_idempotency_key(principal.subject, idempotency_key)
            if idempotency_key
            else new_id("draft")
        )
        now = self._now()
        draft = EmailDraft(
            id=draft_id,
            status=DraftStatus.DRAFT,
            content=content,
            content_hash=content_hash,
            prepared_by=principal.subject,
            requested_by=principal.subject,
            created_at=now,
            updated_at=now,
            idempotency_key=(f"{principal.subject}:{idempotency_key}" if idempotency_key else None),
        )
        stored, created = await self._repo.create_standalone_draft(draft)
        if not created:
            if stored.content_hash != content_hash and stored.version == 1:
                raise ConflictError(
                    "Idempotency-Key was already used with different content",
                    details={"draft_id": stored.id},
                )
            return stored, False
        report = self._reviewer.review(
            content,
            build_review_context(
                None,
                today=now.date(),
                max_recipients=self._max_recipients,
                allowed_senders=self._allowed_senders,
            ),
        )
        return await self._repo.submit_for_review(draft_id, report), True

    async def edit(
        self, draft_id: str, update: DraftUpdate, principal: Principal, expected_version: int | None
    ) -> EmailDraft:
        """Any edit invalidates all approvals and sends the draft back for review."""
        current = await self._repo.get_draft(draft_id)
        if current is None:
            raise NotFoundError("email draft not found", details={"draft_id": draft_id})
        content = update.apply(current.content)
        self._check_limits(content)
        new_hash = compute_content_hash(content)
        if new_hash == current.content_hash:
            return current
        report = await self._review(content, current.onboarding_id)
        updated = await self._repo.update_content(
            draft_id,
            content=content,
            content_hash=new_hash,
            review=report,
            expected_version=expected_version,
            allowed_from=EDITABLE,
        )
        logger.info("draft edited; approvals invalidated", extra={"draft_id": draft_id})
        return updated

    async def cancel(self, draft_id: str, principal: Principal) -> EmailDraft:
        return await self._repo.cancel(draft_id)

    async def send(self, draft_id: str, principal: Principal) -> SendResponse:
        """Verify, claim, deliver, record. See the SAD for the full safety argument."""
        verify_sender_authority(principal)
        draft, delivery = await self._repo.claim_for_sending(draft_id, principal.subject)
        email = OutboundEmail(
            delivery_id=delivery.id,
            idempotency_key=delivery.idempotency_key,
            message_id=f"<{delivery.id}@{self._message_id_domain}>",
            content=draft.content,
        )

        async def record(attempt: DeliveryAttempt) -> None:
            await self._repo.record_attempt(delivery.id, attempt)

        try:
            outcome = await self._providers.deliver(email, record)
            status, provider, message_id = (
                outcome.status,
                outcome.provider,
                outcome.provider_message_id,
            )
        except Exception:
            # We cannot tell whether a provider accepted the message.
            logger.exception("delivery interrupted", extra={"delivery_id": delivery.id})
            status, provider, message_id = DeliveryStatus.UNKNOWN, None, None
        final_draft, final_delivery = await self._repo.complete_delivery(
            delivery.id, status=status, provider=provider, provider_message_id=message_id
        )
        detail = await self._repo.get_draft_detail(draft_id)
        if detail:
            final_delivery = next(d for d in detail.deliveries if d.id == delivery.id)
        return SendResponse(
            draft_id=draft_id, draft_status=final_draft.status, delivery=final_delivery
        )

    async def reconcile(
        self, draft_id: str, outcome: DeliveryStatus, note: str, principal: Principal
    ) -> SendResponse:
        if not principal.has(Permission.RECONCILE):
            raise PermissionDeniedError("only human reviewers may reconcile deliveries")
        draft, delivery = await self._repo.reconcile(draft_id, outcome, principal.subject, note)
        return SendResponse(draft_id=draft_id, draft_status=draft.status, delivery=delivery)
