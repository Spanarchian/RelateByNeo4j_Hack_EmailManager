"""Onboarding agent: prepares a draft from verified onboarding data.

Steps: retrieve onboarding -> select template -> populate verified fields ->
render -> store draft (DRAFT) -> submit for review (review agent ->
PENDING_REVIEW). It has no access to approval or delivery; a human must
approve and send.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from email_service.agents.base import (
    ONBOARDING_AGENT,
    AgentIdentity,
    ReviewContext,
    build_review_context,
)
from email_service.agents.review_agent import ReviewAgent
from email_service.approvals.security import compute_content_hash
from email_service.exceptions import NotFoundError, ValidationFailedError
from email_service.graph.repositories import GraphRepository
from email_service.graph.schema import new_id
from email_service.models.email import DraftStatus, EmailContent, EmailDraft
from email_service.models.onboarding import Onboarding, TemplateKind
from email_service.onboarding.templates import TemplateRenderer
from email_service.onboarding.workflow import build_template_context, select_template_id


class OnboardingAgent:
    def __init__(
        self,
        repository: GraphRepository,
        renderer: TemplateRenderer,
        reviewer: ReviewAgent,
        *,
        sender: str | None,
        allowed_senders: tuple[str, ...],
        max_recipients: int,
        identity: AgentIdentity = ONBOARDING_AGENT,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.identity = identity
        self._repo = repository
        self._renderer = renderer
        self._reviewer = reviewer
        self._sender = sender
        self._allowed_senders = allowed_senders
        self._max_recipients = max_recipients
        self._now = clock

    def review_context(self, onboarding: Onboarding) -> ReviewContext:
        return build_review_context(
            onboarding,
            today=self._now().date(),
            max_recipients=self._max_recipients,
            allowed_senders=self._allowed_senders,
        )

    async def prepare_draft(
        self, onboarding_id: str, kind: TemplateKind, requested_by: str
    ) -> EmailDraft:
        if not self._sender:
            raise ValidationFailedError(
                "EMAIL_DEFAULT_SENDER must be configured before drafts can be prepared"
            )
        onboarding = await self._repo.get_onboarding(onboarding_id)
        if onboarding is None:
            raise NotFoundError("onboarding not found", details={"onboarding_id": onboarding_id})

        template_id = select_template_id(onboarding.type, kind)
        template = await self._repo.get_template(template_id)
        if template is None:
            raise NotFoundError("email template not seeded", details={"template_id": template_id})

        rendered = self._renderer.render(template, build_template_context(onboarding))
        content = EmailContent(
            sender=self._sender,
            recipients=[onboarding.person_email],
            subject=rendered.subject,
            body=rendered.text,
            html_body=rendered.html,
        )
        now = self._now()
        draft = EmailDraft(
            id=new_id("draft"),
            status=DraftStatus.DRAFT,
            content=content,
            content_hash=compute_content_hash(content),
            onboarding_id=onboarding.id,
            template_id=template.id,
            prepared_by=self.identity.id,
            requested_by=requested_by,
            created_at=now,
            updated_at=now,
        )
        await self._repo.create_onboarding_draft(draft, self.identity.id)
        report = self._reviewer.review(content, self.review_context(onboarding))
        return await self._repo.submit_for_review(draft.id, report)
