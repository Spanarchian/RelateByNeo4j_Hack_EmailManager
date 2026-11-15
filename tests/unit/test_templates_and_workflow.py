from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from email_service.exceptions import InvalidStateError, TemplateRenderError, ValidationFailedError
from email_service.models.email import DraftStatus
from email_service.models.onboarding import (
    Onboarding,
    OnboardingStatus,
    OnboardingType,
    TemplateKind,
)
from email_service.onboarding.templates import (
    DEFAULT_TEMPLATES,
    EMPLOYEE_FIRST_DAY,
    VOLUNTEER_WELCOME,
    TemplateRenderer,
)
from email_service.onboarding.workflow import (
    ALLOWED_TRANSITIONS,
    build_template_context,
    can_transition,
    ensure_transition,
    format_date,
    select_template_id,
)

pytestmark = [pytest.mark.unit, pytest.mark.workflow]


def onboarding(kind: OnboardingType) -> Onboarding:
    details = (
        {"induction_details": "Saturday 10:00 at the warehouse."}
        if kind is OnboardingType.VOLUNTEER
        else {"department": "Insights", "first_day_arrangements": "Arrive at 09:30."}
    )
    return Onboarding(
        id="onb-1",
        type=kind,
        status=OnboardingStatus.CREATED,
        person_id="p",
        person_name="Alex Morgan",
        person_email="alex@example.org",
        organisation_id="o",
        organisation_name="Riverside & Co <Charity>",
        team_id="t",
        team_name="Warehouse",
        role_title="Driver",
        contact_name="Priya Shah",
        start_date=date(2026, 12, 7),
        details=details,
        created_by="u",
        created_at=datetime(2026, 11, 1, tzinfo=UTC),
    )


EXPECTED_SUBJECTS = {
    "volunteer-welcome": "Welcome to Riverside & Co <Charity>!",
    "volunteer-induction": "Your volunteer induction – Riverside & Co <Charity>",
    "employee-welcome": "Welcome to Riverside & Co <Charity> – your new role",
    "employee-first-day": "Your first day at Riverside & Co <Charity>",
}


@pytest.mark.parametrize("template", DEFAULT_TEMPLATES, ids=lambda t: t.id)
def test_all_templates_render_text_and_html(template: object) -> None:
    from email_service.onboarding.templates import EmailTemplate

    assert isinstance(template, EmailTemplate)
    context = build_template_context(onboarding(template.onboarding_type))
    rendered = TemplateRenderer().render(template, context)
    assert rendered.subject == EXPECTED_SUBJECTS[template.id]
    assert "Dear Alex," in rendered.text
    assert "Monday 7 December 2026" in rendered.text
    assert "{{" not in rendered.text and "{{" not in rendered.html
    # HTML is auto-escaped; plain text is not.
    assert "Riverside &amp; Co &lt;Charity&gt;" in rendered.html
    assert "<Charity>" not in rendered.html
    assert "Riverside & Co <Charity>" in rendered.text


def test_missing_required_value_rejected() -> None:
    context = build_template_context(onboarding(OnboardingType.VOLUNTEER))
    context["coordinator_name"] = "  "
    with pytest.raises(TemplateRenderError) as exc:
        TemplateRenderer().render(VOLUNTEER_WELCOME, context)
    assert exc.value.details["missing_fields"] == ["coordinator_name"]


def test_unresolved_placeholder_rejected() -> None:
    template = VOLUNTEER_WELCOME.__class__(
        **{
            **{f: getattr(VOLUNTEER_WELCOME, f) for f in VOLUNTEER_WELCOME.__slots__},
            "text": "Dear {{ first_name }}, your badge number is {{ badge_number }}.",
        }
    )
    context = build_template_context(onboarding(OnboardingType.VOLUNTEER))
    with pytest.raises(TemplateRenderError, match="badge_number"):
        TemplateRenderer().render(template, context)


def test_template_syntax_in_values_rejected() -> None:
    context = build_template_context(onboarding(OnboardingType.EMPLOYEE))
    context["first_day_arrangements"] = "{{ config }}"
    with pytest.raises(TemplateRenderError, match="template syntax"):
        TemplateRenderer().render(EMPLOYEE_FIRST_DAY, context)


def test_sandbox_blocks_attribute_access() -> None:
    template = VOLUNTEER_WELCOME.__class__(
        **{
            **{f: getattr(VOLUNTEER_WELCOME, f) for f in VOLUNTEER_WELCOME.__slots__},
            "text": "{{ first_name.__class__.__mro__ }}",
        }
    )
    with pytest.raises(TemplateRenderError):
        TemplateRenderer().render(
            template, build_template_context(onboarding(OnboardingType.VOLUNTEER))
        )


class TestWorkflow:
    def test_template_selection(self) -> None:
        assert select_template_id(OnboardingType.VOLUNTEER, TemplateKind.INDUCTION) == (
            "volunteer-induction"
        )
        assert select_template_id(OnboardingType.EMPLOYEE, TemplateKind.FIRST_DAY) == (
            "employee-first-day"
        )
        with pytest.raises(ValidationFailedError):
            select_template_id(OnboardingType.VOLUNTEER, TemplateKind.FIRST_DAY)

    def test_format_date(self) -> None:
        assert format_date(date(2027, 1, 4)) == "Monday 4 January 2027"

    def test_transitions(self) -> None:
        assert can_transition(DraftStatus.APPROVED, DraftStatus.SENDING)
        assert not can_transition(DraftStatus.PENDING_REVIEW, DraftStatus.SENDING)
        assert not can_transition(DraftStatus.UNKNOWN, DraftStatus.SENDING)
        assert ALLOWED_TRANSITIONS[DraftStatus.SENT] == frozenset()
        assert set(ALLOWED_TRANSITIONS) == set(DraftStatus)
        with pytest.raises(InvalidStateError):
            ensure_transition(DraftStatus.SENT, DraftStatus.SENDING)

    def test_context_only_contains_verified_fields(self) -> None:
        context = build_template_context(onboarding(OnboardingType.EMPLOYEE))
        assert context["manager_name"] == "Priya Shah"
        assert context["first_name"] == "Alex"
        assert "person_email" not in context
