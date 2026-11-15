"""Volunteer and employee onboarding endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Query, Response, status

from email_service.api.errors import error_responses
from email_service.dependencies import OnboardingServiceDep, require
from email_service.models.email import EmailDraft
from email_service.models.onboarding import (
    DraftRequest,
    Onboarding,
    OnboardingCreated,
    OnboardingHistory,
    OnboardingRequest,
    OnboardingType,
)
from email_service.security.authentication import Principal
from email_service.security.permissions import Permission

router = APIRouter(prefix="/api/v1/onboarding", tags=["onboarding"])
Reader = Annotated[Principal, Depends(require(Permission.READ))]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Register a volunteer or employee onboarding (idempotent)",
    responses=error_responses(401, 403, 422),
)
async def create_onboarding(
    request: Annotated[OnboardingRequest, Body()],
    service: OnboardingServiceDep,
    principal: Annotated[Principal, Depends(require(Permission.CREATE_ONBOARDING))],
    response: Response,
) -> OnboardingCreated:
    result = await service.create(request, principal)
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return result


@router.get("", summary="List onboardings, optionally by type")
async def list_onboardings(
    service: OnboardingServiceDep,
    _: Reader,
    type: Annotated[OnboardingType | None, Query()] = None,
) -> list[Onboarding]:
    return await service.list(type)


@router.get("/{onboarding_id}", responses=error_responses(401, 403, 404))
async def get_onboarding(
    onboarding_id: str, service: OnboardingServiceDep, _: Reader
) -> Onboarding:
    return await service.get(onboarding_id)


@router.post(
    "/{onboarding_id}/draft",
    status_code=status.HTTP_201_CREATED,
    summary="Ask the onboarding agent to prepare a draft for review",
    responses=error_responses(401, 403, 404, 409, 422),
)
async def prepare_draft(
    onboarding_id: str,
    service: OnboardingServiceDep,
    principal: Annotated[Principal, Depends(require(Permission.PREPARE_DRAFT))],
    body: DraftRequest | None = None,
) -> EmailDraft:
    kind = (body or DraftRequest()).template
    return await service.prepare_draft(onboarding_id, kind, principal)


@router.get("/{onboarding_id}/history", responses=error_responses(401, 403, 404))
async def onboarding_history(
    onboarding_id: str, service: OnboardingServiceDep, _: Reader
) -> OnboardingHistory:
    return await service.history(onboarding_id)
