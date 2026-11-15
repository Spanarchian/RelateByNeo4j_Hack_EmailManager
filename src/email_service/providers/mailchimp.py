"""Mailchimp Transactional (formerly Mandrill) provider (secondary).

Uses ``POST https://mandrillapp.com/api/1.0/messages/send.json`` with the API
key in the JSON body (``key``). This is the Transactional API, not the
Mailchimp Marketing campaign API.

Response handling:

* Success is a JSON array with one entry per recipient:
  ``{"email", "status", "_id", "reject_reason"}``. ``sent``, ``queued`` and
  ``scheduled`` mean accepted; ``rejected`` and ``invalid`` mean refused.
* Errors are ``{"status": "error", "code", "name", "message"}``. The HTTP
  status is not reliable (``Invalid_Key`` was observed as HTTP 401 during
  implementation; older documentation shows HTTP 500), so the body decides.

The API key is never logged or returned; it only ever appears in the request body.
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import SecretStr

from email_service.models.email import AttemptOutcome
from email_service.providers.base import (
    EmailProvider,
    OutboundEmail,
    ProviderResult,
    classify_http_status,
    classify_transport_error,
    safe_error_text,
)

ACCEPTED_STATUSES = frozenset({"sent", "queued", "scheduled"})
#: Named errors that prove the message was not accepted.
DEFINITIVE_ERRORS = frozenset(
    {"Invalid_Key", "ValidationError", "PaymentRequired", "Unknown_Subaccount", "Unknown_Template"}
)


class MailchimpTransactionalProvider(EmailProvider):
    name = "mailchimp"

    def __init__(
        self,
        api_key: SecretStr | None,
        client: httpx.AsyncClient,
        *,
        base_url: str = "https://mandrillapp.com/api/1.0",
        from_name: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._api_key = api_key
        self._client = client
        self._url = f"{base_url.rstrip('/')}/messages/send.json"
        self._from_name = from_name
        self._timeout = timeout

    @property
    def is_configured(self) -> bool:
        return self._api_key is not None and bool(self._api_key.get_secret_value())

    def build_payload(self, email: OutboundEmail) -> dict[str, Any]:
        content = email.content
        message: dict[str, Any] = {
            "subject": content.subject,
            "from_email": content.sender,
            "to": [{"email": address, "type": "to"} for address in content.recipients],
            "text": content.body,
            "headers": {
                "Message-Id": email.message_id,
                "X-Onboarding-Delivery-ID": email.delivery_id,
            },
            "metadata": {"delivery_id": email.delivery_id},
            "preserve_recipients": True,  # one message to all, like Gmail
            "track_opens": False,
            "track_clicks": False,
            "auto_text": False,
        }
        if content.html_body:
            message["html"] = content.html_body
        if self._from_name:
            message["from_name"] = self._from_name
        assert self._api_key is not None
        return {"key": self._api_key.get_secret_value(), "message": message, "async": False}

    async def send(self, email: OutboundEmail) -> ProviderResult:
        if not self.is_configured:
            return ProviderResult(
                provider=self.name,
                outcome=AttemptOutcome.REJECTED,
                error_code="provider_not_configured",
                error_message="MAILCHIMP_TRANSACTIONAL_API_KEY is not set",
            )
        try:
            response = await self._client.post(
                self._url, json=self.build_payload(email), timeout=self._timeout
            )
        except httpx.HTTPError as exc:
            return classify_transport_error(self.name, exc)

        try:
            body = response.json()
        except ValueError:
            body = None

        if isinstance(body, dict) and body.get("status") == "error":
            return self._error_result(response.status_code, body)
        if response.is_success and isinstance(body, list):
            return self._recipients_result(body)

        outcome, retryable = classify_http_status(response.status_code)
        if response.is_success:  # 2xx with an unexpected body: accepted but unparseable
            outcome, retryable = AttemptOutcome.UNKNOWN, False
        return ProviderResult(
            provider=self.name,
            outcome=outcome,
            retryable=retryable,
            provider_status=str(response.status_code),
            error_code=f"http_{response.status_code}",
            error_message=safe_error_text(response.text),
        )

    def _error_result(self, status_code: int, body: dict[str, Any]) -> ProviderResult:
        name = str(body.get("name", "Unknown"))
        if name in DEFINITIVE_ERRORS:
            outcome, retryable = AttemptOutcome.REJECTED, False
        else:
            outcome, retryable = classify_http_status(status_code)
            if status_code < 400:
                outcome, retryable = AttemptOutcome.UNKNOWN, False
        return ProviderResult(
            provider=self.name,
            outcome=outcome,
            retryable=retryable,
            provider_status="error",
            error_code=name,
            error_message=safe_error_text(str(body.get("message", ""))),
        )

    def _recipients_result(self, entries: list[Any]) -> ProviderResult:
        statuses: dict[str, str] = {}
        ids: list[str] = []
        accepted = 0
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            status = str(entry.get("status", "unknown"))
            reason = entry.get("reject_reason")
            statuses[str(entry.get("email", "?"))] = f"{status}:{reason}" if reason else status
            if status in ACCEPTED_STATUSES:
                accepted += 1
                if entry.get("_id"):
                    ids.append(str(entry["_id"]))
        message_id = ",".join(dict.fromkeys(ids)) or None
        if accepted == 0:
            return ProviderResult(
                provider=self.name,
                outcome=AttemptOutcome.REJECTED,
                provider_status="rejected",
                error_code="all_recipients_rejected",
                error_message="Mailchimp Transactional rejected every recipient",
                recipient_statuses=statuses,
            )
        # Partial acceptance counts as accepted: failing over would duplicate the
        # message for the recipients who already received it.
        status = "partial" if accepted < len(statuses) else _summary(statuses)
        result = ProviderResult.accepted(self.name, message_id, status)
        return result.model_copy(update={"recipient_statuses": statuses})


def _summary(statuses: dict[str, str]) -> str:
    distinct = sorted({s.split(":")[0] for s in statuses.values()})
    return ",".join(distinct)
