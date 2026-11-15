"""Provider selection, retry and safe failover.

Policy (in order, per delivery):

1. Try the primary provider.
2. ``ACCEPTED`` -> done (SENT).
3. ``UNKNOWN`` -> stop immediately (UNKNOWN). Nothing is retried, nothing fails
   over: the primary may have delivered the message.
4. ``REJECTED`` and retryable (connection refused, 429, 408) -> retry the same
   provider with exponential backoff, up to ``max_retries``.
5. ``REJECTED`` definitively -> if failover is enabled, try the secondary
   provider under the same rules; otherwise FAILED.

Every attempt is passed to ``record_attempt`` *before* the next step, so the
attempt log is durable even if the process dies mid-delivery.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from email_service.models.email import AttemptOutcome, DeliveryAttempt, DeliveryStatus
from email_service.providers.base import EmailProvider, OutboundEmail, ProviderResult

logger = logging.getLogger(__name__)

RecordAttempt = Callable[[DeliveryAttempt], Awaitable[None]]
Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_retries: int = 2
    backoff_seconds: float = 1.0
    max_backoff_seconds: float = 30.0

    def delay(self, retry_number: int) -> float:
        return float(
            min(self.backoff_seconds * (2 ** (retry_number - 1)), self.max_backoff_seconds)
        )


@dataclass(slots=True)
class DeliveryOutcome:
    status: DeliveryStatus
    provider: str | None = None
    provider_message_id: str | None = None
    attempts: list[DeliveryAttempt] = field(default_factory=list)


class ProviderService:
    def __init__(
        self,
        providers: Mapping[str, EmailProvider],
        *,
        primary: str,
        secondary: str | None,
        failover_enabled: bool,
        retry: RetryPolicy,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = lambda: datetime.now(UTC),
    ) -> None:
        if primary not in providers:
            raise ValueError(f"primary provider '{primary}' is not registered")
        self._providers = providers
        self.primary = primary
        self.secondary = secondary
        self.failover_enabled = failover_enabled
        self._retry = retry
        self._sleep = sleep
        self._now = clock

    def chain(self) -> list[str]:
        chain = [self.primary]
        if self.failover_enabled and self.secondary and self.secondary != self.primary:
            chain.append(self.secondary)
        return chain

    def status(self) -> dict[str, dict[str, object]]:
        return {
            name: {
                "configured": provider.is_configured,
                "role": "primary" if name == self.primary else "secondary",
            }
            for name, provider in self._providers.items()
            if name in (self.primary, self.secondary)
        }

    async def deliver(self, email: OutboundEmail, record_attempt: RecordAttempt) -> DeliveryOutcome:
        outcome = DeliveryOutcome(status=DeliveryStatus.FAILED)
        attempt_number = 0
        for name in self.chain():
            provider = self._providers.get(name)
            if provider is None or not provider.is_configured:
                attempt_number += 1
                started = self._now()
                attempt = DeliveryAttempt(
                    provider=name,
                    attempt=attempt_number,
                    outcome=AttemptOutcome.REJECTED,
                    error_code="provider_not_configured",
                    error_message="provider is not configured; it was not contacted",
                    started_at=started,
                    finished_at=started,
                )
                await record_attempt(attempt)
                outcome.attempts.append(attempt)
                continue

            for try_number in range(1, self._retry.max_retries + 2):
                attempt_number += 1
                started = self._now()
                result = await self._safe_send(provider, email)
                attempt = DeliveryAttempt(
                    provider=name,
                    attempt=attempt_number,
                    outcome=result.outcome,
                    retryable=result.retryable,
                    provider_message_id=result.provider_message_id,
                    provider_status=result.provider_status,
                    error_code=result.error_code,
                    error_message=result.error_message,
                    started_at=started,
                    finished_at=self._now(),
                )
                await record_attempt(attempt)
                outcome.attempts.append(attempt)
                logger.info(
                    "delivery attempt",
                    extra={
                        "delivery_id": email.delivery_id,
                        "provider": name,
                        "outcome": result.outcome.value,
                        "error_code": result.error_code,
                    },
                )
                if result.outcome is AttemptOutcome.ACCEPTED:
                    outcome.status = DeliveryStatus.SENT
                    outcome.provider = name
                    outcome.provider_message_id = result.provider_message_id
                    return outcome
                if result.outcome is AttemptOutcome.UNKNOWN:
                    outcome.status = DeliveryStatus.UNKNOWN
                    outcome.provider = name
                    return outcome
                if result.retryable and try_number <= self._retry.max_retries:
                    await self._sleep(self._retry.delay(try_number))
                    continue
                break  # definitive rejection for this provider -> next in chain
        return outcome

    async def _safe_send(self, provider: EmailProvider, email: OutboundEmail) -> ProviderResult:
        try:
            return await provider.send(email)
        except Exception as exc:
            logger.exception("provider raised unexpectedly", extra={"provider": provider.name})
            return ProviderResult(
                provider=provider.name,
                outcome=AttemptOutcome.UNKNOWN,
                error_code="provider_exception",
                error_message=type(exc).__name__,
            )
