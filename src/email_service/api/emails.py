"""Email draft endpoints. Submitting an email creates a draft; it is never sent directly."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Response, status

from email_service.api.errors import error_responses
from email_service.dependencies import EmailServiceDep, require
from email_service.models.email import (
    DeliveryStatus,
    DraftDetail,
    DraftUpdate,
    EmailDelivery,
    EmailDraft,
    EmailRequest,
    ReconcileRequest,
    SendResponse,
)
from email_service.security.authentication import Principal
from email_service.security.permissions import Permission

router = APIRouter(prefix="/api/v1/emails", tags=["emails"])

Reader = Annotated[Principal, Depends(require(Permission.READ))]
IdempotencyKey = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        max_length=128,
        pattern=r"^[A-Za-z0-9_.:-]+$",
        description="Repeat-safe submissions: the same key returns the same draft.",
    ),
]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Submit an email for review",
    description=(
        "Validates the email and stores it as a draft. The review agent checks it and it waits "
        "in PENDING_REVIEW until a human reviewer approves it and then sends it."
    ),
    responses=error_responses(401, 403, 409, 422),
)
async def submit_email(
    request: EmailRequest,
    service: EmailServiceDep,
    principal: Annotated[Principal, Depends(require(Permission.SUBMIT_EMAIL))],
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> EmailDraft:
    draft, created = await service.submit(request, principal, idempotency_key)
    if not created:
        response.status_code = status.HTTP_200_OK
    return draft


@router.get("/pending-review", summary="Drafts waiting for a human decision")
async def pending_review(service: EmailServiceDep, _: Reader) -> list[EmailDraft]:
    return await service.pending_review()


@router.get("/unsent", summary="Onboarding drafts not yet sent")
async def unsent(service: EmailServiceDep, _: Reader) -> list[EmailDraft]:
    return await service.unsent_onboarding()


@router.get("/failed-deliveries", summary="Deliveries that FAILED or are UNKNOWN")
async def failed_deliveries(service: EmailServiceDep, _: Reader) -> list[EmailDelivery]:
    return await service.failed_deliveries()


@router.get("/{draft_id}", responses=error_responses(401, 403, 404))
async def get_email(draft_id: str, service: EmailServiceDep, _: Reader) -> DraftDetail:
    return await service.get(draft_id)


@router.patch(
    "/{draft_id}",
    summary="Edit a draft (invalidates any approval)",
    responses=error_responses(401, 403, 404, 409, 422),
)
async def edit_email(
    draft_id: str,
    update: DraftUpdate,
    service: EmailServiceDep,
    principal: Annotated[Principal, Depends(require(Permission.EDIT_DRAFT))],
    if_match: Annotated[
        int | None, Header(alias="If-Match", description="Expected draft version")
    ] = None,
) -> EmailDraft:
    return await service.edit(draft_id, update, principal, if_match)


@router.post("/{draft_id}/cancel", responses=error_responses(401, 403, 404, 409))
async def cancel_email(
    draft_id: str,
    service: EmailServiceDep,
    principal: Annotated[Principal, Depends(require(Permission.CANCEL_DRAFT))],
) -> EmailDraft:
    return await service.cancel(draft_id, principal)


@router.post(
    "/{draft_id}/send",
    summary="Send an approved draft",
    description=(
        "Human reviewers only. Re-verifies the approval hash, creates a durable delivery record "
        "and submits through the provider chain. 200 = SENT, 202 = UNKNOWN (outcome uncertain; "
        "reconcile manually), 502 = FAILED (no provider accepted it)."
    ),
    responses={
        202: {"model": SendResponse, "description": "Outcome unknown"},
        502: {"model": SendResponse, "description": "Delivery failed"},
        **error_responses(401, 403, 404, 409),
    },
)
async def send_email(
    draft_id: str,
    service: EmailServiceDep,
    principal: Annotated[Principal, Depends(require(Permission.SEND))],
    response: Response,
) -> SendResponse:
    result = await service.send(draft_id, principal)
    response.status_code = {
        DeliveryStatus.SENT: status.HTTP_200_OK,
        DeliveryStatus.UNKNOWN: status.HTTP_202_ACCEPTED,
    }.get(result.delivery.status, status.HTTP_502_BAD_GATEWAY)
    return result


@router.post(
    "/{draft_id}/reconcile",
    summary="Resolve an UNKNOWN delivery after checking the provider",
    responses=error_responses(401, 403, 404, 409, 422),
)
async def reconcile_email(
    draft_id: str,
    body: ReconcileRequest,
    service: EmailServiceDep,
    principal: Annotated[Principal, Depends(require(Permission.RECONCILE))],
) -> SendResponse:
    return await service.reconcile(draft_id, body.outcome, body.note, principal)
