"""Content hashing, reviewer verification, authentication and permissions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from email_service.approvals.security import (
    compute_content_hash,
    verify_approval_integrity,
    verify_reviewer,
    verify_sender_authority,
)
from email_service.exceptions import ApprovalIntegrityError, PermissionDeniedError
from email_service.models.approval import Approval, ApprovalDecision
from email_service.models.email import DraftStatus, EmailContent, EmailDraft
from email_service.security.authentication import (
    ApiKeyAuthenticator,
    ApiKeyRecord,
    Principal,
    generate_api_key,
    hash_api_key,
)
from email_service.security.permissions import Permission, PrincipalKind, Role
from email_service.security.rate_limit import FixedWindowLimiter
from email_service.security.redaction import redact

pytestmark = [pytest.mark.unit, pytest.mark.workflow]
NOW = datetime(2026, 11, 13, tzinfo=UTC)

BASE = EmailContent(
    sender="sender@example.org",
    recipients=["a@example.org", "b@example.org"],
    subject="Subject",
    body="Body",
    html_body="<p>Body</p>",
)


def draft(content: EmailContent = BASE, **overrides: Any) -> EmailDraft:
    data: dict[str, Any] = {
        "id": "draft-1",
        "status": DraftStatus.APPROVED,
        "content": content,
        "content_hash": compute_content_hash(content),
        "prepared_by": "agent:onboarding",
        "requested_by": "sam.submitter",
        "created_at": NOW,
        "updated_at": NOW,
    }
    data.update(overrides)
    return EmailDraft(**data)


def approval(content_hash: str, **overrides: Any) -> Approval:
    data: dict[str, Any] = {
        "id": "appr-1",
        "draft_id": "draft-1",
        "decision": ApprovalDecision.APPROVED,
        "reviewer": "riley.reviewer",
        "content_hash": content_hash,
        "decided_at": NOW,
    }
    data.update(overrides)
    return Approval(**data)


REVIEWER = Principal(subject="riley.reviewer", roles=frozenset({Role.REVIEWER}))


class TestContentHash:
    def test_stable_and_hex(self) -> None:
        digest = compute_content_hash(BASE)
        assert digest == compute_content_hash(BASE.model_copy())
        assert len(digest) == 64
        int(digest, 16)

    @pytest.mark.parametrize(
        "change",
        [
            {"sender": "other@example.org"},
            {"recipients": ["a@example.org"]},
            {"recipients": ["b@example.org", "a@example.org"]},
            {"subject": "Subject!"},
            {"body": "Body "},
            {"html_body": "<p>Body!</p>"},
            {"html_body": None},
        ],
    )
    def test_every_covered_field_changes_the_hash(self, change: dict[str, Any]) -> None:
        changed = EmailContent(**{**BASE.model_dump(), **change})
        assert compute_content_hash(changed) != compute_content_hash(BASE)

    def test_address_case_does_not_change_hash(self) -> None:
        upper = EmailContent(**{**BASE.model_dump(), "sender": "Sender@example.org"})
        assert compute_content_hash(upper) == compute_content_hash(BASE)


class TestApprovalIntegrity:
    def test_valid(self) -> None:
        d = draft()
        assert verify_approval_integrity(d, approval(d.content_hash)) == d.content_hash

    def test_missing_invalid_or_rejected_approval(self) -> None:
        d = draft()
        with pytest.raises(ApprovalIntegrityError):
            verify_approval_integrity(d, None)
        with pytest.raises(ApprovalIntegrityError):
            verify_approval_integrity(d, approval(d.content_hash, valid=False))
        with pytest.raises(ApprovalIntegrityError):
            verify_approval_integrity(
                d, approval(d.content_hash, decision=ApprovalDecision.REJECTED)
            )

    def test_content_changed_after_approval(self) -> None:
        original = draft()
        tampered = original.model_copy(
            update={"content": BASE.model_copy(update={"body": "Changed"})}
        )
        with pytest.raises(ApprovalIntegrityError, match="no longer matches"):
            verify_approval_integrity(tampered, approval(original.content_hash))

    def test_stored_hash_mismatch(self) -> None:
        d = draft(content_hash="0" * 64)
        with pytest.raises(ApprovalIntegrityError):
            verify_approval_integrity(d, approval(compute_content_hash(BASE)))


class TestReviewerVerification:
    def test_reviewer_ok(self) -> None:
        verify_reviewer(REVIEWER, draft(), four_eyes=True)

    def test_non_human_rejected(self) -> None:
        bot = Principal(subject="svc", roles=frozenset({Role.REVIEWER}), kind=PrincipalKind.SERVICE)
        with pytest.raises(PermissionDeniedError, match="human"):
            verify_reviewer(bot, draft(), four_eyes=True)

    def test_role_required(self) -> None:
        submitter = Principal(subject="x", roles=frozenset({Role.SUBMITTER}))
        with pytest.raises(PermissionDeniedError):
            verify_reviewer(submitter, draft(), four_eyes=True)

    def test_preparer_cannot_review(self) -> None:
        with pytest.raises(PermissionDeniedError, match="prepared"):
            verify_reviewer(REVIEWER, draft(prepared_by="riley.reviewer"), four_eyes=False)

    def test_four_eyes(self) -> None:
        own = draft(requested_by="riley.reviewer")
        with pytest.raises(PermissionDeniedError, match="four-eyes"):
            verify_reviewer(REVIEWER, own, four_eyes=True)
        verify_reviewer(REVIEWER, own, four_eyes=False)

    def test_sender_authority(self) -> None:
        verify_sender_authority(REVIEWER)
        with pytest.raises(PermissionDeniedError):
            verify_sender_authority(Principal(subject="s", roles=frozenset({Role.SUBMITTER})))


class TestAuthentication:
    def test_generated_key_round_trip(self) -> None:
        key, digest = generate_api_key()
        assert key.startswith("goa_")
        assert digest == hash_api_key(key)
        auth = ApiKeyAuthenticator(
            [
                ApiKeyRecord(
                    name="u",
                    key_sha256=digest,
                    roles=frozenset({Role.REVIEWER}),
                    kind=PrincipalKind.HUMAN,
                )
            ]
        )
        principal = auth.authenticate(key)
        assert principal is not None
        assert principal.subject == "u"
        assert auth.authenticate("wrong") is None
        assert auth.authenticate(None) is None
        assert auth.authenticate("x" * 1000) is None

    def test_permissions(self) -> None:
        reviewer = Principal(subject="r", roles=frozenset({Role.REVIEWER}))
        admin = Principal(subject="a", roles=frozenset({Role.ADMIN}))
        agent = Principal(
            subject="agent:x", roles=frozenset({Role.REVIEWER}), kind=PrincipalKind.AGENT
        )
        assert reviewer.has(Permission.APPROVE) and reviewer.has(Permission.SEND)
        assert not admin.has(Permission.APPROVE)
        assert not agent.has(Permission.APPROVE)
        assert not agent.has(Permission.SEND)
        assert agent.has(Permission.READ)


class TestRateLimitAndRedaction:
    def test_fixed_window(self) -> None:
        now = [0.0]
        limiter = FixedWindowLimiter(2, clock=lambda: now[0])
        assert limiter.allow("c")[0] and limiter.allow("c")[0]
        allowed, reset = limiter.allow("c")
        assert not allowed and 0 < reset <= 60
        assert limiter.allow("other")[0]
        now[0] = 61.0
        assert limiter.allow("c")[0]

    @pytest.mark.parametrize(
        ("text", "secret"),
        [
            ("Authorization: Bearer ya29.a0AfH6SMBx", "ya29.a0AfH6SMBx"),
            ('{"key": "md-abcdef123456"}', "md-abcdef123456"),
            ("token=1//0gAbCdEfGhIjKlMnOpQrStUv", "1//0gAbCdEfGhIjKlMnOpQrStUv"),
            ("client goa_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345", "goa_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"),
            ("sending to alex.morgan@example.org", "alex.morgan"),
        ],
    )
    def test_redaction(self, text: str, secret: str) -> None:
        assert secret not in redact(text)
