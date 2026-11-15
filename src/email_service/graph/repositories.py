"""Repository over the Relate graph. The only module that knows about Cypher."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

from email_service.approvals.security import verify_approval_integrity
from email_service.exceptions import (
    ConflictError,
    DuplicateSubmissionError,
    InvalidStateError,
    NotFoundError,
)
from email_service.graph import queries as q
from email_service.graph.database import RelateDatabase, Row, Tx
from email_service.graph.schema import CONSTRAINTS, Label, delivery_idempotency_key, new_id
from email_service.models.approval import Approval, ApprovalDecision, ReviewReport
from email_service.models.email import (
    DeliveryAttempt,
    DeliveryStatus,
    DraftDetail,
    DraftStatus,
    EmailContent,
    EmailDelivery,
    EmailDraft,
)
from email_service.models.onboarding import Onboarding, OnboardingStatus, OnboardingType
from email_service.onboarding.templates import EmailTemplate
from email_service.onboarding.workflow import SENDABLE, UNSENT, ensure_transition

Clock = Callable[[], datetime]


# --- (de)serialisation --------------------------------------------------------


def _draft_props(draft: EmailDraft) -> dict[str, Any]:
    return {
        "id": draft.id,
        "status": draft.status.value,
        "sender": draft.content.sender,
        "recipients": list(draft.content.recipients),
        "subject": draft.content.subject,
        "body": draft.content.body,
        "html_body": draft.content.html_body,
        "content_hash": draft.content_hash,
        "onboarding_id": draft.onboarding_id,
        "template_id": draft.template_id,
        "prepared_by": draft.prepared_by,
        "requested_by": draft.requested_by,
        "created_at": draft.created_at,
        "updated_at": draft.updated_at,
        "version": draft.version,
        "delivery_count": draft.delivery_count,
        "review_json": draft.review.model_dump_json() if draft.review else None,
        "idempotency_key": draft.idempotency_key,
    }


def _draft(props: Row) -> EmailDraft:
    review = props.get("review_json")
    return EmailDraft(
        id=props["id"],
        status=DraftStatus(props["status"]),
        content=EmailContent(
            sender=props["sender"],
            recipients=props["recipients"],
            subject=props["subject"],
            body=props["body"],
            html_body=props.get("html_body"),
        ),
        content_hash=props["content_hash"],
        onboarding_id=props.get("onboarding_id"),
        template_id=props.get("template_id"),
        prepared_by=props["prepared_by"],
        requested_by=props["requested_by"],
        created_at=props["created_at"],
        updated_at=props["updated_at"],
        version=props.get("version", 1),
        delivery_count=props.get("delivery_count", 0),
        review=ReviewReport.model_validate_json(review) if review else None,
        idempotency_key=props.get("idempotency_key"),
    )


def _approval(props: Row) -> Approval:
    return Approval.model_validate(props)


def _approval_props(approval: Approval) -> dict[str, Any]:
    data = approval.model_dump()
    data["decision"] = approval.decision.value
    return data


def _delivery(props: Row) -> EmailDelivery:
    data = dict(props)
    attempts = json.loads(data.pop("attempts_json", None) or "[]")
    return EmailDelivery.model_validate({**data, "attempts": attempts})


def _delivery_props(delivery: EmailDelivery) -> dict[str, Any]:
    data = delivery.model_dump(exclude={"attempts"})
    data["status"] = delivery.status.value
    data["attempts_json"] = json.dumps([a.model_dump(mode="json") for a in delivery.attempts])
    return data


def _onboarding(row: Row) -> Onboarding:
    ob, person, org, team = row["ob"], row["p"], row["o"], row["t"]
    return Onboarding(
        id=ob["id"],
        type=OnboardingType(ob["type"]),
        status=OnboardingStatus(ob["status"]),
        person_id=person["id"],
        person_name=person["name"],
        person_email=person["email"],
        organisation_id=org["id"],
        organisation_name=org["name"],
        team_id=team["id"],
        team_name=team["name"],
        role_title=ob["role_title"],
        contact_name=ob["contact_name"],
        start_date=ob["start_date"],
        details=json.loads(ob["details_json"]),
        created_by=ob["created_by"],
        created_at=ob["created_at"],
    )


def _template(props: Row) -> EmailTemplate:
    from email_service.models.onboarding import TemplateKind

    return EmailTemplate(
        id=props["id"],
        version=props["version"],
        name=props["name"],
        kind=TemplateKind(props["kind"]),
        onboarding_type=OnboardingType(props["onboarding_type"]),
        subject=props["subject"],
        text=props["text"],
        html=props["html"],
        required_fields=tuple(props["required_fields"]),
    )


# --- repository -----------------------------------------------------------------


class GraphRepository:
    def __init__(self, db: RelateDatabase, clock: Clock) -> None:
        self._db = db
        self._now = clock

    # schema & reference data ---------------------------------------------------

    async def ensure_schema(self) -> None:
        def work(tx: Tx) -> None:
            for statement in CONSTRAINTS:
                tx.run(statement)

        await self._db.read(work)  # schema statements run in autocommit mode

    async def seed_reference_data(
        self,
        templates: Iterable[EmailTemplate],
        agents: Iterable[tuple[str, str, str, str]],
        providers: Iterable[tuple[str, str, bool]],
    ) -> None:
        """Idempotent: safe to run on every start-up."""
        now = self._now()
        template_list, agent_list, provider_list = list(templates), list(agents), list(providers)

        def work(tx: Tx) -> None:
            for t in template_list:
                tx.run(
                    q.SEED_TEMPLATE,
                    {
                        "id": t.id,
                        "version": t.version,
                        "name": t.name,
                        "kind": t.kind.value,
                        "onboarding_type": t.onboarding_type.value,
                        "subject": t.subject,
                        "text": t.text,
                        "html": t.html,
                        "required_fields": list(t.required_fields),
                        "now": now,
                    },
                )
            for agent_id, name, kind, description in agent_list:
                tx.run(
                    q.SEED_AGENT,
                    {
                        "id": agent_id,
                        "name": name,
                        "kind": kind,
                        "description": description,
                        "now": now,
                    },
                )
            for provider, role, enabled in provider_list:
                tx.run(
                    q.SEED_PROVIDER, {"id": provider, "role": role, "enabled": enabled, "now": now}
                )

        await self._db.write(work)

    async def get_template(self, template_id: str) -> EmailTemplate | None:
        row = await self._db.read(lambda tx: tx.single(q.GET_TEMPLATE, {"id": template_id}))
        return _template(row["t"]) if row else None

    # onboarding -------------------------------------------------------------------

    async def create_onboarding(self, onboarding: Onboarding) -> tuple[Onboarding, bool]:
        """Create the onboarding and its people/organisation/team; idempotent by id."""

        def work(tx: Tx) -> tuple[Onboarding, bool]:
            existing = tx.single(q.GET_ONBOARDING, {"id": onboarding.id})
            if existing:
                return _onboarding(existing), False
            tx.run(
                q.CREATE_ONBOARDING,
                {
                    "org_id": onboarding.organisation_id,
                    "org_name": onboarding.organisation_name,
                    "person_id": onboarding.person_id,
                    "person_name": onboarding.person_name,
                    "person_email": onboarding.person_email,
                    "team_id": onboarding.team_id,
                    "team_name": onboarding.team_name,
                    "now": onboarding.created_at,
                    "onboarding": {
                        "id": onboarding.id,
                        "type": onboarding.type.value,
                        "status": onboarding.status.value,
                        "team_id": onboarding.team_id,
                        "role_title": onboarding.role_title,
                        "contact_name": onboarding.contact_name,
                        "start_date": onboarding.start_date,
                        "details_json": json.dumps(onboarding.details, sort_keys=True),
                        "created_by": onboarding.created_by,
                        "created_at": onboarding.created_at,
                    },
                },
            )
            created = tx.single(q.GET_ONBOARDING, {"id": onboarding.id})
            assert created is not None
            return _onboarding(created), True

        return await self._db.write(work)

    async def get_onboarding(self, onboarding_id: str) -> Onboarding | None:
        row = await self._db.read(lambda tx: tx.single(q.GET_ONBOARDING, {"id": onboarding_id}))
        return _onboarding(row) if row else None

    async def list_onboardings(
        self, onboarding_type: OnboardingType | None = None, limit: int = 100
    ) -> list[Onboarding]:
        params = {"type": onboarding_type.value if onboarding_type else None, "limit": limit}
        rows = await self._db.read(lambda tx: tx.run(q.LIST_ONBOARDINGS, params))
        return [_onboarding(row) for row in rows]

    # drafts -----------------------------------------------------------------------

    async def create_onboarding_draft(self, draft: EmailDraft, agent_id: str) -> EmailDraft:
        """Store an agent-prepared draft; at most one active draft per template."""
        assert draft.onboarding_id
        assert draft.template_id

        def work(tx: Tx) -> EmailDraft:
            active = tx.single(
                q.ACTIVE_DRAFT_FOR_TEMPLATE,
                {"onboarding_id": draft.onboarding_id, "template_id": draft.template_id},
            )
            if active:
                raise DuplicateSubmissionError(
                    "an active draft already exists for this onboarding and template",
                    details={"draft_id": active["id"]},
                )
            row = tx.single(
                q.CREATE_ONBOARDING_DRAFT,
                {
                    "onboarding_id": draft.onboarding_id,
                    "template_id": draft.template_id,
                    "agent_id": agent_id,
                    "draft": _draft_props(draft),
                    "now": draft.created_at,
                },
            )
            if row is None:
                raise NotFoundError("onboarding, template or agent not found")
            return draft

        return await self._db.write(work)

    async def create_standalone_draft(self, draft: EmailDraft) -> tuple[EmailDraft, bool]:
        """Store an API-submitted draft. Returns ``(draft, created)``; idempotent by id."""

        def work(tx: Tx) -> tuple[EmailDraft, bool]:
            existing = tx.single(q.GET_DRAFT, {"id": draft.id})
            if existing:
                return _draft(existing["d"]), False
            tx.run(q.CREATE_STANDALONE_DRAFT, {"draft": _draft_props(draft)})
            return draft, True

        return await self._db.write(work)

    async def get_draft(self, draft_id: str) -> EmailDraft | None:
        row = await self._db.read(lambda tx: tx.single(q.GET_DRAFT, {"id": draft_id}))
        return _draft(row["d"]) if row else None

    async def get_draft_detail(self, draft_id: str) -> DraftDetail | None:
        def work(tx: Tx) -> DraftDetail | None:
            row = tx.single(q.GET_DRAFT, {"id": draft_id})
            if row is None:
                return None
            approvals = tx.run(q.DRAFT_APPROVALS, {"id": draft_id})
            deliveries = tx.run(q.DRAFT_DELIVERIES, {"id": draft_id})
            return DraftDetail(
                draft=_draft(row["d"]),
                approvals=[_approval(r["a"]) for r in approvals],
                deliveries=[_delivery(r["x"]) for r in deliveries],
            )

        return await self._db.read(work)

    async def list_drafts(
        self, statuses: Iterable[DraftStatus], limit: int = 200
    ) -> list[EmailDraft]:
        params = {"statuses": [s.value for s in statuses], "limit": limit}
        rows = await self._db.read(lambda tx: tx.run(q.DRAFTS_BY_STATUS, params))
        return [_draft(row["d"]) for row in rows]

    async def list_unsent_onboarding_drafts(self, limit: int = 200) -> list[EmailDraft]:
        params = {"statuses": [s.value for s in UNSENT], "limit": limit}
        rows = await self._db.read(lambda tx: tx.run(q.UNSENT_ONBOARDING_DRAFTS, params))
        return [_draft(row["d"]) for row in rows]

    async def list_failed_deliveries(self, limit: int = 200) -> list[EmailDelivery]:
        rows = await self._db.read(lambda tx: tx.run(q.FAILED_DELIVERIES, {"limit": limit}))
        return [_delivery(row["x"]) for row in rows]

    async def onboarding_drafts(self, onboarding_id: str) -> list[DraftDetail]:
        def work(tx: Tx) -> list[DraftDetail]:
            details = []
            for row in tx.run(q.ONBOARDING_DRAFTS, {"id": onboarding_id}):
                draft = _draft(row["d"])
                details.append(
                    DraftDetail(
                        draft=draft,
                        approvals=[
                            _approval(r["a"]) for r in tx.run(q.DRAFT_APPROVALS, {"id": draft.id})
                        ],
                        deliveries=[
                            _delivery(r["x"]) for r in tx.run(q.DRAFT_DELIVERIES, {"id": draft.id})
                        ],
                    )
                )
            return details

        return await self._db.read(work)

    # state changes ------------------------------------------------------------------

    @staticmethod
    def _load_draft(tx: Tx, draft_id: str) -> EmailDraft:
        row = tx.single(q.GET_DRAFT, {"id": draft_id})
        if row is None:
            raise NotFoundError("email draft not found", details={"draft_id": draft_id})
        return _draft(row["d"])

    def _set_status(self, tx: Tx, draft: EmailDraft, target: DraftStatus) -> EmailDraft:
        ensure_transition(draft.status, target)
        now = self._now()
        tx.run(
            q.UPDATE_DRAFT, {"id": draft.id, "props": {"status": target.value, "updated_at": now}}
        )
        return draft.model_copy(update={"status": target, "updated_at": now})

    async def submit_for_review(self, draft_id: str, review: ReviewReport) -> EmailDraft:
        """DRAFT -> PENDING_REVIEW with the review agent's findings attached."""

        def work(tx: Tx) -> EmailDraft:
            draft = self._load_draft(tx, draft_id)
            if review.content_hash != draft.content_hash:
                raise ConflictError("review does not match the current draft content")
            ensure_transition(draft.status, DraftStatus.PENDING_REVIEW)
            now = self._now()
            props = {
                "status": DraftStatus.PENDING_REVIEW.value,
                "review_json": review.model_dump_json(),
                "updated_at": now,
            }
            tx.run(q.UPDATE_DRAFT, {"id": draft_id, "props": props})
            return draft.model_copy(
                update={"status": DraftStatus.PENDING_REVIEW, "review": review, "updated_at": now}
            )

        return await self._db.write(work)

    async def update_content(
        self,
        draft_id: str,
        *,
        content: EmailContent,
        content_hash: str,
        review: ReviewReport,
        expected_version: int | None,
        allowed_from: frozenset[DraftStatus],
    ) -> EmailDraft:
        """Replace content, invalidate every approval and return to PENDING_REVIEW."""

        def work(tx: Tx) -> EmailDraft:
            draft = self._load_draft(tx, draft_id)
            if expected_version is not None and expected_version != draft.version:
                raise ConflictError(
                    "the draft was modified by someone else",
                    details={"expected_version": expected_version, "version": draft.version},
                )
            if draft.status not in allowed_from:
                raise InvalidStateError(f"a draft in status {draft.status} cannot be edited")
            now = self._now()
            tx.run(
                q.INVALIDATE_APPROVALS,
                {"id": draft_id, "now": now, "reason": "content edited"},
            )
            updated = draft.model_copy(
                update={
                    "content": content,
                    "content_hash": content_hash,
                    "review": review,
                    "status": DraftStatus.PENDING_REVIEW,
                    "version": draft.version + 1,
                    "updated_at": now,
                }
            )
            props = _draft_props(updated)
            tx.run(q.UPDATE_DRAFT, {"id": draft_id, "props": props})
            return updated

        return await self._db.write(work)

    async def record_decision(self, approval: Approval) -> EmailDraft:
        """Atomically record a human decision against the exact reviewed content."""

        def work(tx: Tx) -> EmailDraft:
            draft = self._load_draft(tx, approval.draft_id)
            if draft.status is not DraftStatus.PENDING_REVIEW:
                raise InvalidStateError(
                    f"only drafts pending review can be decided (status is {draft.status})"
                )
            if approval.content_hash != draft.content_hash:
                raise ConflictError(
                    "the draft changed since it was reviewed; reload and review again",
                    details={"current_hash": draft.content_hash},
                )
            if approval.decision is ApprovalDecision.APPROVED and (
                draft.review is None
                or not draft.review.passed
                or draft.review.content_hash != draft.content_hash
            ):
                raise InvalidStateError("the draft has blocking review findings")
            target = (
                DraftStatus.APPROVED
                if approval.decision is ApprovalDecision.APPROVED
                else DraftStatus.REJECTED
            )
            tx.run(
                q.INVALIDATE_APPROVALS,
                {"id": draft.id, "now": approval.decided_at, "reason": "superseded"},
            )
            tx.run(q.CREATE_APPROVAL, {"draft_id": draft.id, "approval": _approval_props(approval)})
            return self._set_status(tx, draft, target)

        return await self._db.write(work)

    async def valid_approval(self, draft_id: str) -> Approval | None:
        rows = await self._db.read(lambda tx: tx.run(q.DRAFT_APPROVALS, {"id": draft_id}))
        valid = [
            _approval(r["a"])
            for r in rows
            if r["a"].get("valid") and r["a"]["decision"] == ApprovalDecision.APPROVED.value
        ]
        return valid[-1] if valid else None

    async def claim_for_sending(
        self, draft_id: str, initiated_by: str
    ) -> tuple[EmailDraft, EmailDelivery]:
        """The duplicate-send guard.

        In one transaction (and on the single graph worker thread): verify the
        status, re-verify the approval hash against freshly recomputed content,
        allocate the next delivery sequence number, create a durable PENDING
        delivery with a unique idempotency key, and move the draft to SENDING.
        A concurrent second call sees SENDING and is refused.
        """

        def work(tx: Tx) -> tuple[EmailDraft, EmailDelivery]:
            draft = self._load_draft(tx, draft_id)
            if draft.status not in SENDABLE:
                raise DuplicateSubmissionError(
                    f"draft cannot be sent from status {draft.status}",
                    details={"status": draft.status.value},
                )
            approvals = [_approval(r["a"]) for r in tx.run(q.DRAFT_APPROVALS, {"id": draft_id})]
            valid = [a for a in approvals if a.valid and a.decision is ApprovalDecision.APPROVED]
            content_hash = verify_approval_integrity(draft, valid[-1] if valid else None)
            sequence = draft.delivery_count + 1
            now = self._now()
            delivery = EmailDelivery(
                id=new_id("dlv"),
                draft_id=draft_id,
                idempotency_key=delivery_idempotency_key(draft_id, sequence),
                sequence=sequence,
                status=DeliveryStatus.PENDING,
                content_hash=content_hash,
                initiated_by=initiated_by,
                created_at=now,
            )
            tx.run(q.CREATE_DELIVERY, {"draft_id": draft_id, "delivery": _delivery_props(delivery)})
            tx.run(q.UPDATE_DRAFT, {"id": draft_id, "props": {"delivery_count": sequence}})
            sending = self._set_status(tx, draft, DraftStatus.SENDING)
            return sending.model_copy(update={"delivery_count": sequence}), delivery

        return await self._db.write(work)

    async def record_attempt(self, delivery_id: str, attempt: DeliveryAttempt) -> None:
        """Durably append one provider attempt before any further action is taken."""

        def work(tx: Tx) -> None:
            row = tx.single(q.GET_DELIVERY, {"id": delivery_id})
            if row is None:
                raise NotFoundError("delivery not found")
            delivery = _delivery(row["x"])
            attempts = [*delivery.attempts, attempt]
            tx.run(
                q.UPDATE_DELIVERY,
                {
                    "id": delivery_id,
                    "props": {
                        "attempts_json": json.dumps([a.model_dump(mode="json") for a in attempts])
                    },
                },
            )
            tx.run(
                q.LINK_DELIVERY_PROVIDER,
                {
                    "id": delivery_id,
                    "provider": attempt.provider,
                    "outcome": attempt.outcome.value,
                    "message_id": attempt.provider_message_id,
                    "now": attempt.finished_at,
                },
            )

        await self._db.write(work)

    async def complete_delivery(
        self,
        delivery_id: str,
        *,
        status: DeliveryStatus,
        provider: str | None,
        provider_message_id: str | None,
    ) -> tuple[EmailDraft, EmailDelivery]:
        draft_status = {
            DeliveryStatus.SENT: DraftStatus.SENT,
            DeliveryStatus.FAILED: DraftStatus.FAILED,
            DeliveryStatus.UNKNOWN: DraftStatus.UNKNOWN,
        }[status]

        def work(tx: Tx) -> tuple[EmailDraft, EmailDelivery]:
            row = tx.single(q.GET_DELIVERY, {"id": delivery_id})
            if row is None:
                raise NotFoundError("delivery not found")
            delivery = _delivery(row["x"])
            if delivery.status is not DeliveryStatus.PENDING:
                raise InvalidStateError("delivery already completed")
            now = self._now()
            props = {
                "status": status.value,
                "provider": provider,
                "provider_message_id": provider_message_id,
                "completed_at": now,
            }
            tx.run(q.UPDATE_DELIVERY, {"id": delivery_id, "props": props})
            draft = self._set_status(tx, self._load_draft(tx, delivery.draft_id), draft_status)
            return draft, delivery.model_copy(
                update={
                    "status": status,
                    "provider": provider,
                    "provider_message_id": provider_message_id,
                    "completed_at": now,
                }
            )

        return await self._db.write(work)

    async def reconcile(
        self, draft_id: str, outcome: DeliveryStatus, actor: str, note: str
    ) -> tuple[EmailDraft, EmailDelivery]:
        """Human resolution of an UNKNOWN delivery."""

        def work(tx: Tx) -> tuple[EmailDraft, EmailDelivery]:
            draft = self._load_draft(tx, draft_id)
            if draft.status is not DraftStatus.UNKNOWN:
                raise InvalidStateError("only drafts with an UNKNOWN delivery can be reconciled")
            rows = tx.run(q.DRAFT_DELIVERIES, {"id": draft_id})
            delivery = _delivery(rows[-1]["x"])
            now = self._now()
            props = {
                "status": outcome.value,
                "reconciled_by": actor,
                "reconciliation_note": note,
                "completed_at": delivery.completed_at or now,
            }
            tx.run(q.UPDATE_DELIVERY, {"id": delivery.id, "props": props})
            target = DraftStatus.SENT if outcome is DeliveryStatus.SENT else DraftStatus.FAILED
            updated = self._set_status(tx, draft, target)
            return updated, delivery.model_copy(
                update={"status": outcome, "reconciled_by": actor, "reconciliation_note": note}
            )

        return await self._db.write(work)

    async def cancel(self, draft_id: str) -> EmailDraft:
        def work(tx: Tx) -> EmailDraft:
            draft = self._load_draft(tx, draft_id)
            tx.run(
                q.INVALIDATE_APPROVALS, {"id": draft_id, "now": self._now(), "reason": "cancelled"}
            )
            return self._set_status(tx, draft, DraftStatus.CANCELLED)

        return await self._db.write(work)

    async def recover_interrupted_sends(self) -> int:
        """At start-up, a draft left in SENDING has an unknowable outcome -> UNKNOWN."""

        def work(tx: Tx) -> int:
            rows = tx.run(q.INTERRUPTED_DELIVERIES)
            now = self._now()
            for row in rows:
                tx.run(
                    q.UPDATE_DELIVERY,
                    {
                        "id": row["delivery_id"],
                        "props": {"status": DeliveryStatus.UNKNOWN.value, "completed_at": now},
                    },
                )
                draft = self._load_draft(tx, row["draft_id"])
                self._set_status(tx, draft, DraftStatus.UNKNOWN)
            return len(rows)

        return await self._db.write(work)

    # diagnostics -----------------------------------------------------------------------

    async def label_counts(self) -> dict[str, int]:
        labels = [label.value for label in Label]
        rows = await self._db.read(lambda tx: tx.run(q.LABEL_COUNTS, {"labels": labels}))
        return {row["label"]: row["n"] for row in rows}
