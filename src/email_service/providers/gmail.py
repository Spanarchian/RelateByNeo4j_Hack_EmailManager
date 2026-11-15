"""Gmail API provider (primary).

Uses the official Gmail REST API endpoint ``users.messages.send``
(``POST https://gmail.googleapis.com/gmail/v1/users/me/messages/send`` with a
base64url-encoded RFC 5322 message in ``raw``) and OAuth 2.0 user credentials
from ``google-auth`` with the single scope ``gmail.send``.

The Gmail API offers no idempotency key, so duplicate prevention is done by
the application (see ``ProviderService`` and ``GraphRepository.claim_for_sending``).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import tempfile
import threading
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate
from pathlib import Path
from typing import Protocol

import httpx

from email_service.exceptions import CredentialsUnavailableError
from email_service.models.email import AttemptOutcome
from email_service.providers.base import (
    EmailProvider,
    OutboundEmail,
    ProviderResult,
    classify_http_status,
    classify_transport_error,
    safe_error_text,
)

logger = logging.getLogger(__name__)

GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
SCOPES = [GMAIL_SEND_SCOPE]
DELIVERY_HEADER = "X-Onboarding-Delivery-ID"


def build_mime_message(email: OutboundEmail) -> EmailMessage:
    """RFC 5322 message: text/plain alone, or multipart/alternative (plain + HTML)."""
    content = email.content
    message = EmailMessage(policy=SMTP)
    message["From"] = content.sender
    message["To"] = ", ".join(content.recipients)
    message["Subject"] = content.subject
    message["Date"] = formatdate(usegmt=True)
    message["Message-ID"] = email.message_id
    message[DELIVERY_HEADER] = email.delivery_id
    message.set_content(content.body, subtype="plain", charset="utf-8")
    if content.html_body:
        message.add_alternative(content.html_body, subtype="html", charset="utf-8")
    return message


def encode_raw(message: EmailMessage) -> str:
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


class AccessTokenSource(Protocol):
    def is_available(self) -> bool: ...

    async def get_access_token(self) -> str: ...


class GmailCredentialStore:
    """Loads, refreshes and persists the OAuth token file (``token.json``).

    The token file is produced once by ``uv run task auth`` and then mounted
    read-write (it is refreshed in place) into the container.
    """

    def __init__(self, token_file: Path) -> None:
        self.token_file = token_file
        self._lock = threading.Lock()
        self._credentials: object | None = None

    def is_available(self) -> bool:
        return self.token_file.is_file()

    def _load_and_refresh(self) -> str:
        from google.auth.exceptions import GoogleAuthError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        with self._lock:
            creds = self._credentials
            if creds is None:
                if not self.token_file.is_file():
                    raise CredentialsUnavailableError("Gmail token file not found; run 'task auth'")
                try:
                    creds = Credentials.from_authorized_user_file(str(self.token_file), SCOPES)
                except (ValueError, json.JSONDecodeError) as exc:
                    raise CredentialsUnavailableError("Gmail token file is invalid") from exc
            assert isinstance(creds, Credentials)
            if not creds.valid:
                if not creds.refresh_token:
                    raise CredentialsUnavailableError("Gmail token has no refresh token")
                try:
                    creds.refresh(Request())
                except GoogleAuthError as exc:
                    raise CredentialsUnavailableError(
                        "Gmail token refresh failed; re-run 'task auth'"
                    ) from exc
                try:
                    save_token_file(self.token_file, creds.to_json())
                except OSError:
                    # Read-only mount: keep the refreshed token in memory only.
                    logger.warning("gmail token refreshed but could not be persisted")
                else:
                    logger.info("gmail access token refreshed")
            self._credentials = creds
            token = creds.token
            if not token:
                raise CredentialsUnavailableError("Gmail credentials have no access token")
            return str(token)

    async def get_access_token(self) -> str:
        return await asyncio.to_thread(self._load_and_refresh)


def save_token_file(path: Path, token_json: str) -> None:
    """Atomic write with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".token-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token_json)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class GmailProvider(EmailProvider):
    name = "gmail"

    def __init__(
        self,
        token_source: AccessTokenSource,
        client: httpx.AsyncClient,
        *,
        base_url: str = "https://gmail.googleapis.com",
        timeout: float = 15.0,
    ) -> None:
        self._tokens = token_source
        self._client = client
        self._url = f"{base_url.rstrip('/')}/gmail/v1/users/me/messages/send"
        self._timeout = timeout

    @property
    def is_configured(self) -> bool:
        return self._tokens.is_available()

    async def send(self, email: OutboundEmail) -> ProviderResult:
        try:
            token = await self._tokens.get_access_token()
        except CredentialsUnavailableError as exc:
            return ProviderResult(
                provider=self.name,
                outcome=AttemptOutcome.REJECTED,
                error_code="credentials_unavailable",
                error_message=exc.message,
            )
        raw = encode_raw(build_mime_message(email))
        try:
            response = await self._client.post(
                self._url,
                json={"raw": raw},
                headers={"Authorization": f"Bearer {token}"},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            return classify_transport_error(self.name, exc)

        if response.is_success:
            try:
                payload = response.json()
                message_id = str(payload["id"])
            except ValueError, KeyError, TypeError:
                # 2xx means Gmail accepted the message, even if the body is odd.
                return ProviderResult.accepted(self.name, None, "accepted-unparsed")
            labels = payload.get("labelIds") or []
            return ProviderResult.accepted(self.name, message_id, ",".join(labels) or "sent")

        outcome, retryable = classify_http_status(response.status_code)
        reason, message = _google_error(response)
        if response.status_code == 401:
            self._invalidate_cached_token()
        return ProviderResult(
            provider=self.name,
            outcome=outcome,
            retryable=retryable,
            provider_status=str(response.status_code),
            error_code=reason or f"http_{response.status_code}",
            error_message=safe_error_text(message),
        )

    def _invalidate_cached_token(self) -> None:
        if isinstance(self._tokens, GmailCredentialStore):
            self._tokens._credentials = None


def _google_error(response: httpx.Response) -> tuple[str | None, str]:
    try:
        error = response.json()["error"]
        reason = (error.get("errors") or [{}])[0].get("reason") or error.get("status")
        return (str(reason) if reason else None), str(error.get("message", ""))
    except ValueError, KeyError, TypeError, AttributeError:
        return None, response.text
