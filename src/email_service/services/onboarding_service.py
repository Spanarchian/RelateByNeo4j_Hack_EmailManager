"""Volunteer and employee onboarding."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from email_service.agents.onboarding_agent import OnboardingAgent
from email_service.exceptions import NotFoundError
from email_service.graph import schema
from email_service.graph.repositories import GraphRepository
from email_service.models.email import EmailDraft
from email_service.models.onboarding import (
    EmployeeOnboardingRequest,
    HistoryEvent,
    Onboarding,
    OnboardingCreated,
    OnboardingHistory,
    OnboardingStatus,
    OnboardingType,
    TemplateKind,
    VolunteerOnboardingRequest,
)
from email_service.security.authentication import Principal

DEFAULT_VOLUNTEER_TEAM = "Volunteers"


def build_onboarding(
    request: VolunteerOnboardingRequest | EmployeeOnboardingRequest,
    created_by: str,
    now: datetime,
) -> Onboarding:
    org_id = schema.organisation_id(request.organisation)
    if isinstance(request, VolunteerOnboardingRequest):
        team_name = request.team or DEFAULT_VOLUNTEER_TEAM
        role_title, contact = request.volunteer_role, request.coordinator
        details = {"induction_details": request.induction_details}
    else:
        team_name = request.department
        role_title, contact = request.job_title, request.manager
        details = {
            "department": request.department,
            "first_day_arrangements": request.first_day_arrangements,
        }
    return Onboarding(
        id=schema.onboarding_id(request.type.value, request.email, org_id, request.start_date),
        type=OnboardingType(request.type),
        status=OnboardingStatus.CREATED,
        person_id=schema.person_id(request.email),
        person_name=request.name,
        person_email=request.email,
        organisation_id=org_id,
        organisation_name=request.organisation,
        team_id=schema.team_id(org_id, team_name),
        team_name=team_name,
        role_title=role_title,
        contact_name=contact,
        start_date=request.start_date,
        details=details,
        created_by=created_by,
        created_at=now,
    )


class OnboardingService:
    def __init__(
        self,
        repository: GraphRepository,
        agent: OnboardingAgent,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repo = repository
        self._agent = agent
        self._now = clock

    async def create(
        self,
        request: VolunteerOnboardingRequest | EmployeeOnboardingRequest,
        principal: Principal,
    ) -> OnboardingCreated:
        onboarding, created = await self._repo.create_onboarding(
            build_onboarding(request, principal.subject, self._now())
        )
        return OnboardingCreated(onboarding=onboarding, created=created)

    async def get(self, onboarding_id: str) -> Onboarding:
        onboarding = await self._repo.get_onboarding(onboarding_id)
        if onboarding is None:
            raise NotFoundError("onboarding not found", details={"onboarding_id": onboarding_id})
        return onboarding

    async def list(self, onboarding_type: OnboardingType | None) -> list[Onboarding]:
        return await self._repo.list_onboardings(onboarding_type)

    async def prepare_draft(
        self, onboarding_id: str, kind: TemplateKind, principal: Principal
    ) -> EmailDraft:
        return await self._agent.prepare_draft(onboarding_id, kind, principal.subject)

    async def history(self, onboarding_id: str) -> OnboardingHistory:
        onboarding = await self.get(onboarding_id)
        events = [
            HistoryEvent(
                at=onboarding.created_at,
                event="onboarding_created",
                actor=onboarding.created_by,
                detail=f"{onboarding.type.value} onboarding starting {onboarding.start_date}",
            )
        ]
        for detail in await self._repo.onboarding_drafts(onboarding_id):
            draft = detail.draft
            events.append(
                HistoryEvent(
                    at=draft.created_at,
                    event="draft_prepared",
                    actor=draft.prepared_by,
                    draft_id=draft.id,
                    detail=f"template {draft.template_id}, requested by {draft.requested_by}",
                )
            )
            if draft.review:
                errors = len(draft.review.blocking)
                events.append(
                    HistoryEvent(
                        at=draft.review.reviewed_at,
                        event="draft_reviewed",
                        actor=draft.review.agent_id,
                        draft_id=draft.id,
                        detail=f"{len(draft.review.findings)} findings, {errors} blocking",
                    )
                )
            for approval in detail.approvals:
                events.append(
                    HistoryEvent(
                        at=approval.decided_at,
                        event=f"draft_{approval.decision.value.lower()}",
                        actor=approval.reviewer,
                        draft_id=draft.id,
                        status=approval.decision.value,
                        detail=approval.comment,
                    )
                )
                if approval.invalidated_at and approval.decision.value == "APPROVED":
                    events.append(
                        HistoryEvent(
                            at=approval.invalidated_at,
                            event="approval_invalidated",
                            draft_id=draft.id,
                            detail=approval.invalidated_reason,
                        )
                    )
            for delivery in detail.deliveries:
                events.append(
                    HistoryEvent(
                        at=delivery.created_at,
                        event="delivery_started",
                        actor=delivery.initiated_by,
                        draft_id=draft.id,
                        detail=f"delivery {delivery.sequence}",
                    )
                )
                for attempt in delivery.attempts:
                    events.append(
                        HistoryEvent(
                            at=attempt.finished_at,
                            event="delivery_attempt",
                            draft_id=draft.id,
                            status=attempt.outcome.value,
                            detail=f"{attempt.provider}: {attempt.error_code or 'accepted'}",
                        )
                    )
                if delivery.completed_at:
                    events.append(
                        HistoryEvent(
                            at=delivery.completed_at,
                            event="delivery_completed",
                            actor=delivery.reconciled_by,
                            draft_id=draft.id,
                            status=delivery.status.value,
                            detail=delivery.provider,
                        )
                    )
            events.append(
                HistoryEvent(
                    at=draft.updated_at,
                    event="draft_status",
                    draft_id=draft.id,
                    status=draft.status.value,
                )
            )
        events.sort(key=lambda e: e.at)
        return OnboardingHistory(onboarding=onboarding, events=events)
