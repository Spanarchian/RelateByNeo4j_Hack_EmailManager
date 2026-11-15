"""Draft lifecycle state machine and onboarding template context."""

from __future__ import annotations

from datetime import date

from email_service.exceptions import InvalidStateError, ValidationFailedError
from email_service.models.email import DraftStatus
from email_service.models.onboarding import Onboarding, OnboardingType, TemplateKind

S = DraftStatus

ALLOWED_TRANSITIONS: dict[DraftStatus, frozenset[DraftStatus]] = {
    S.DRAFT: frozenset({S.PENDING_REVIEW, S.CANCELLED}),
    S.PENDING_REVIEW: frozenset({S.PENDING_REVIEW, S.APPROVED, S.REJECTED, S.CANCELLED}),
    S.APPROVED: frozenset({S.SENDING, S.PENDING_REVIEW, S.CANCELLED}),
    S.REJECTED: frozenset({S.PENDING_REVIEW, S.CANCELLED}),
    S.SENDING: frozenset({S.SENT, S.FAILED, S.UNKNOWN}),
    # Retrying after a *definitive* failure is allowed while the approval stays valid.
    S.FAILED: frozenset({S.SENDING, S.PENDING_REVIEW, S.CANCELLED}),
    # UNKNOWN is resolved only by a human reconciliation.
    S.UNKNOWN: frozenset({S.SENT, S.FAILED}),
    S.SENT: frozenset(),
    S.CANCELLED: frozenset(),
}

EDITABLE = frozenset({S.DRAFT, S.PENDING_REVIEW, S.APPROVED, S.REJECTED, S.FAILED})
SENDABLE = frozenset({S.APPROVED, S.FAILED})
TERMINAL = frozenset({S.SENT, S.CANCELLED})
UNSENT = frozenset({S.DRAFT, S.PENDING_REVIEW, S.APPROVED, S.REJECTED, S.FAILED})


def can_transition(current: DraftStatus, target: DraftStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


def ensure_transition(current: DraftStatus, target: DraftStatus) -> None:
    if not can_transition(current, target):
        raise InvalidStateError(
            f"cannot move a draft from {current} to {target}",
            details={"current": current.value, "target": target.value},
        )


TEMPLATE_IDS: dict[tuple[OnboardingType, TemplateKind], str] = {
    (OnboardingType.VOLUNTEER, TemplateKind.WELCOME): "volunteer-welcome",
    (OnboardingType.VOLUNTEER, TemplateKind.INDUCTION): "volunteer-induction",
    (OnboardingType.EMPLOYEE, TemplateKind.WELCOME): "employee-welcome",
    (OnboardingType.EMPLOYEE, TemplateKind.FIRST_DAY): "employee-first-day",
}


def select_template_id(onboarding_type: OnboardingType, kind: TemplateKind) -> str:
    try:
        return TEMPLATE_IDS[(onboarding_type, kind)]
    except KeyError:
        allowed = sorted(k.value for (t, k) in TEMPLATE_IDS if t is onboarding_type)
        raise ValidationFailedError(
            f"template '{kind.value}' is not available for {onboarding_type.value} onboarding",
            details={"allowed": allowed},
        ) from None


def format_date(value: date) -> str:
    """e.g. ``Monday 7 December 2026`` — unambiguous for UK and US readers."""
    return f"{value:%A} {value.day} {value:%B %Y}"


def build_template_context(onboarding: Onboarding) -> dict[str, str]:
    """Only verified, validated onboarding fields ever reach a template."""
    context = {
        "person_name": onboarding.person_name,
        "first_name": onboarding.person_name.split()[0],
        "organisation_name": onboarding.organisation_name,
        "start_date": format_date(onboarding.start_date),
        "team_name": onboarding.team_name,
    }
    if onboarding.type is OnboardingType.VOLUNTEER:
        context |= {
            "volunteer_role": onboarding.role_title,
            "coordinator_name": onboarding.contact_name,
            "induction_details": onboarding.details["induction_details"],
        }
    else:
        context |= {
            "job_title": onboarding.role_title,
            "department": onboarding.details["department"],
            "manager_name": onboarding.contact_name,
            "first_day_arrangements": onboarding.details["first_day_arrangements"],
        }
    return context
