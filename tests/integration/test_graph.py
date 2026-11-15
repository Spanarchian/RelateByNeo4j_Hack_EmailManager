"""Relate graph repository against the real embedded engine."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import relate

from email_service.agents.base import ALL_AGENTS
from email_service.approvals.security import compute_content_hash
from email_service.exceptions import (
    ApprovalIntegrityError,
    ConflictError,
    DuplicateSubmissionError,
    GraphUnavailableError,
    InvalidStateError,
    NotFoundError,
)
from email_service.graph.database import RelateDatabase, Tx
from email_service.graph.repositories import GraphRepository
from email_service.graph.schema import Label, new_id
from email_service.models.approval import Approval, ApprovalDecision, ReviewReport
from email_service.models.email import (
    AttemptOutcome,
    DeliveryAttempt,
    DeliveryStatus,
    DraftStatus,
    EmailContent,
    EmailDraft,
)
from email_service.models.onboarding import EmployeeOnboardingRequest, OnboardingType
from email_service.onboarding.templates import DEFAULT_TEMPLATES
from email_service.services.onboarding_service import build_onboarding
from tests.conftest import Clock, employee_payload, volunteer_payload

pytestmark = [pytest.mark.integration, pytest.mark.graph]
NOW = datetime(2026, 11, 13, 9, tzinfo=UTC)


def volunteer() -> object:
    from email_service.models.onboarding import VolunteerOnboardingRequest

    return build_onboarding(VolunteerOnboardingRequest(**volunteer_payload()), "sam", NOW)


async def seed(repo: GraphRepository) -> None:
    await repo.ensure_schema()
    await repo.seed_reference_data(
        DEFAULT_TEMPLATES,
        [(a.id, a.name, a.kind, a.description) for a in ALL_AGENTS],
        [("gmail", "primary", True)],
    )


def make_draft(onboarding_id: str | None = None, template_id: str | None = None) -> EmailDraft:
    content = EmailContent(
        sender="s@example.org", recipients=["alex.morgan@example.org"], subject="Hi", body="Body"
    )
    return EmailDraft(
        id=new_id("draft"),
        status=DraftStatus.DRAFT,
        content=content,
        content_hash=compute_content_hash(content),
        onboarding_id=onboarding_id,
        template_id=template_id,
        prepared_by="agent:onboarding",
        requested_by="sam",
        created_at=NOW,
        updated_at=NOW,
    )


def passing_review(draft: EmailDraft) -> ReviewReport:
    return ReviewReport(agent_id="agent:review", content_hash=draft.content_hash, reviewed_at=NOW)


async def approved(repo: GraphRepository) -> EmailDraft:
    draft, _ = await repo.create_standalone_draft(make_draft())
    await repo.submit_for_review(draft.id, passing_review(draft))
    return await repo.record_decision(
        Approval(
            id=new_id("appr"),
            draft_id=draft.id,
            decision=ApprovalDecision.APPROVED,
            reviewer="riley",
            content_hash=draft.content_hash,
            decided_at=NOW,
        )
    )


async def test_schema_and_seed_are_idempotent(repository: GraphRepository) -> None:
    await seed(repository)
    await seed(repository)
    counts = await repository.label_counts()
    assert counts["EmailTemplate"] == 4
    assert counts["Agent"] == 2
    assert set(counts) == {label.value for label in Label}


async def test_constraints_exist(repository: GraphRepository) -> None:
    rows = await repository._db.read(lambda tx: tx.run("SHOW CONSTRAINTS"))
    names = {row["name"] for row in rows}
    assert {"person_id", "emaildraft_id", "emaildelivery_idempotency"} <= names


async def test_onboarding_graph_shape(repository: GraphRepository) -> None:
    onboarding = volunteer()
    created, is_new = await repository.create_onboarding(onboarding)  # type: ignore[arg-type]
    again, is_new_again = await repository.create_onboarding(onboarding)  # type: ignore[arg-type]
    assert (is_new, is_new_again) == (True, False)
    assert created == again
    assert created.start_date == date(2026, 12, 7)
    assert created.team_name == "Saturday Warehouse"

    query = """
    MATCH (p:Person)-[:UNDERGOES]->(o:Onboarding {id: $id})-[:FOR_ORGANISATION]->(org:Organisation)
    MATCH (p)-[:MEMBER_OF]->(t:Team)-[:PART_OF]->(org)
    RETURN p.email AS email, org.name AS org, t.name AS team
    """
    rows = await repository._db.read(lambda tx: tx.run(query, {"id": created.id}))
    assert rows == [
        {
            "email": "alex.morgan@example.org",
            "org": "Riverside Community Food Bank",
            "team": "Saturday Warehouse",
        }
    ]

    employee = build_onboarding(EmployeeOnboardingRequest(**employee_payload()), "sam", NOW)
    await repository.create_onboarding(employee)
    assert [o.id for o in await repository.list_onboardings(OnboardingType.EMPLOYEE)] == [
        employee.id
    ]
    assert len(await repository.list_onboardings()) == 2


async def test_draft_relationships_and_duplicate_guard(repository: GraphRepository) -> None:
    onboarding, _ = await repository.create_onboarding(volunteer())  # type: ignore[arg-type]
    draft = make_draft(onboarding.id, "volunteer-welcome")
    await repository.create_onboarding_draft(draft, "agent:onboarding")
    with pytest.raises(DuplicateSubmissionError):
        await repository.create_onboarding_draft(
            make_draft(onboarding.id, "volunteer-welcome"), "agent:onboarding"
        )
    query = """
    MATCH (a:Agent)-[pf:PREPARED_FOR]->(o:Onboarding)-[:HAS_DRAFT]->(d:EmailDraft)
    MATCH (o)-[:USES_TEMPLATE]->(t:EmailTemplate)
    RETURN a.id AS agent, pf.drafts AS drafts, t.id AS template, o.status AS status
    """
    rows = await repository._db.read(lambda tx: tx.run(query))
    assert rows == [
        {
            "agent": "agent:onboarding",
            "drafts": 1,
            "template": "volunteer-welcome",
            "status": "IN_PROGRESS",
        }
    ]
    with pytest.raises(NotFoundError):
        await repository.create_onboarding_draft(
            make_draft("onb-missing", "volunteer-welcome"), "agent:onboarding"
        )


async def test_full_delivery_lifecycle(repository: GraphRepository) -> None:
    draft = await approved(repository)
    assert draft.status is DraftStatus.APPROVED
    sending, delivery = await repository.claim_for_sending(draft.id, "riley")
    assert sending.status is DraftStatus.SENDING
    assert delivery.idempotency_key == f"{draft.id}#1"
    attempt = DeliveryAttempt(
        provider="gmail",
        attempt=1,
        outcome=AttemptOutcome.ACCEPTED,
        provider_message_id="gm-1",
        started_at=NOW,
        finished_at=NOW,
    )
    await repository.record_attempt(delivery.id, attempt)
    final, done = await repository.complete_delivery(
        delivery.id, status=DeliveryStatus.SENT, provider="gmail", provider_message_id="gm-1"
    )
    assert final.status is DraftStatus.SENT
    detail = await repository.get_draft_detail(draft.id)
    assert detail is not None
    assert detail.deliveries[0].attempts == [attempt]
    assert detail.deliveries[0].status is DeliveryStatus.SENT
    rows = await repository._db.read(
        lambda tx: tx.run(
            "MATCH (:EmailDraft {id: $id})-[:HAS_DELIVERY]->(:EmailDelivery)-[r:SENT_THROUGH]->(p:EmailProvider) "
            "RETURN p.id AS provider, r.attempts AS attempts, r.provider_message_id AS mid",
            {"id": draft.id},
        )
    )
    assert rows == [{"provider": "gmail", "attempts": 1, "mid": "gm-1"}]
    with pytest.raises(InvalidStateError):
        await repository.complete_delivery(
            delivery.id, status=DeliveryStatus.SENT, provider="gmail", provider_message_id="x"
        )


async def test_claim_is_atomic_under_concurrency(repository: GraphRepository) -> None:
    draft = await approved(repository)
    results = await asyncio.gather(
        *(repository.claim_for_sending(draft.id, "riley") for _ in range(10)),
        return_exceptions=True,
    )
    winners = [r for r in results if not isinstance(r, BaseException)]
    losers = [r for r in results if isinstance(r, DuplicateSubmissionError)]
    assert len(winners) == 1 and len(losers) == 9
    counts = await repository.label_counts()
    assert counts["EmailDelivery"] == 1


async def test_delivery_idempotency_key_is_unique_in_the_graph(repository: GraphRepository) -> None:
    draft = await approved(repository)
    _, delivery = await repository.claim_for_sending(draft.id, "riley")
    with pytest.raises(relate.IntegrityError):
        await repository._db.write(
            lambda tx: tx.run(
                "CREATE (:EmailDelivery {id: 'other', idempotency_key: $key})",
                {"key": delivery.idempotency_key},
            )
        )


async def test_claim_rechecks_approval_hash(repository: GraphRepository) -> None:
    draft = await approved(repository)
    await repository._db.write(
        lambda tx: tx.run(
            "MATCH (d:EmailDraft {id: $id}) SET d.subject = 'Changed'", {"id": draft.id}
        )
    )
    with pytest.raises(ApprovalIntegrityError):
        await repository.claim_for_sending(draft.id, "riley")
    assert (await repository.get_draft(draft.id)).status is DraftStatus.APPROVED  # type: ignore[union-attr]


async def test_decision_requires_matching_hash_and_status(repository: GraphRepository) -> None:
    draft, _ = await repository.create_standalone_draft(make_draft())
    decision = Approval(
        id=new_id("appr"),
        draft_id=draft.id,
        decision=ApprovalDecision.APPROVED,
        reviewer="riley",
        content_hash=draft.content_hash,
        decided_at=NOW,
    )
    with pytest.raises(InvalidStateError):  # still DRAFT
        await repository.record_decision(decision)
    await repository.submit_for_review(draft.id, passing_review(draft))
    with pytest.raises(ConflictError):
        await repository.record_decision(decision.model_copy(update={"content_hash": "f" * 64}))
    with pytest.raises(ConflictError):
        await repository.submit_for_review(
            draft.id, passing_review(draft).model_copy(update={"content_hash": "0" * 64})
        )


async def test_recover_interrupted_sends(repository: GraphRepository) -> None:
    draft = await approved(repository)
    await repository.claim_for_sending(draft.id, "riley")
    assert await repository.recover_interrupted_sends() == 1
    assert (await repository.get_draft(draft.id)).status is DraftStatus.UNKNOWN  # type: ignore[union-attr]
    assert [d.status for d in await repository.list_failed_deliveries()] == [DeliveryStatus.UNKNOWN]
    assert await repository.recover_interrupted_sends() == 0


async def test_reports(repository: GraphRepository) -> None:
    onboarding, _ = await repository.create_onboarding(volunteer())  # type: ignore[arg-type]
    draft = make_draft(onboarding.id, "volunteer-welcome")
    await repository.create_onboarding_draft(draft, "agent:onboarding")
    await repository.submit_for_review(draft.id, passing_review(draft))
    await repository.create_standalone_draft(make_draft())
    assert [d.id for d in await repository.list_unsent_onboarding_drafts()] == [draft.id]
    assert len(await repository.list_drafts([DraftStatus.PENDING_REVIEW])) == 1
    assert len(await repository.list_drafts([DraftStatus.DRAFT])) == 1
    assert len(await repository.onboarding_drafts(onboarding.id)) == 1
    assert await repository.get_draft("missing") is None
    assert await repository.get_draft_detail("missing") is None
    assert await repository.get_onboarding("missing") is None
    assert await repository.get_template("missing") is None


async def test_transaction_rolls_back_on_error(repository: GraphRepository) -> None:
    def work(tx: Tx) -> None:
        tx.run("CREATE (:Person {id: 'rollback-me'})")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await repository._db.write(work)
    rows = await repository._db.read(
        lambda tx: tx.run("MATCH (p:Person {id: 'rollback-me'}) RETURN p")
    )
    assert rows == []


# --- persistence and single-writer behaviour -------------------------------------------


async def test_data_persists_across_reopen(tmp_path: Path) -> None:
    store = str(tmp_path / "graph" / "store")
    db = RelateDatabase(store)
    await db.open()
    repo = GraphRepository(db, Clock())
    await seed(repo)
    onboarding, _ = await repo.create_onboarding(volunteer())  # type: ignore[arg-type]
    await db.close()

    reopened = RelateDatabase(store)
    await reopened.open()
    repo2 = GraphRepository(reopened, Clock())
    loaded = await repo2.get_onboarding(onboarding.id)
    assert loaded == onboarding
    await seed(repo2)  # idempotent on an existing store
    assert (await repo2.label_counts())["EmailTemplate"] == 4
    await reopened.close()


async def test_second_writer_is_refused_but_reader_is_allowed(tmp_path: Path) -> None:
    store = str(tmp_path / "store")
    writer = RelateDatabase(store)
    await writer.open()
    await seed(GraphRepository(writer, Clock()))

    second = RelateDatabase(store)
    with pytest.raises(GraphUnavailableError, match="locked"):
        await second.open()

    reader = RelateDatabase(store, read_only=True)
    await reader.open()
    counts = await GraphRepository(reader, Clock()).label_counts()
    assert counts["EmailTemplate"] == 4
    await reader.close()
    await writer.close()


async def test_closed_database_reports_unavailable() -> None:
    db = RelateDatabase(":memory:")
    assert not await db.ping()
    with pytest.raises(GraphUnavailableError):
        await db.read(lambda tx: tx.run("RETURN 1"))
    await db.open()
    await db.open()  # idempotent
    assert db.is_open and await db.ping()
    await db.close()
    await db.close()
