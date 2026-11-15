"""Gmail + Mailchimp adapters wired through ProviderService and EmailService,
with delivery records persisted in Relate."""

from __future__ import annotations

import httpx
import pytest
import respx

from email_service.dependencies import Container
from email_service.graph.schema import Label
from email_service.models.email import DraftStatus, EmailRequest
from email_service.security.authentication import Principal
from email_service.security.permissions import Role
from tests.conftest import gmail_ok, mailchimp_ok

pytestmark = [pytest.mark.integration, pytest.mark.providers]

SUBMITTER = Principal(subject="sam", roles=frozenset({Role.SUBMITTER}))
REVIEWER = Principal(subject="riley", roles=frozenset({Role.REVIEWER}))


async def approved_draft_id(container: Container) -> str:
    draft, _ = await container.email_service.submit(
        EmailRequest(
            title="Rota update",
            body="Hello Alex, the Saturday rota for December is now available on the noticeboard.",
            sender="sender@example.org",
            recipients=["alex@example.org"],
        ),
        SUBMITTER,
        None,
    )
    await container.approval_service.approve(draft.id, REVIEWER, draft.content_hash, None)
    return draft.id


@pytest.fixture
async def started(container: Container) -> object:
    await container.start()
    yield container
    await container.stop()


async def test_failover_records_every_attempt_in_the_graph(
    started: Container, gmail_route: respx.Route, mailchimp_route: respx.Route
) -> None:
    gmail_route.mock(side_effect=httpx.ConnectError("refused"))  # retryable, then definitive
    mailchimp_route.mock(return_value=mailchimp_ok("alex@example.org"))
    draft_id = await approved_draft_id(started)

    result = await started.email_service.send(draft_id, REVIEWER)
    assert result.draft_status is DraftStatus.SENT
    assert gmail_route.call_count == 2  # 1 try + 1 retry (PROVIDER_MAX_RETRIES=1 in tests)
    assert [(a.provider, a.outcome.value) for a in result.delivery.attempts] == [
        ("gmail", "REJECTED"),
        ("gmail", "REJECTED"),
        ("mailchimp", "ACCEPTED"),
    ]
    rows = await started.db.read(
        lambda tx: tx.run(
            "MATCH (x:EmailDelivery)-[r:SENT_THROUGH]->(p:EmailProvider) "
            "RETURN p.id AS provider, r.attempts AS attempts, r.last_outcome AS outcome "
            "ORDER BY provider"
        )
    )
    assert rows == [
        {"provider": "gmail", "attempts": 2, "outcome": "REJECTED"},
        {"provider": "mailchimp", "attempts": 1, "outcome": "ACCEPTED"},
    ]


async def test_gmail_success_records_message_id(
    started: Container, gmail_route: respx.Route
) -> None:
    gmail_route.mock(return_value=gmail_ok("gm-777"))
    draft_id = await approved_draft_id(started)
    result = await started.email_service.send(draft_id, REVIEWER)
    assert result.delivery.provider_message_id == "gm-777"
    counts = await started.repository.label_counts()
    assert counts[Label.EMAIL_DELIVERY.value] == 1
    assert counts[Label.APPROVAL.value] == 1


async def test_mailchimp_disabled_means_no_failover(
    started: Container, gmail_route: respx.Route, mailchimp_route: respx.Route
) -> None:
    started.provider_service._providers["mailchimp"]._api_key = None  # type: ignore[attr-defined]
    gmail_route.mock(return_value=httpx.Response(400, json={"error": {"message": "bad"}}))
    draft_id = await approved_draft_id(started)
    result = await started.email_service.send(draft_id, REVIEWER)
    assert result.draft_status is DraftStatus.FAILED
    assert mailchimp_route.call_count == 0
    assert result.delivery.attempts[-1].error_code == "provider_not_configured"


async def test_unexpected_error_during_delivery_marks_unknown(
    started: Container, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft_id = await approved_draft_id(started)

    async def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("crash mid-delivery")

    monkeypatch.setattr(started.provider_service, "deliver", explode)
    result = await started.email_service.send(draft_id, REVIEWER)
    assert result.draft_status is DraftStatus.UNKNOWN
