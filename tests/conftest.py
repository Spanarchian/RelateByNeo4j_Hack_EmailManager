"""Shared fixtures.

Graph tests run against the real Relate engine (``:memory:`` or temporary
directory stores). External HTTP (Gmail, Mailchimp) is mocked with respx;
real provider calls live in ``tests/e2e/test_external.py`` and are opt-in.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from pydantic import SecretStr

from email_service.agents.base import ALL_AGENTS
from email_service.config import ApiKeyEntry, Settings
from email_service.dependencies import Container, build_container
from email_service.graph.database import RelateDatabase
from email_service.graph.repositories import GraphRepository
from email_service.main import create_app
from email_service.onboarding.templates import DEFAULT_TEMPLATES
from email_service.providers.gmail import GmailProvider
from email_service.providers.mailchimp import MailchimpTransactionalProvider
from email_service.security.authentication import hash_api_key
from email_service.security.permissions import PrincipalKind, Role

FIXED_NOW = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
GMAIL_BASE = "https://gmail.test"
GMAIL_SEND_URL = f"{GMAIL_BASE}/gmail/v1/users/me/messages/send"
MAILCHIMP_BASE = "https://mandrill.test/api/1.0"
MAILCHIMP_SEND_URL = f"{MAILCHIMP_BASE}/messages/send.json"
SENDER = "onboarding@riverside.example.org"
MAILCHIMP_TEST_KEY = "md-test-key-0123456789"

KEYS = {
    "submitter": "test-key-submitter",
    "reviewer": "test-key-reviewer",
    "reviewer2": "test-key-reviewer-two",
    "service": "test-key-service",
    "admin": "test-key-admin",
}


def api_headers(who: str, **extra: str) -> dict[str, str]:
    return {"X-API-Key": KEYS[who], **extra}


class Clock:
    def __init__(self, now: datetime = FIXED_NOW) -> None:
        self.now = now
        self._ticks = 0

    def __call__(self) -> datetime:
        # Advance a microsecond per call so event ordering is deterministic.
        self._ticks += 1
        from datetime import timedelta

        return self.now + timedelta(microseconds=self._ticks)


class StaticTokenSource:
    def __init__(self, available: bool = True) -> None:
        self.available = available

    def is_available(self) -> bool:
        return self.available

    async def get_access_token(self) -> str:
        return "test-access-token"


async def _no_sleep(_: float) -> None:
    return None


def volunteer_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "volunteer",
        "name": "Alex Morgan",
        "email": "alex.morgan@example.org",
        "organisation": "Riverside Community Food Bank",
        "volunteer_role": "Warehouse Volunteer",
        "coordinator": "Priya Shah",
        "start_date": "2026-12-07",
        "induction_details": "Saturday 5 December 2026, 10:00-12:30 at the Riverside warehouse.",
        "team": "Saturday Warehouse",
    }
    payload.update(overrides)
    return payload


def employee_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "employee",
        "name": "Jordan Lee",
        "email": "jordan.lee@example.com",
        "organisation": "Northwind Analytics Ltd",
        "job_title": "Data Analyst",
        "department": "Insights",
        "manager": "Sam Carter",
        "start_date": "2027-01-04",
        "first_day_arrangements": "Arrive at 09:30 at reception, 4th floor. Bring photo ID.",
    }
    payload.update(overrides)
    return payload


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's environment (and .env) out of tests."""
    for key in list(os.environ):
        if key.startswith(("EMAIL_", "GMAIL_", "MAILCHIMP_", "RELATE_", "API_KEYS", "APP_ENV")):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def settings(tmp_path: Any) -> Settings:
    entries = [
        ApiKeyEntry(
            name="sam.submitter",
            key_sha256=hash_api_key(KEYS["submitter"]),
            roles=frozenset({Role.SUBMITTER}),
        ),
        ApiKeyEntry(
            name="riley.reviewer",
            key_sha256=hash_api_key(KEYS["reviewer"]),
            roles=frozenset({Role.REVIEWER}),
        ),
        ApiKeyEntry(
            name="casey.reviewer",
            key_sha256=hash_api_key(KEYS["reviewer2"]),
            roles=frozenset({Role.REVIEWER, Role.SUBMITTER}),
        ),
        ApiKeyEntry(
            name="hr-system",
            key_sha256=hash_api_key(KEYS["service"]),
            roles=frozenset({Role.SUBMITTER}),
            kind=PrincipalKind.SERVICE,
        ),
        ApiKeyEntry(
            name="ada.admin", key_sha256=hash_api_key(KEYS["admin"]), roles=frozenset({Role.ADMIN})
        ),
    ]
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="test",
        relate_store_path=":memory:",
        api_keys=entries,
        rate_limit_per_minute=0,
        email_default_sender=SENDER,
        email_allowed_senders=[SENDER, "sender@example.org"],
        email_max_recipients=5,
        provider_retry_backoff_seconds=0,
        provider_max_retries=1,
        gmail_token_file=tmp_path / "token.json",
        gmail_api_base_url=GMAIL_BASE,
        mailchimp_api_base_url=MAILCHIMP_BASE,
        mailchimp_transactional_api_key=SecretStr(MAILCHIMP_TEST_KEY),
    )


@pytest.fixture
def mock_http() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        router.route(host="testserver").pass_through()
        yield router


@pytest.fixture
def gmail_route(mock_http: respx.MockRouter) -> respx.Route:
    return mock_http.post(GMAIL_SEND_URL)


@pytest.fixture
def mailchimp_route(mock_http: respx.MockRouter) -> respx.Route:
    return mock_http.post(MAILCHIMP_SEND_URL)


@pytest.fixture
def container(settings: Settings, clock: Clock, mock_http: respx.MockRouter) -> Container:
    http = httpx.AsyncClient()
    providers = {
        "gmail": GmailProvider(StaticTokenSource(), http, base_url=GMAIL_BASE, timeout=5),
        "mailchimp": MailchimpTransactionalProvider(
            SecretStr(MAILCHIMP_TEST_KEY), http, base_url=MAILCHIMP_BASE, timeout=5
        ),
    }
    return build_container(settings, providers=providers, http=http, clock=clock, sleep=_no_sleep)


@pytest.fixture
def client(settings: Settings, container: Container) -> Iterator[TestClient]:
    with TestClient(create_app(settings, container)) as test_client:
        yield test_client


@pytest.fixture
async def repository(clock: Clock) -> AsyncIterator[GraphRepository]:
    db = RelateDatabase(":memory:")
    await db.open()
    repo = GraphRepository(db, clock)
    await repo.ensure_schema()
    await repo.seed_reference_data(
        DEFAULT_TEMPLATES,
        [(a.id, a.name, a.kind, a.description) for a in ALL_AGENTS],
        [("gmail", "primary", True), ("mailchimp", "secondary", True)],
    )
    try:
        yield repo
    finally:
        await db.close()


def gmail_ok(message_id: str = "18c0ffee0001") -> httpx.Response:
    return httpx.Response(200, json={"id": message_id, "threadId": "t1", "labelIds": ["SENT"]})


def mailchimp_ok(*emails: str, status: str = "sent") -> httpx.Response:
    return httpx.Response(
        200,
        json=[
            {"email": e, "status": status, "_id": f"md-{i}", "reject_reason": None}
            for i, e in enumerate(emails)
        ],
    )


START_DATE = date(2026, 12, 7)
