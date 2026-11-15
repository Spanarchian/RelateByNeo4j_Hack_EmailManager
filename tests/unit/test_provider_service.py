"""Provider selection, retry and failover policy with scripted providers."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from email_service.models.email import AttemptOutcome, DeliveryAttempt, DeliveryStatus, EmailContent
from email_service.providers.base import EmailProvider, OutboundEmail, ProviderResult
from email_service.services.provider_service import ProviderService, RetryPolicy

pytestmark = [pytest.mark.unit, pytest.mark.providers]

EMAIL = OutboundEmail(
    delivery_id="dlv-1",
    idempotency_key="d#1",
    message_id="<dlv-1@x.invalid>",
    content=EmailContent(
        sender="s@example.org", recipients=["r@example.org"], subject="S", body="B"
    ),
)


def result(name: str, outcome: AttemptOutcome, retryable: bool = False) -> ProviderResult:
    return ProviderResult(
        provider=name,
        outcome=outcome,
        retryable=retryable,
        provider_message_id="mid" if outcome is AttemptOutcome.ACCEPTED else None,
    )


class Scripted(EmailProvider):
    def __init__(
        self, name: str, script: Sequence[ProviderResult | Exception], configured: bool = True
    ):
        self.name = name
        self.script = list(script)
        self.calls = 0
        self.configured = configured

    @property
    def is_configured(self) -> bool:
        return self.configured

    async def send(self, email: OutboundEmail) -> ProviderResult:
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


A, R, U = AttemptOutcome.ACCEPTED, AttemptOutcome.REJECTED, AttemptOutcome.UNKNOWN


def service(
    gmail: Scripted, mailchimp: Scripted, *, failover: bool = True, retries: int = 2
) -> tuple[ProviderService, list[float]]:
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    svc = ProviderService(
        {"gmail": gmail, "mailchimp": mailchimp},
        primary="gmail",
        secondary="mailchimp",
        failover_enabled=failover,
        retry=RetryPolicy(max_retries=retries, backoff_seconds=1.0),
        sleep=sleep,
    )
    return svc, sleeps


async def deliver(
    svc: ProviderService,
) -> tuple[DeliveryStatus, list[DeliveryAttempt], list[DeliveryAttempt]]:
    recorded: list[DeliveryAttempt] = []

    async def record(attempt: DeliveryAttempt) -> None:
        recorded.append(attempt)

    outcome = await svc.deliver(EMAIL, record)
    return outcome.status, outcome.attempts, recorded


async def test_primary_accepts() -> None:
    gmail, mc = Scripted("gmail", [result("gmail", A)]), Scripted("mailchimp", [])
    status, attempts, recorded = await deliver(service(gmail, mc)[0])
    assert status is DeliveryStatus.SENT
    assert attempts == recorded and len(attempts) == 1
    assert mc.calls == 0


async def test_definitive_primary_failure_fails_over() -> None:
    gmail = Scripted("gmail", [result("gmail", R)])
    mc = Scripted("mailchimp", [result("mailchimp", A)])
    svc, _ = service(gmail, mc)
    outcome = await svc.deliver(EMAIL, _noop)
    assert outcome.status is DeliveryStatus.SENT
    assert outcome.provider == "mailchimp"
    assert [(a.provider, a.outcome, a.attempt) for a in outcome.attempts] == [
        ("gmail", R, 1),
        ("mailchimp", A, 2),
    ]


async def test_unknown_primary_outcome_never_fails_over_or_retries() -> None:
    gmail = Scripted("gmail", [result("gmail", U)])
    mc = Scripted("mailchimp", [result("mailchimp", A)])
    status, attempts, _ = await deliver(service(gmail, mc)[0])
    assert status is DeliveryStatus.UNKNOWN
    assert gmail.calls == 1 and mc.calls == 0


async def test_transient_errors_are_retried_with_backoff() -> None:
    gmail = Scripted(
        "gmail", [result("gmail", R, True), result("gmail", R, True), result("gmail", A)]
    )
    svc, sleeps = service(gmail, Scripted("mailchimp", []))
    status, attempts, _ = await deliver(svc)
    assert status is DeliveryStatus.SENT
    assert gmail.calls == 3
    assert sleeps == [1.0, 2.0]


async def test_retries_exhausted_then_failover() -> None:
    gmail = Scripted("gmail", [result("gmail", R, True)] * 3)
    mc = Scripted("mailchimp", [result("mailchimp", A)])
    svc, sleeps = service(gmail, mc, retries=2)
    status, attempts, _ = await deliver(svc)
    assert status is DeliveryStatus.SENT
    assert gmail.calls == 3 and mc.calls == 1
    assert len(sleeps) == 2


async def test_failover_disabled() -> None:
    gmail = Scripted("gmail", [result("gmail", R)])
    mc = Scripted("mailchimp", [result("mailchimp", A)])
    status, _, _ = await deliver(service(gmail, mc, failover=False)[0])
    assert status is DeliveryStatus.FAILED
    assert mc.calls == 0


async def test_unconfigured_provider_is_skipped_and_recorded() -> None:
    gmail = Scripted("gmail", [], configured=False)
    mc = Scripted("mailchimp", [result("mailchimp", A)])
    status, attempts, recorded = await deliver(service(gmail, mc)[0])
    assert status is DeliveryStatus.SENT
    assert gmail.calls == 0
    assert recorded[0].error_code == "provider_not_configured"


async def test_both_fail() -> None:
    gmail = Scripted("gmail", [result("gmail", R)])
    mc = Scripted("mailchimp", [result("mailchimp", R)])
    status, attempts, _ = await deliver(service(gmail, mc)[0])
    assert status is DeliveryStatus.FAILED and len(attempts) == 2


async def test_secondary_unknown_is_unknown() -> None:
    gmail = Scripted("gmail", [result("gmail", R)])
    mc = Scripted("mailchimp", [result("mailchimp", U)])
    status, _, _ = await deliver(service(gmail, mc)[0])
    assert status is DeliveryStatus.UNKNOWN


async def test_unexpected_exception_is_treated_as_unknown() -> None:
    gmail = Scripted("gmail", [RuntimeError("bug")])
    mc = Scripted("mailchimp", [result("mailchimp", A)])
    status, attempts, _ = await deliver(service(gmail, mc)[0])
    assert status is DeliveryStatus.UNKNOWN
    assert attempts[0].error_code == "provider_exception"
    assert mc.calls == 0


def test_configuration() -> None:
    gmail, mc = Scripted("gmail", []), Scripted("mailchimp", [], configured=False)
    svc, _ = service(gmail, mc)
    assert svc.chain() == ["gmail", "mailchimp"]
    assert svc.status() == {
        "gmail": {"configured": True, "role": "primary"},
        "mailchimp": {"configured": False, "role": "secondary"},
    }
    assert service(gmail, mc, failover=False)[0].chain() == ["gmail"]
    with pytest.raises(ValueError, match="not registered"):
        ProviderService(
            {}, primary="gmail", secondary=None, failover_enabled=True, retry=RetryPolicy()
        )
    assert RetryPolicy(backoff_seconds=10, max_backoff_seconds=15).delay(3) == 15


async def _noop(_: DeliveryAttempt) -> None:
    return None
