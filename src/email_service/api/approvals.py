"""Human approval endpoints (reviewers only)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from email_service.api.errors import error_responses
from email_service.dependencies import ApprovalServiceDep, require
from email_service.models.approval import ApproveRequest, RejectRequest
from email_service.models.email import EmailDraft
from email_service.security.authentication import Principal
from email_service.security.permissions import Permission

router = APIRouter(prefix="/api/v1/emails", tags=["approvals"])
Reviewer = Annotated[Principal, Depends(require(Permission.APPROVE))]


@router.post(
    "/{draft_id}/approve",
    summary="Approve the exact content you reviewed",
    description=(
        "Send the `content_hash` shown by GET /api/v1/emails/{id}. If the draft changed since, "
        "the approval is refused. Drafts with blocking review findings cannot be approved."
    ),
    responses=error_responses(401, 403, 404, 409, 422),
)
async def approve(
    draft_id: str, body: ApproveRequest, service: ApprovalServiceDep, reviewer: Reviewer
) -> EmailDraft:
    return await service.approve(draft_id, reviewer, body.content_hash, body.comment)


@router.post("/{draft_id}/reject", responses=error_responses(401, 403, 404, 409, 422))
async def reject(
    draft_id: str, body: RejectRequest, service: ApprovalServiceDep, reviewer: Reviewer
) -> EmailDraft:
    return await service.reject(draft_id, reviewer, body.reason)
