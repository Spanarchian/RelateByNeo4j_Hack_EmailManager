"""Real provider calls. Opt-in only; never run in normal CI.

    RUN_EXTERNAL_TESTS=1 EXTERNAL_TEST_RECIPIENT=you@example.com \
    EXTERNAL_TEST_SENDER=you@gmail.com uv run pytest -m external

Gmail needs a provisioned ``GMAIL_TOKEN_FILE``; Mailchimp needs
``MAILCHIMP_TRANSACTIONAL_API_KEY`` and a verified sending domain.
Each test sends one real email.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from email_service.models.email import AttemptOutcome, EmailContent
from email_service.providers.base import OutboundEmail
from email_service.providers.gmail import GmailCredentialStore, GmailProvider
from email_service.providers.mailchimp import MailchimpTransactionalProvider

pytestmark = [
    pytest.mark.external,
    pytest.mark.providers,
    pytest.mark.skipif(
        os.environ.get("RUN_EXTERNAL_TESTS") != "1", reason="set RUN_EXTERNAL_TESTS=1 to run"
    ),
]


def email(sender: str) -> OutboundEmail:
    recipient = os.environ.get("EXTERNAL_TEST_RECIPIENT")
    if not recipient:
        pytest.skip("EXTERNAL_TEST_RECIPIENT is not set")
    delivery = f"dlv-external-{uuid.uuid4()}"
    return OutboundEmail(
        delivery_id=delivery,
        idempotency_key=f"{delivery}#1",
        message_id=f"<{delivery}@onboarding.invalid>",
        content=EmailContent(
            sender=sender,
            recipients=[recipient],
            subject="Gmail Onboarding Assistant external test",
            body="This is an automated test message from the external provider tests.",
            html_body="<p>This is an automated test message from the external provider tests.</p>",
        ),
    )


async def test_real_gmail_send() -> None:
    token = Path(os.environ.get("GMAIL_TOKEN_FILE", "secrets/token.json"))
    sender = os.environ.get("EXTERNAL_TEST_SENDER")
    if not token.is_file() or not sender:
        pytest.skip("GMAIL_TOKEN_FILE and EXTERNAL_TEST_SENDER are required")
    async with httpx.AsyncClient() as client:
        result = await GmailProvider(GmailCredentialStore(token), client).send(email(sender))
    assert result.outcome is AttemptOutcome.ACCEPTED, result
    assert result.provider_message_id


async def test_real_mailchimp_send() -> None:
    key = os.environ.get("MAILCHIMP_TRANSACTIONAL_API_KEY")
    sender = os.environ.get("EXTERNAL_TEST_MAILCHIMP_SENDER") or os.environ.get(
        "EXTERNAL_TEST_SENDER"
    )
    if not key or not sender:
        pytest.skip("MAILCHIMP_TRANSACTIONAL_API_KEY and a sender are required")
    async with httpx.AsyncClient() as client:
        result = await MailchimpTransactionalProvider(SecretStr(key), client).send(email(sender))
    assert result.outcome is AttemptOutcome.ACCEPTED, result
