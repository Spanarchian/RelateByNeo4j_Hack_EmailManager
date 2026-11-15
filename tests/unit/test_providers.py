"""Gmail and Mailchimp Transactional adapters against mocked HTTP."""

from __future__ import annotations

import base64
import email
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from email_service.exceptions import CredentialsUnavailableError
from email_service.models.email import AttemptOutcome, EmailContent
from email_service.providers.base import (
    OutboundEmail,
    classify_http_status,
    classify_transport_error,
    safe_error_text,
)
from email_service.providers.gmail import (
    DELIVERY_HEADER,
    GmailCredentialStore,
    GmailProvider,
    build_mime_message,
    encode_raw,
    save_token_file,
)
from email_service.providers.mailchimp import MailchimpTransactionalProvider
from tests.conftest import (
    GMAIL_BASE,
    GMAIL_SEND_URL,
    MAILCHIMP_BASE,
    MAILCHIMP_SEND_URL,
    StaticTokenSource,
)

pytestmark = [pytest.mark.unit, pytest.mark.providers]


def outbound(html: str | None = "<p>Hello <b>Alex</b></p>", recipients: int = 2) -> OutboundEmail:
    return OutboundEmail(
        delivery_id="dlv-1",
        idempotency_key="draft-1#1",
        message_id="<dlv-1@onboarding.invalid>",
        content=EmailContent(
            sender="sender@example.org",
            recipients=[f"person{i}@example.com" for i in range(1, recipients + 1)],
            subject="Welcome – café",
            body="Hello Alex\nSecond line ✓",
            html_body=html,
        ),
    )


# --- MIME ---------------------------------------------------------------------------


class TestMime:
    def test_multipart_alternative_with_plain_and_html(self) -> None:
        message = build_mime_message(outbound())
        assert message.get_content_type() == "multipart/alternative"
        types = [part.get_content_type() for part in message.iter_parts()]
        assert types == ["text/plain", "text/html"]  # plain first: least preferred
        assert message["To"] == "person1@example.com, person2@example.com"
        assert message["Message-ID"] == "<dlv-1@onboarding.invalid>"
        assert message[DELIVERY_HEADER] == "dlv-1"

    def test_plain_only(self) -> None:
        message = build_mime_message(outbound(html=None))
        assert message.get_content_type() == "text/plain"
        assert "Second line ✓" in message.get_content()

    def test_raw_is_base64url_and_round_trips_unicode(self) -> None:
        raw = encode_raw(build_mime_message(outbound()))
        assert "+" not in raw and "/" not in raw
        parsed = email.message_from_bytes(base64.urlsafe_b64decode(raw))
        from email.header import decode_header, make_header

        assert str(make_header(decode_header(parsed["Subject"]))) == "Welcome – café"


# --- classification -----------------------------------------------------------------


class TestClassification:
    @pytest.mark.parametrize(
        ("exc", "outcome", "retryable"),
        [
            (httpx.ConnectError("refused"), AttemptOutcome.REJECTED, True),
            (httpx.ConnectTimeout("slow"), AttemptOutcome.REJECTED, True),
            (httpx.ReadTimeout("slow"), AttemptOutcome.UNKNOWN, False),
            (httpx.WriteTimeout("slow"), AttemptOutcome.UNKNOWN, False),
            (httpx.RemoteProtocolError("eof"), AttemptOutcome.UNKNOWN, False),
        ],
    )
    def test_transport(
        self, exc: httpx.HTTPError, outcome: AttemptOutcome, retryable: bool
    ) -> None:
        result = classify_transport_error("p", exc)
        assert (result.outcome, result.retryable) == (outcome, retryable)

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (400, (AttemptOutcome.REJECTED, False)),
            (403, (AttemptOutcome.REJECTED, False)),
            (408, (AttemptOutcome.REJECTED, True)),
            (429, (AttemptOutcome.REJECTED, True)),
            (500, (AttemptOutcome.UNKNOWN, False)),
            (503, (AttemptOutcome.UNKNOWN, False)),
        ],
    )
    def test_status(self, status: int, expected: tuple[AttemptOutcome, bool]) -> None:
        assert classify_http_status(status) == expected

    def test_safe_error_text_strips_secrets(self) -> None:
        text = safe_error_text('bad "key": "md-secret" and Bearer abc.def')
        assert "md-secret" not in text and "abc.def" not in text


# --- Gmail ----------------------------------------------------------------------------


@pytest.fixture
async def gmail() -> Any:
    async with httpx.AsyncClient() as client:
        yield GmailProvider(StaticTokenSource(), client, base_url=GMAIL_BASE)


@respx.mock
async def test_gmail_success(gmail: GmailProvider) -> None:
    route = respx.post(GMAIL_SEND_URL).mock(
        return_value=httpx.Response(200, json={"id": "abc123", "labelIds": ["SENT"]})
    )
    result = await gmail.send(outbound())
    assert result.outcome is AttemptOutcome.ACCEPTED
    assert result.provider_message_id == "abc123"
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer test-access-token"
    assert set(json.loads(request.content)) == {"raw"}


@respx.mock
async def test_gmail_success_with_unparseable_body_is_still_accepted(gmail: GmailProvider) -> None:
    respx.post(GMAIL_SEND_URL).mock(return_value=httpx.Response(200, text="ok"))
    result = await gmail.send(outbound())
    assert result.outcome is AttemptOutcome.ACCEPTED
    assert result.provider_message_id is None


@respx.mock
@pytest.mark.parametrize(
    ("status", "reason", "outcome", "retryable"),
    [
        (400, "invalidArgument", AttemptOutcome.REJECTED, False),
        (401, "authError", AttemptOutcome.REJECTED, False),
        (429, "rateLimitExceeded", AttemptOutcome.REJECTED, True),
        (500, "backendError", AttemptOutcome.UNKNOWN, False),
    ],
)
async def test_gmail_errors(
    gmail: GmailProvider, status: int, reason: str, outcome: AttemptOutcome, retryable: bool
) -> None:
    respx.post(GMAIL_SEND_URL).mock(
        return_value=httpx.Response(
            status,
            json={"error": {"code": status, "message": "nope", "errors": [{"reason": reason}]}},
        )
    )
    result = await gmail.send(outbound())
    assert (result.outcome, result.retryable, result.error_code) == (outcome, retryable, reason)


@respx.mock
async def test_gmail_non_json_error(gmail: GmailProvider) -> None:
    respx.post(GMAIL_SEND_URL).mock(return_value=httpx.Response(404, text="Not Found"))
    result = await gmail.send(outbound())
    assert result.error_code == "http_404"
    assert result.outcome is AttemptOutcome.REJECTED


@respx.mock
async def test_gmail_timeout_is_unknown(gmail: GmailProvider) -> None:
    respx.post(GMAIL_SEND_URL).mock(side_effect=httpx.ReadTimeout("timeout"))
    assert (await gmail.send(outbound())).outcome is AttemptOutcome.UNKNOWN


async def test_gmail_without_credentials_is_definitive_rejection(tmp_path: Path) -> None:
    async with httpx.AsyncClient() as client:
        provider = GmailProvider(GmailCredentialStore(tmp_path / "missing.json"), client)
        assert provider.is_configured is False
        result = await provider.send(outbound())
    assert result.outcome is AttemptOutcome.REJECTED
    assert result.error_code == "credentials_unavailable"


class TestGmailCredentialStore:
    def token(self, path: Path, expiry: datetime, refresh: str | None = "1//refresh") -> None:
        data: dict[str, Any] = {
            "token": "ya29.current",
            "client_id": "cid.apps.googleusercontent.com",
            "client_secret": "csecret",
            "scopes": ["https://www.googleapis.com/auth/gmail.send"],
            "expiry": expiry.replace(tzinfo=None).isoformat() + "Z",
        }
        if refresh:
            data["refresh_token"] = refresh
        path.write_text(json.dumps(data))

    async def test_valid_token_is_used_without_refresh(self, tmp_path: Path) -> None:
        path = tmp_path / "token.json"
        self.token(path, datetime.now(UTC) + timedelta(hours=1))
        store = GmailCredentialStore(path)
        assert store.is_available()
        assert await store.get_access_token() == "ya29.current"

    async def test_expired_token_is_refreshed_and_persisted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from google.oauth2.credentials import Credentials

        path = tmp_path / "token.json"
        self.token(path, datetime.now(UTC) - timedelta(hours=1))

        def fake_refresh(self: Credentials, request: object) -> None:
            self.token = "ya29.refreshed"
            self.expiry = (datetime.now(UTC) + timedelta(hours=1)).replace(tzinfo=None)

        monkeypatch.setattr(Credentials, "refresh", fake_refresh)
        store = GmailCredentialStore(path)
        assert await store.get_access_token() == "ya29.refreshed"
        assert json.loads(path.read_text())["token"] == "ya29.refreshed"
        assert path.stat().st_mode & 0o777 == 0o600

    async def test_refresh_on_read_only_mount_keeps_token_in_memory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from google.oauth2.credentials import Credentials

        import email_service.providers.gmail as gmail_module

        path = tmp_path / "token.json"
        self.token(path, datetime.now(UTC) - timedelta(hours=1))

        def fake_refresh(self: Credentials, request: object) -> None:
            self.token = "ya29.memory"
            self.expiry = (datetime.now(UTC) + timedelta(hours=1)).replace(tzinfo=None)

        def read_only(*args: object) -> None:
            raise PermissionError("read-only file system")

        monkeypatch.setattr(Credentials, "refresh", fake_refresh)
        monkeypatch.setattr(gmail_module, "save_token_file", read_only)
        assert await GmailCredentialStore(path).get_access_token() == "ya29.memory"

    async def test_refresh_failure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from google.auth.exceptions import RefreshError
        from google.oauth2.credentials import Credentials

        path = tmp_path / "token.json"
        self.token(path, datetime.now(UTC) - timedelta(hours=1))

        def failing(self: Credentials, request: object) -> None:
            raise RefreshError("invalid_grant")

        monkeypatch.setattr(Credentials, "refresh", failing)
        with pytest.raises(CredentialsUnavailableError, match="refresh failed"):
            await GmailCredentialStore(path).get_access_token()

    async def test_no_refresh_token(self, tmp_path: Path) -> None:
        path = tmp_path / "token.json"
        self.token(path, datetime.now(UTC) - timedelta(hours=1), refresh=None)
        with pytest.raises(CredentialsUnavailableError):
            await GmailCredentialStore(path).get_access_token()

    async def test_invalid_file(self, tmp_path: Path) -> None:
        path = tmp_path / "token.json"
        path.write_text("{not json")
        with pytest.raises(CredentialsUnavailableError, match="invalid"):
            await GmailCredentialStore(path).get_access_token()

    def test_save_token_file_is_atomic_and_private(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "token.json"
        save_token_file(target, '{"token": "x"}')
        assert target.read_text() == '{"token": "x"}'
        assert target.stat().st_mode & 0o777 == 0o600
        assert list(target.parent.iterdir()) == [target]


# --- Mailchimp Transactional ----------------------------------------------------------


@pytest.fixture
async def mailchimp() -> Any:
    async with httpx.AsyncClient() as client:
        yield MailchimpTransactionalProvider(
            SecretStr("md-key-123"), client, base_url=MAILCHIMP_BASE, from_name="Riverside"
        )


@respx.mock
async def test_mailchimp_payload(mailchimp: MailchimpTransactionalProvider) -> None:
    route = respx.post(MAILCHIMP_SEND_URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                {"email": "person1@example.com", "status": "sent", "_id": "id1"},
                {"email": "person2@example.com", "status": "queued", "_id": "id2"},
            ],
        )
    )
    result = await mailchimp.send(outbound())
    assert result.outcome is AttemptOutcome.ACCEPTED
    assert result.provider_message_id == "id1,id2"
    assert result.provider_status == "queued,sent"
    payload = json.loads(route.calls.last.request.content)
    assert payload["key"] == "md-key-123"
    message = payload["message"]
    assert message["from_email"] == "sender@example.org"
    assert message["from_name"] == "Riverside"
    assert message["text"].startswith("Hello Alex")
    assert message["html"] == "<p>Hello <b>Alex</b></p>"
    assert message["to"] == [
        {"email": "person1@example.com", "type": "to"},
        {"email": "person2@example.com", "type": "to"},
    ]
    assert message["headers"]["Message-Id"] == "<dlv-1@onboarding.invalid>"
    assert message["metadata"] == {"delivery_id": "dlv-1"}
    assert "md-key-123" not in result.model_dump_json()


@respx.mock
async def test_mailchimp_plain_text_only(mailchimp: MailchimpTransactionalProvider) -> None:
    route = respx.post(MAILCHIMP_SEND_URL).mock(
        return_value=httpx.Response(
            200, json=[{"email": "person1@example.com", "status": "sent", "_id": "a"}]
        )
    )
    await mailchimp.send(outbound(html=None, recipients=1))
    assert "html" not in json.loads(route.calls.last.request.content)["message"]


@respx.mock
async def test_mailchimp_partial_acceptance_is_accepted(
    mailchimp: MailchimpTransactionalProvider,
) -> None:
    respx.post(MAILCHIMP_SEND_URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                {"email": "person1@example.com", "status": "sent", "_id": "a"},
                {
                    "email": "person2@example.com",
                    "status": "rejected",
                    "_id": "b",
                    "reject_reason": "hard-bounce",
                },
            ],
        )
    )
    result = await mailchimp.send(outbound())
    assert result.outcome is AttemptOutcome.ACCEPTED
    assert result.provider_status == "partial"
    assert result.recipient_statuses["person2@example.com"] == "rejected:hard-bounce"


@respx.mock
async def test_mailchimp_all_rejected(mailchimp: MailchimpTransactionalProvider) -> None:
    respx.post(MAILCHIMP_SEND_URL).mock(
        return_value=httpx.Response(
            200, json=[{"email": "person1@example.com", "status": "invalid", "_id": "a"}, "junk"]
        )
    )
    result = await mailchimp.send(outbound(recipients=1))
    assert result.outcome is AttemptOutcome.REJECTED
    assert result.error_code == "all_recipients_rejected"


@respx.mock
@pytest.mark.parametrize(
    ("status", "name", "outcome"),
    [
        (401, "Invalid_Key", AttemptOutcome.REJECTED),  # observed live during implementation
        (500, "Invalid_Key", AttemptOutcome.REJECTED),  # documented historic form
        (500, "ValidationError", AttemptOutcome.REJECTED),
        (500, "PaymentRequired", AttemptOutcome.REJECTED),
        (500, "GeneralError", AttemptOutcome.UNKNOWN),
        (200, "GeneralError", AttemptOutcome.UNKNOWN),
    ],
)
async def test_mailchimp_errors(
    mailchimp: MailchimpTransactionalProvider, status: int, name: str, outcome: AttemptOutcome
) -> None:
    respx.post(MAILCHIMP_SEND_URL).mock(
        return_value=httpx.Response(
            status, json={"status": "error", "code": -1, "name": name, "message": "key md-key-123"}
        )
    )
    result = await mailchimp.send(outbound())
    assert result.outcome is outcome
    assert result.error_code == name


@respx.mock
@pytest.mark.parametrize(
    ("response", "outcome"),
    [
        (httpx.Response(502, text="Bad gateway"), AttemptOutcome.UNKNOWN),
        (httpx.Response(429, text="slow down"), AttemptOutcome.REJECTED),
        (httpx.Response(200, json={"unexpected": True}), AttemptOutcome.UNKNOWN),
    ],
)
async def test_mailchimp_unexpected_responses(
    mailchimp: MailchimpTransactionalProvider, response: httpx.Response, outcome: AttemptOutcome
) -> None:
    respx.post(MAILCHIMP_SEND_URL).mock(return_value=response)
    assert (await mailchimp.send(outbound())).outcome is outcome


@respx.mock
async def test_mailchimp_timeouts(mailchimp: MailchimpTransactionalProvider) -> None:
    route = respx.post(MAILCHIMP_SEND_URL)
    route.mock(side_effect=httpx.ReadTimeout("t"))
    assert (await mailchimp.send(outbound())).outcome is AttemptOutcome.UNKNOWN
    route.mock(side_effect=httpx.ConnectTimeout("t"))
    result = await mailchimp.send(outbound())
    assert result.outcome is AttemptOutcome.REJECTED and result.retryable


async def test_mailchimp_disabled_without_key() -> None:
    async with httpx.AsyncClient() as client:
        provider = MailchimpTransactionalProvider(None, client)
        assert provider.is_configured is False
        result = await provider.send(outbound())
    assert result.error_code == "provider_not_configured"
