"""Provider-independent email delivery interface and outcome classification.

Every provider maps its responses onto three outcomes (see
:class:`~email_service.models.email.AttemptOutcome`):

* ``ACCEPTED`` - the provider took responsibility for the message.
* ``REJECTED`` - the provider definitively did **not** accept it. Only this
  outcome may lead to a retry or to failover.
* ``UNKNOWN`` - the request may have been processed (e.g. a read timeout after
  the request was written). Never retried and never failed over automatically.

``send`` never raises for transport or provider errors; it returns a result.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx
from pydantic import BaseModel

from email_service.models.email import AttemptOutcome, EmailContent


@dataclass(frozen=True, slots=True)
class OutboundEmail:
    delivery_id: str
    idempotency_key: str
    message_id: str  # RFC 5322 Message-ID, derived from the delivery id
    content: EmailContent


class ProviderResult(BaseModel):
    provider: str
    outcome: AttemptOutcome
    retryable: bool = False
    provider_message_id: str | None = None
    provider_status: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    recipient_statuses: dict[str, str] = {}

    @classmethod
    def accepted(cls, provider: str, message_id: str | None, status: str) -> ProviderResult:
        return cls(
            provider=provider,
            outcome=AttemptOutcome.ACCEPTED,
            provider_message_id=message_id,
            provider_status=status,
        )


class EmailProvider(ABC):
    name: str

    @property
    @abstractmethod
    def is_configured(self) -> bool:
        """True when credentials are present; an unconfigured provider is never called."""

    @abstractmethod
    async def send(self, email: OutboundEmail) -> ProviderResult: ...


#: Transport errors raised before the request could reach the provider.
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.UnsupportedProtocol)


def classify_transport_error(provider: str, exc: httpx.HTTPError) -> ProviderResult:
    """Map an httpx exception to an outcome, conservatively."""
    if isinstance(exc, _NOT_SENT):
        return ProviderResult(
            provider=provider,
            outcome=AttemptOutcome.REJECTED,
            retryable=True,
            error_code="connection_failed",
            error_message=f"{type(exc).__name__}: request did not reach the provider",
        )
    # ReadTimeout, WriteTimeout, RemoteProtocolError, ReadError, WriteError, ...
    return ProviderResult(
        provider=provider,
        outcome=AttemptOutcome.UNKNOWN,
        error_code="outcome_unknown",
        error_message=f"{type(exc).__name__}: the provider may have accepted the message",
    )


def classify_http_status(status_code: int) -> tuple[AttemptOutcome, bool]:
    """Outcome and retryability for a non-2xx HTTP status.

    429 and 408 are definitive refusals that may succeed later. Other 4xx are
    permanent. 5xx is treated as UNKNOWN: a server error does not prove that
    the message was not queued.
    """
    if status_code in (408, 429):
        return AttemptOutcome.REJECTED, True
    if 400 <= status_code < 500:
        return AttemptOutcome.REJECTED, False
    return AttemptOutcome.UNKNOWN, False


_SECRETISH = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+|(\"?key\"?\s*[:=]\s*\"?)[^\s\",}]+")


def safe_error_text(text: str, limit: int = 300) -> str:
    """Trim provider error text and strip anything that looks like a credential."""
    cleaned = _SECRETISH.sub(lambda m: (m.group(1) or m.group(2) or "") + "[REDACTED]", text)
    return cleaned[:limit]
