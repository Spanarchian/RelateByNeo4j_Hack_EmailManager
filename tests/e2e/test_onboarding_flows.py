"""Complete volunteer and employee onboarding through the HTTP API."""

from __future__ import annotations

import base64
import email
import json
from email.message import Message
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient

from tests.conftest import SENDER, api_headers, employee_payload, gmail_ok, volunteer_payload

pytestmark = [pytest.mark.e2e, pytest.mark.workflow]


def create_onboarding(client: TestClient, payload: dict[str, Any]) -> str:
    response = client.post("/api/v1/onboarding", json=payload, headers=api_headers("submitter"))
    assert response.status_code == 201, response.text
    return str(response.json()["onboarding"]["id"])


def prepare(client: TestClient, onboarding_id: str, template: str) -> dict[str, Any]:
    response = client.post(
        f"/api/v1/onboarding/{onboarding_id}/draft",
        json={"template": template},
        headers=api_headers("submitter"),
    )
    assert response.status_code == 201, response.text
    draft: dict[str, Any] = response.json()
    return draft


def approve(client: TestClient, draft_id: str, who: str = "reviewer") -> dict[str, Any]:
    detail = client.get(f"/api/v1/emails/{draft_id}", headers=api_headers(who)).json()
    response = client.post(
        f"/api/v1/emails/{draft_id}/approve",
        json={"content_hash": detail["draft"]["content_hash"], "comment": "Checked"},
        headers=api_headers(who),
    )
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


def sent_message(route: respx.Route) -> Message:
    raw = json.loads(route.calls.last.request.content)["raw"]
    return email.message_from_bytes(base64.urlsafe_b64decode(raw))


def test_complete_volunteer_onboarding(client: TestClient, gmail_route: respx.Route) -> None:
    gmail_route.mock(return_value=gmail_ok("gmail-vol-1"))
    onboarding_id = create_onboarding(client, volunteer_payload())

    draft = prepare(client, onboarding_id, "welcome")
    assert draft["status"] == "PENDING_REVIEW"
    assert draft["prepared_by"] == "agent:onboarding"
    assert draft["requested_by"] == "sam.submitter"
    assert draft["content"]["subject"] == "Welcome to Riverside Community Food Bank!"
    assert draft["content"]["recipients"] == ["alex.morgan@example.org"]
    assert draft["content"]["sender"] == SENDER
    assert "Monday 7 December 2026" in draft["content"]["body"]
    assert draft["review"]["agent_id"] == "agent:review"
    assert not [f for f in draft["review"]["findings"] if f["severity"] == "ERROR"]

    pending = client.get("/api/v1/emails/pending-review", headers=api_headers("reviewer")).json()
    assert [d["id"] for d in pending] == [draft["id"]]

    assert approve(client, draft["id"])["status"] == "APPROVED"
    assert gmail_route.call_count == 0  # approval alone never sends

    response = client.post(f"/api/v1/emails/{draft['id']}/send", headers=api_headers("reviewer"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["draft_status"] == "SENT"
    assert body["delivery"]["provider"] == "gmail"
    assert body["delivery"]["provider_message_id"] == "gmail-vol-1"
    assert [a["outcome"] for a in body["delivery"]["attempts"]] == ["ACCEPTED"]

    message = sent_message(gmail_route)
    assert message["To"] == "alex.morgan@example.org"
    assert message["Subject"] == "Welcome to Riverside Community Food Bank!"
    assert message.get_content_type() == "multipart/alternative"
    assert gmail_route.calls.last.request.headers["Authorization"] == "Bearer test-access-token"

    # Induction email for the same onboarding.
    induction = prepare(client, onboarding_id, "induction")
    assert induction["content"]["subject"] == (
        "Your volunteer induction – Riverside Community Food Bank"
    )
    assert "Saturday 5 December 2026" in induction["content"]["body"]

    history = client.get(
        f"/api/v1/onboarding/{onboarding_id}/history", headers=api_headers("reviewer")
    ).json()
    events = [e["event"] for e in history["events"]]
    for expected in (
        "onboarding_created",
        "draft_prepared",
        "draft_reviewed",
        "draft_approved",
        "delivery_started",
        "delivery_attempt",
        "delivery_completed",
    ):
        assert expected in events
    assert events[0] == "onboarding_created"

    volunteers = client.get(
        "/api/v1/onboarding", params={"type": "volunteer"}, headers=api_headers("reviewer")
    ).json()
    assert [o["id"] for o in volunteers] == [onboarding_id]
    unsent = client.get("/api/v1/emails/unsent", headers=api_headers("reviewer")).json()
    assert [d["id"] for d in unsent] == [induction["id"]]


def test_complete_employee_onboarding(client: TestClient, gmail_route: respx.Route) -> None:
    gmail_route.mock(return_value=gmail_ok("gmail-emp-1"))
    onboarding_id = create_onboarding(client, employee_payload())
    onboarding = client.get(
        f"/api/v1/onboarding/{onboarding_id}", headers=api_headers("submitter")
    ).json()
    assert onboarding["team_name"] == "Insights"
    assert onboarding["role_title"] == "Data Analyst"

    welcome = prepare(client, onboarding_id, "welcome")
    assert welcome["content"]["subject"] == "Welcome to Northwind Analytics Ltd – your new role"
    first_day = prepare(client, onboarding_id, "first_day")
    assert first_day["content"]["subject"] == "Your first day at Northwind Analytics Ltd"
    assert "Bring photo ID" in first_day["content"]["body"]
    assert "Monday 4 January 2027" in first_day["content"]["body"]

    approve(client, first_day["id"])
    response = client.post(
        f"/api/v1/emails/{first_day['id']}/send", headers=api_headers("reviewer")
    )
    assert response.status_code == 200
    assert response.json()["delivery"]["provider_message_id"] == "gmail-emp-1"

    plain = sent_message(gmail_route)
    parts = {p.get_content_type(): p.get_payload(decode=True) for p in plain.walk()}
    assert b"Bring photo ID" in parts["text/plain"]
    assert b"<strong>Data Analyst</strong>" in parts["text/html"]

    employees = client.get(
        "/api/v1/onboarding", params={"type": "employee"}, headers=api_headers("reviewer")
    ).json()
    assert len(employees) == 1


def test_wrong_template_for_type_is_rejected(client: TestClient) -> None:
    onboarding_id = create_onboarding(client, employee_payload())
    response = client.post(
        f"/api/v1/onboarding/{onboarding_id}/draft",
        json={"template": "induction"},
        headers=api_headers("submitter"),
    )
    assert response.status_code == 422
    assert response.json()["error"]["details"]["allowed"] == ["first_day", "welcome"]


def test_duplicate_draft_for_same_template_is_refused(client: TestClient) -> None:
    onboarding_id = create_onboarding(client, volunteer_payload())
    first = prepare(client, onboarding_id, "welcome")
    response = client.post(
        f"/api/v1/onboarding/{onboarding_id}/draft",
        json={"template": "welcome"},
        headers=api_headers("submitter"),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "duplicate_submission"
    assert response.json()["error"]["details"]["draft_id"] == first["id"]

    # After cancelling, a fresh draft may be prepared.
    cancel = client.post(f"/api/v1/emails/{first['id']}/cancel", headers=api_headers("submitter"))
    assert cancel.json()["status"] == "CANCELLED"
    assert prepare(client, onboarding_id, "welcome")["id"] != first["id"]


def test_onboarding_creation_is_idempotent(client: TestClient) -> None:
    first = client.post(
        "/api/v1/onboarding", json=volunteer_payload(), headers=api_headers("submitter")
    )
    second = client.post(
        "/api/v1/onboarding", json=volunteer_payload(), headers=api_headers("submitter")
    )
    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json()["onboarding"]["id"] == second.json()["onboarding"]["id"]
    assert second.json()["created"] is False
