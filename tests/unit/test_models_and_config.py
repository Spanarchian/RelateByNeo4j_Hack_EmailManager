from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from pydantic import SecretStr, TypeAdapter, ValidationError

from email_service.config import ApiKeyEntry, Settings
from email_service.models.email import (
    DeliveryStatus,
    DraftUpdate,
    EmailContent,
    EmailRequest,
    ReconcileRequest,
)
from email_service.models.onboarding import (
    EmployeeOnboardingRequest,
    OnboardingRequest,
    VolunteerOnboardingRequest,
)
from email_service.security.authentication import hash_api_key
from email_service.security.permissions import PrincipalKind, Role
from tests.conftest import employee_payload, volunteer_payload

pytestmark = pytest.mark.unit


def content(**overrides: Any) -> EmailContent:
    data: dict[str, Any] = {
        "sender": "sender@example.org",
        "recipients": ["a@example.org"],
        "subject": "Hello",
        "body": "Body text",
    }
    data.update(overrides)
    return EmailContent(**data)


class TestEmailModels:
    def test_valid_request_maps_to_content(self) -> None:
        request = EmailRequest(
            title="Welcome to our organisation",
            body="Welcome to our team.",
            html_body="<h1>Welcome</h1>",
            sender="sender@gmail.com",
            recipients=["person1@example.com", "person2@example.com"],
        )
        mapped = request.to_content()
        assert mapped.subject == "Welcome to our organisation"
        assert mapped.recipients == ["person1@example.com", "person2@example.com"]

    @pytest.mark.parametrize("address", ["not-an-email", "a@", "@example.org", "a b@example.org"])
    def test_invalid_email_addresses_rejected(self, address: str) -> None:
        with pytest.raises(ValidationError):
            content(recipients=[address])
        with pytest.raises(ValidationError):
            content(sender=address)

    def test_recipient_count_bounds(self) -> None:
        with pytest.raises(ValidationError):
            content(recipients=[])
        with pytest.raises(ValidationError):
            content(recipients=[f"p{i}@example.org" for i in range(101)])

    def test_duplicate_recipients_rejected_case_insensitively(self) -> None:
        with pytest.raises(ValidationError, match="unique"):
            content(recipients=["A@example.org", "a@example.org"])

    @pytest.mark.parametrize("subject", ["Hi\r\nBcc: evil@example.org", "Hi\nthere"])
    def test_header_injection_rejected(self, subject: str) -> None:
        with pytest.raises(ValidationError, match="line breaks"):
            content(subject=subject)

    def test_plain_text_body_is_required(self) -> None:
        with pytest.raises(ValidationError):
            content(body="   ")

    def test_blank_html_becomes_none(self) -> None:
        assert content(html_body="  ").html_body is None

    def test_content_is_immutable(self) -> None:
        item = content()
        with pytest.raises(ValidationError):
            item.subject = "changed"  # type: ignore[misc]

    def test_draft_update_applies_only_set_fields(self) -> None:
        original = content(html_body="<p>x</p>")
        updated = DraftUpdate(title="New").apply(original)
        assert updated.subject == "New"
        assert updated.body == original.body
        assert updated.html_body == "<p>x</p>"
        cleared = DraftUpdate(html_body=None).apply(original)
        assert cleared.html_body is None

    def test_reconcile_requires_definitive_outcome(self) -> None:
        assert ReconcileRequest(outcome=DeliveryStatus.SENT, note="checked").outcome == "SENT"
        with pytest.raises(ValidationError):
            ReconcileRequest(outcome=DeliveryStatus.UNKNOWN, note="checked")


class TestOnboardingModels:
    adapter: TypeAdapter[Any] = TypeAdapter(OnboardingRequest)

    def test_discriminated_union(self) -> None:
        assert isinstance(
            self.adapter.validate_python(volunteer_payload()), VolunteerOnboardingRequest
        )
        assert isinstance(
            self.adapter.validate_python(employee_payload()), EmployeeOnboardingRequest
        )

    def test_whitespace_is_normalised(self) -> None:
        parsed = self.adapter.validate_python(volunteer_payload(name="  Alex   Morgan "))
        assert parsed.name == "Alex Morgan"
        assert parsed.start_date == date(2026, 12, 7)

    @pytest.mark.parametrize(
        "field", ["name", "volunteer_role", "coordinator", "induction_details"]
    )
    def test_required_volunteer_fields(self, field: str) -> None:
        with pytest.raises(ValidationError):
            self.adapter.validate_python(volunteer_payload(**{field: "   "}))

    @pytest.mark.parametrize(
        "field", ["job_title", "department", "manager", "first_day_arrangements"]
    )
    def test_required_employee_fields(self, field: str) -> None:
        payload = employee_payload()
        del payload[field]
        with pytest.raises(ValidationError):
            self.adapter.validate_python(payload)

    def test_unknown_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self.adapter.validate_python(volunteer_payload(type="contractor"))


class TestSettings:
    def base(self, **overrides: Any) -> Settings:
        return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]

    def test_defaults(self) -> None:
        settings = self.base()
        assert settings.email_primary_provider == "gmail"
        assert settings.email_secondary_provider == "mailchimp"
        assert settings.email_failover_enabled is True
        assert settings.mailchimp_enabled is False  # disabled until a key is provided

    def test_mailchimp_enabled_with_key_and_secret_hidden(self) -> None:
        settings = self.base(mailchimp_transactional_api_key=SecretStr("abc123"))
        assert settings.mailchimp_enabled
        assert "abc123" not in repr(settings)

    def test_env_parsing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EMAIL_ALLOWED_SENDERS", "a@example.org, b@example.org")
        monkeypatch.setenv("EMAIL_SECONDARY_PROVIDER", "none")
        monkeypatch.setenv("MAILCHIMP_TRANSACTIONAL_API_KEY", "")
        digest = hash_api_key("k")
        monkeypatch.setenv(
            "API_KEYS", f'[{{"name":"r","key_sha256":"{digest}","roles":["reviewer"]}}]'
        )
        settings = self.base()
        assert settings.email_allowed_senders == ["a@example.org", "b@example.org"]
        assert settings.email_secondary_provider is None
        assert settings.mailchimp_enabled is False
        assert settings.api_keys[0].roles == frozenset({Role.REVIEWER})

    def test_primary_and_secondary_must_differ(self) -> None:
        with pytest.raises(ValidationError, match="must differ"):
            self.base(email_primary_provider="gmail", email_secondary_provider="gmail")

    def test_production_guards(self) -> None:
        with pytest.raises(ValidationError, match="in-memory"):
            self.base(app_env="production", relate_store_path=":memory:")
        with pytest.raises(ValidationError, match="API_KEYS"):
            self.base(app_env="production")

    def test_duplicate_client_names(self) -> None:
        entry = {"name": "x", "key_sha256": hash_api_key("k"), "roles": ["submitter"]}
        with pytest.raises(ValidationError, match="unique"):
            self.base(api_keys=[entry, entry])


class TestApiKeyEntry:
    def test_hash_must_be_sha256_hex(self) -> None:
        with pytest.raises(ValidationError):
            ApiKeyEntry(name="x", key_sha256="plaintext-key", roles=frozenset({Role.SUBMITTER}))

    def test_agents_cannot_have_keys(self) -> None:
        with pytest.raises(ValidationError, match="agent"):
            ApiKeyEntry(
                name="agent:bot", key_sha256=hash_api_key("k"), roles=frozenset({Role.SUBMITTER})
            )

    def test_service_clients_cannot_review(self) -> None:
        with pytest.raises(ValidationError, match="reviewer"):
            ApiKeyEntry(
                name="svc",
                key_sha256=hash_api_key("k"),
                roles=frozenset({Role.REVIEWER}),
                kind=PrincipalKind.SERVICE,
            )
