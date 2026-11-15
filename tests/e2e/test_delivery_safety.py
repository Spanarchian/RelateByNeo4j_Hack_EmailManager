"""Approval integrity, duplicate prevention, failover and UNKNOWN delivery handling."""

from __future__ import annotations

import json
import threading
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from email_service.dependencies import Container
from tests.conftest import (
    MAILCHIMP_TEST_KEY,
    api_headers,
    gmail_ok,
    mailchimp_ok,
    volunteer_payload,
)
from tests.e2e.test_onboarding_flows import approve, create_onboarding, prepare

pytestmark = [pytest.mark.e2e, pytest.mark.workflow]


def approved_draft(client: TestClient) -> dict[str, Any]:
    onboarding_id = create_onboarding(client, volunteer_payload())
    draft = prepare(client, onboarding_id, "welcome")
    approve(client, draft["id"])
    return draft


def send(client: TestClient, draft_id: str, who: str = "reviewer") -> httpx.Response:
    return client.post(f"/api/v1/emails/{draft_id}/send", headers=api_headers(who))


def test_draft_rejection(client: TestClient, gmail_route: respx.Route) -> None:
    onboarding_id = create_onboarding(client, volunteer_payload())
    draft = prepare(client, onboarding_id, "welcome")
    response = client.post(
        f"/api/v1/emails/{draft['id']}/reject",
        json={"reason": "Coordinator name is wrong"},
        headers=api_headers("reviewer"),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "REJECTED"
    assert send(client, draft["id"]).status_code == 409
    assert gmail_route.call_count == 0
    detail = client.get(f"/api/v1/emails/{draft['id']}", headers=api_headers("reviewer")).json()
    assert detail["approvals"][0]["decision"] == "REJECTED"
    assert detail["approvals"][0]["valid"] is False


def test_send_requires_approval(client: TestClient, gmail_route: respx.Route) -> None:
    onboarding_id = create_onboarding(client, volunteer_payload())
    draft = prepare(client, onboarding_id, "welcome")
    response = send(client, draft["id"])
    assert response.status_code == 409
    assert gmail_route.call_count == 0


def test_gmail_failure_fails_over_to_mailchimp(
    client: TestClient, gmail_route: respx.Route, mailchimp_route: respx.Route
) -> None:
    gmail_route.mock(
        return_value=httpx.Response(
            403,
            json={
                "error": {
                    "code": 403,
                    "message": "Insufficient Permission",
                    "errors": [{"reason": "insufficientPermissions"}],
                }
            },
        )
    )
    mailchimp_route.mock(return_value=mailchimp_ok("alex.morgan@example.org", status="queued"))
    draft = approved_draft(client)

    response = send(client, draft["id"])
    assert response.status_code == 200, response.text
    delivery = response.json()["delivery"]
    assert delivery["status"] == "SENT"
    assert delivery["provider"] == "mailchimp"
    assert delivery["provider_message_id"] == "md-0"
    assert [(a["provider"], a["outcome"]) for a in delivery["attempts"]] == [
        ("gmail", "REJECTED"),
        ("mailchimp", "ACCEPTED"),
    ]
    assert delivery["attempts"][0]["error_code"] == "insufficientPermissions"
    payload = json.loads(mailchimp_route.calls.last.request.content)
    assert payload["key"] == MAILCHIMP_TEST_KEY
    assert payload["message"]["to"] == [{"email": "alex.morgan@example.org", "type": "to"}]
    assert MAILCHIMP_TEST_KEY not in response.text  # never echoed back


def test_gmail_timeout_is_unknown_and_never_fails_over(
    client: TestClient, gmail_route: respx.Route, mailchimp_route: respx.Route
) -> None:
    gmail_route.mock(side_effect=httpx.ReadTimeout("timed out"))
    mailchimp_route.mock(return_value=mailchimp_ok("alex.morgan@example.org"))
    draft = approved_draft(client)

    response = send(client, draft["id"])
    assert response.status_code == 202
    body = response.json()
    assert body["draft_status"] == "UNKNOWN"
    assert body["delivery"]["status"] == "UNKNOWN"
    assert gmail_route.call_count == 1  # no retry
    assert mailchimp_route.call_count == 0  # no failover

    # A retry is refused until a human reconciles.
    assert send(client, draft["id"]).status_code == 409
    failed = client.get("/api/v1/emails/failed-deliveries", headers=api_headers("reviewer"))
    assert [d["status"] for d in failed.json()] == ["UNKNOWN"]

    # Submitters cannot reconcile; reviewers can.
    reconcile_body = {"outcome": "SENT", "note": "Found in Gmail Sent folder"}
    assert (
        client.post(
            f"/api/v1/emails/{draft['id']}/reconcile",
            json=reconcile_body,
            headers=api_headers("submitter"),
        ).status_code
        == 403
    )
    reconciled = client.post(
        f"/api/v1/emails/{draft['id']}/reconcile",
        json=reconcile_body,
        headers=api_headers("reviewer"),
    )
    assert reconciled.status_code == 200
    assert reconciled.json()["draft_status"] == "SENT"
    assert reconciled.json()["delivery"]["reconciled_by"] == "riley.reviewer"
    assert send(client, draft["id"]).status_code == 409  # SENT is terminal


def test_unknown_reconciled_as_failed_allows_a_new_delivery(
    client: TestClient, gmail_route: respx.Route
) -> None:
    gmail_route.mock(side_effect=[httpx.ReadTimeout("timed out"), gmail_ok("gmail-2")])
    draft = approved_draft(client)
    assert send(client, draft["id"]).status_code == 202
    client.post(
        f"/api/v1/emails/{draft['id']}/reconcile",
        json={"outcome": "FAILED", "note": "Not in Sent folder after 1 hour"},
        headers=api_headers("reviewer"),
    )
    retry = send(client, draft["id"])
    assert retry.status_code == 200
    assert retry.json()["delivery"]["sequence"] == 2
    assert retry.json()["delivery"]["idempotency_key"].endswith("#2")


def test_all_providers_fail(
    client: TestClient, gmail_route: respx.Route, mailchimp_route: respx.Route
) -> None:
    gmail_route.mock(return_value=httpx.Response(400, json={"error": {"message": "Bad"}}))
    mailchimp_route.mock(
        return_value=httpx.Response(
            401, json={"status": "error", "code": 401, "name": "Invalid_Key", "message": "Invalid"}
        )
    )
    draft = approved_draft(client)
    response = send(client, draft["id"])
    assert response.status_code == 502
    assert response.json()["draft_status"] == "FAILED"
    # A definitive failure may be retried while the approval stays valid.
    gmail_route.mock(return_value=gmail_ok())
    assert send(client, draft["id"]).status_code == 200


def test_duplicate_submission_prevention(client: TestClient, gmail_route: respx.Route) -> None:
    gmail_route.mock(return_value=gmail_ok())
    draft = approved_draft(client)
    assert send(client, draft["id"]).status_code == 200
    second = send(client, draft["id"])
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "duplicate_submission"
    assert gmail_route.call_count == 1


def test_concurrent_send_requests_deliver_once(
    client: TestClient, gmail_route: respx.Route
) -> None:
    gate = threading.Event()

    def slow_gmail(request: httpx.Request) -> httpx.Response:
        gate.wait(timeout=2)
        return gmail_ok()

    gmail_route.mock(side_effect=slow_gmail)
    draft = approved_draft(client)
    results: list[int] = []

    def worker() -> None:
        results.append(send(client, draft["id"]).status_code)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    gate.set()
    for t in threads:
        t.join()
    assert sorted(results) == [200, 409, 409, 409]
    assert gmail_route.call_count == 1


def test_edited_draft_invalidates_approval(client: TestClient, gmail_route: respx.Route) -> None:
    gmail_route.mock(return_value=gmail_ok())
    draft = approved_draft(client)
    edited = client.patch(
        f"/api/v1/emails/{draft['id']}",
        json={"body": draft["content"]["body"] + "\nP.S. Parking is available on site.\n"},
        headers=api_headers("submitter", **{"If-Match": "1"}),
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["status"] == "PENDING_REVIEW"
    assert edited.json()["version"] == 2
    assert send(client, draft["id"]).status_code == 409
    assert gmail_route.call_count == 0

    detail = client.get(f"/api/v1/emails/{draft['id']}", headers=api_headers("reviewer")).json()
    assert detail["approvals"][0]["valid"] is False
    assert detail["approvals"][0]["invalidated_reason"] == "content edited"

    # Approving with the stale hash is refused; approving the new content works.
    stale = client.post(
        f"/api/v1/emails/{draft['id']}/approve",
        json={"content_hash": draft["content_hash"]},
        headers=api_headers("reviewer"),
    )
    assert stale.status_code == 409
    approve(client, draft["id"])
    assert send(client, draft["id"]).status_code == 200


def test_stale_version_edit_is_refused(client: TestClient) -> None:
    onboarding_id = create_onboarding(client, volunteer_payload())
    draft = prepare(client, onboarding_id, "welcome")
    response = client.patch(
        f"/api/v1/emails/{draft['id']}",
        json={"title": "Changed"},
        headers=api_headers("submitter", **{"If-Match": "7"}),
    )
    assert response.status_code == 409


async def _tamper(container: Container, draft_id: str) -> None:
    def work(tx: Any) -> None:
        tx.run(
            "MATCH (d:EmailDraft {id: $id}) SET d.body = d.body + $extra",
            {"id": draft_id, "extra": "\nTampered."},
        )

    await container.db.write(work)


def test_out_of_band_content_change_blocks_send(
    client: TestClient, container: Container, gmail_route: respx.Route
) -> None:
    """Even if content changes behind the API's back, the hash check refuses to send."""
    draft = approved_draft(client)
    client.portal.call(_tamper, container, draft["id"])  # type: ignore[union-attr]
    response = send(client, draft["id"])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "approval_integrity"
    assert gmail_route.call_count == 0


def test_blocking_findings_prevent_approval(client: TestClient) -> None:
    response = client.post(
        "/api/v1/emails",
        json={
            "title": "Welcome {{ name }}",
            "body": "Your password: Winter2026! See you soon TODO",
            "sender": "sender@example.org",
            "recipients": ["new.starter@example.org"],
        },
        headers=api_headers("submitter"),
    )
    assert response.status_code == 201
    draft = response.json()
    codes = {f["code"] for f in draft["review"]["findings"] if f["severity"] == "ERROR"}
    assert {"content.placeholder", "sensitive.credential"} <= codes
    refused = client.post(
        f"/api/v1/emails/{draft['id']}/approve",
        json={"content_hash": draft["content_hash"]},
        headers=api_headers("reviewer"),
    )
    assert refused.status_code == 409
    assert "blocking" in refused.json()["error"]["message"]


def test_only_humans_with_reviewer_role_can_approve(client: TestClient) -> None:
    onboarding_id = create_onboarding(client, volunteer_payload())
    draft = prepare(client, onboarding_id, "welcome")
    body = {"content_hash": draft["content_hash"]}
    url = f"/api/v1/emails/{draft['id']}/approve"
    assert client.post(url, json=body).status_code == 401
    assert client.post(url, json=body, headers=api_headers("submitter")).status_code == 403
    assert client.post(url, json=body, headers=api_headers("service")).status_code == 403
    assert client.post(url, json=body, headers=api_headers("admin")).status_code == 403


def test_submitter_cannot_review_own_email(client: TestClient) -> None:
    response = client.post(
        "/api/v1/emails",
        json={
            "title": "Team update",
            "body": "Hello, a short note about next week's volunteer rota and parking.",
            "sender": "sender@example.org",
            "recipients": ["team@example.org"],
        },
        headers=api_headers("reviewer2"),  # casey has submitter + reviewer roles
    )
    draft = response.json()
    own = client.post(
        f"/api/v1/emails/{draft['id']}/approve",
        json={"content_hash": draft["content_hash"]},
        headers=api_headers("reviewer2"),
    )
    assert own.status_code == 403
    assert approve(client, draft["id"], who="reviewer")["status"] == "APPROVED"


def test_four_eyes_requester_cannot_approve_agent_draft(client: TestClient) -> None:
    created = client.post(
        "/api/v1/onboarding", json=volunteer_payload(), headers=api_headers("reviewer2")
    )
    onboarding_id = created.json()["onboarding"]["id"]
    draft = client.post(
        f"/api/v1/onboarding/{onboarding_id}/draft", headers=api_headers("reviewer2")
    ).json()
    assert draft["prepared_by"] == "agent:onboarding"
    own = client.post(
        f"/api/v1/emails/{draft['id']}/approve",
        json={"content_hash": draft["content_hash"]},
        headers=api_headers("reviewer2"),
    )
    assert own.status_code == 403
    assert own.json()["error"]["details"]["policy"] == "APPROVAL_REQUIRE_FOUR_EYES"
    assert approve(client, draft["id"], who="reviewer")["status"] == "APPROVED"
