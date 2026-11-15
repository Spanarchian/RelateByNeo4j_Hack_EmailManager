"""HTTP API: authentication, validation, structured errors, idempotency, OpenAPI."""

from __future__ import annotations

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from email_service.config import Settings
from email_service.dependencies import build_container
from email_service.main import create_app
from tests.conftest import api_headers, volunteer_payload

pytestmark = pytest.mark.integration

EMAIL = {
    "title": "Welcome to our organisation",
    "body": "Welcome to our team. Your coordinator will be in touch with more details soon.",
    "html_body": "<h1>Welcome</h1>",
    "sender": "sender@example.org",
    "recipients": ["person1@example.com", "person2@example.com"],
}


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["graph"] == {"engine": "relate", "status": "ok"}
    assert body["providers"]["gmail"] == {"configured": True, "role": "primary"}
    assert "key" not in response.text.lower().replace("keys", "")


def test_authentication_required(client: TestClient) -> None:
    response = client.get("/api/v1/emails/pending-review")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"
    assert response.headers["WWW-Authenticate"] == "APIKey"
    bad = client.get("/api/v1/emails/pending-review", headers={"X-API-Key": "nope"})
    assert bad.status_code == 401


def test_submit_email_creates_reviewed_draft_and_does_not_send(
    client: TestClient, mock_http: respx.MockRouter
) -> None:
    response = client.post("/api/v1/emails", json=EMAIL, headers=api_headers("submitter"))
    assert response.status_code == 201, response.text
    draft = response.json()
    assert draft["status"] == "PENDING_REVIEW"
    assert draft["content"]["recipients"] == EMAIL["recipients"]
    assert draft["review"]["content_hash"] == draft["content_hash"]
    assert len(mock_http.calls) == 0


def test_submit_is_idempotent_with_key(client: TestClient) -> None:
    headers = api_headers("submitter", **{"Idempotency-Key": "hr-import-42"})
    first = client.post("/api/v1/emails", json=EMAIL, headers=headers)
    second = client.post("/api/v1/emails", json=EMAIL, headers=headers)
    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json()["id"] == second.json()["id"]
    changed = client.post("/api/v1/emails", json={**EMAIL, "title": "Other"}, headers=headers)
    assert changed.status_code == 409
    # The same key from another client is a different draft.
    other = client.post(
        "/api/v1/emails",
        json=EMAIL,
        headers=api_headers("service", **{"Idempotency-Key": "hr-import-42"}),
    )
    assert other.status_code == 201 and other.json()["id"] != first.json()["id"]


@pytest.mark.parametrize(
    "patch",
    [
        {"recipients": ["not-an-email"]},
        {"recipients": []},
        {"sender": "bad"},
        {"title": "Hi\r\nBcc: x@example.org"},
        {"body": ""},
    ],
)
def test_validation_errors_are_structured(client: TestClient, patch: dict[str, object]) -> None:
    response = client.post(
        "/api/v1/emails", json={**EMAIL, **patch}, headers=api_headers("submitter")
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_failed"
    assert error["details"]["errors"]
    assert "input" not in str(error["details"]["errors"][0])


def test_configured_recipient_limit(client: TestClient) -> None:
    recipients = [f"p{i}@example.org" for i in range(6)]  # limit is 5 in tests
    response = client.post(
        "/api/v1/emails", json={**EMAIL, "recipients": recipients}, headers=api_headers("submitter")
    )
    assert response.status_code == 422
    assert "at most 5" in response.json()["error"]["message"]


def test_bad_idempotency_key(client: TestClient) -> None:
    response = client.post(
        "/api/v1/emails", json=EMAIL, headers=api_headers("submitter", **{"Idempotency-Key": "a b"})
    )
    assert response.status_code == 422


def test_reviewer_cannot_submit_and_submitter_cannot_send(client: TestClient) -> None:
    assert (
        client.post("/api/v1/emails", json=EMAIL, headers=api_headers("reviewer")).status_code
        == 403
    )
    draft = client.post("/api/v1/emails", json=EMAIL, headers=api_headers("submitter")).json()
    response = client.post(f"/api/v1/emails/{draft['id']}/send", headers=api_headers("submitter"))
    assert response.status_code == 403
    assert response.json()["error"]["details"]["permission"] == "send"


def test_not_found(client: TestClient) -> None:
    for url in (
        "/api/v1/emails/draft-x",
        "/api/v1/onboarding/onb-x",
        "/api/v1/onboarding/onb-x/history",
    ):
        response = client.get(url, headers=api_headers("reviewer"))
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"
    response = client.post("/api/v1/onboarding/onb-x/draft", headers=api_headers("submitter"))
    assert response.status_code == 404


def test_onboarding_validation(client: TestClient) -> None:
    response = client.post(
        "/api/v1/onboarding",
        json=volunteer_payload(email="nope", start_date="tomorrow"),
        headers=api_headers("submitter"),
    )
    assert response.status_code == 422
    locations = {tuple(e["loc"][-1:]) for e in response.json()["error"]["details"]["errors"]}
    assert {("email",), ("start_date",)} <= locations


def test_edit_without_change_is_a_no_op(client: TestClient) -> None:
    draft = client.post("/api/v1/emails", json=EMAIL, headers=api_headers("submitter")).json()
    response = client.patch(
        f"/api/v1/emails/{draft['id']}",
        json={"title": EMAIL["title"]},
        headers=api_headers("submitter"),
    )
    assert response.json()["version"] == 1


def test_cancelled_drafts_cannot_be_cancelled_twice(client: TestClient) -> None:
    draft = client.post("/api/v1/emails", json=EMAIL, headers=api_headers("submitter")).json()
    url = f"/api/v1/emails/{draft['id']}/cancel"
    assert client.post(url, headers=api_headers("submitter")).status_code == 200
    second = client.post(url, headers=api_headers("submitter"))
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "invalid_state"


def test_draft_needs_default_sender(settings: Settings) -> None:
    no_sender = settings.model_copy(update={"email_default_sender": None})
    container = build_container(no_sender, http=httpx.AsyncClient())
    with TestClient(create_app(no_sender, container)) as client:
        created = client.post(
            "/api/v1/onboarding", json=volunteer_payload(), headers=api_headers("submitter")
        )
        response = client.post(
            f"/api/v1/onboarding/{created.json()['onboarding']['id']}/draft",
            headers=api_headers("submitter"),
        )
    assert response.status_code == 422
    assert "EMAIL_DEFAULT_SENDER" in response.json()["error"]["message"]


def test_rate_limit(settings: Settings) -> None:
    limited = settings.model_copy(update={"rate_limit_per_minute": 2})
    container = build_container(limited, http=httpx.AsyncClient())
    with TestClient(create_app(limited, container)) as client:
        codes = [
            client.get("/api/v1/emails/pending-review", headers=api_headers("reviewer")).status_code
            for _ in range(3)
        ]
        assert codes == [200, 200, 429]
        assert client.get("/health").status_code == 200  # exempt


def test_openapi_documents_every_endpoint(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    paths = spec["paths"]
    expected = {
        ("/health", "get"),
        ("/api/v1/emails", "post"),
        ("/api/v1/onboarding", "post"),
        ("/api/v1/onboarding/{onboarding_id}", "get"),
        ("/api/v1/onboarding/{onboarding_id}/draft", "post"),
        ("/api/v1/emails/pending-review", "get"),
        ("/api/v1/emails/{draft_id}", "get"),
        ("/api/v1/emails/{draft_id}/approve", "post"),
        ("/api/v1/emails/{draft_id}/reject", "post"),
        ("/api/v1/emails/{draft_id}/send", "post"),
        ("/api/v1/onboarding/{onboarding_id}/history", "get"),
    }
    assert expected <= {(path, method) for path, ops in paths.items() for method in ops}
    assert "APIKeyHeader" in spec["components"]["securitySchemes"]
    assert client.get("/docs").status_code == 200
