"""Approval integrity: content hashing and reviewer verification."""

from __future__ import annotations

import hashlib
import json

from email_service.exceptions import ApprovalIntegrityError, PermissionDeniedError
from email_service.models.approval import Approval, ApprovalDecision
from email_service.models.email import EmailContent, EmailDraft
from email_service.security.authentication import Principal
from email_service.security.permissions import Permission, PrincipalKind

HASH_VERSION = "v1"


def compute_content_hash(content: EmailContent) -> str:
    """SHA-256 over sender, recipients, subject, plain-text body and HTML body.

    The canonical form is versioned JSON with sorted keys, so the hash is
    stable across processes and any change to any covered field changes it.
    Recipient order is preserved deliberately: it is part of what was approved.
    """
    canonical = json.dumps(
        {
            "version": HASH_VERSION,
            "sender": content.sender.lower(),
            "recipients": [address.lower() for address in content.recipients],
            "subject": content.subject,
            "body": content.body,
            "html_body": content.html_body or "",
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def verify_reviewer(principal: Principal, draft: EmailDraft, *, four_eyes: bool) -> None:
    """Only authenticated humans with approval rights, distinct from the preparer."""
    if principal.kind is not PrincipalKind.HUMAN:
        raise PermissionDeniedError("only human reviewers may approve or reject drafts")
    if not principal.has(Permission.APPROVE):
        raise PermissionDeniedError("the reviewer role is required")
    if principal.subject == draft.prepared_by:
        raise PermissionDeniedError("a draft cannot be reviewed by the identity that prepared it")
    if four_eyes and principal.subject == draft.requested_by:
        raise PermissionDeniedError(
            "four-eyes policy: the requester of a draft cannot approve it",
            details={"policy": "APPROVAL_REQUIRE_FOUR_EYES"},
        )


def verify_sender_authority(principal: Principal) -> None:
    if principal.kind is not PrincipalKind.HUMAN or not principal.has(Permission.SEND):
        raise PermissionDeniedError("only authorised human reviewers may send email")


def verify_approval_integrity(draft: EmailDraft, approval: Approval | None) -> str:
    """Recompute the hash and compare it with the approved hash. Returns the hash."""
    if approval is None or not approval.valid or approval.decision is not ApprovalDecision.APPROVED:
        raise ApprovalIntegrityError("the draft has no valid human approval")
    recomputed = compute_content_hash(draft.content)
    if recomputed != approval.content_hash or recomputed != draft.content_hash:
        raise ApprovalIntegrityError(
            "draft content no longer matches the approved content; re-approval is required",
            details={"approved_hash": approval.content_hash, "current_hash": recomputed},
        )
    return recomputed
