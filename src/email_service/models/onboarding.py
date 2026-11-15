"""Volunteer and employee onboarding models."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, EmailStr, Field, field_validator

from email_service.models.email import DraftStatus


class OnboardingType(StrEnum):
    VOLUNTEER = "volunteer"
    EMPLOYEE = "employee"


class TemplateKind(StrEnum):
    WELCOME = "welcome"
    INDUCTION = "induction"
    FIRST_DAY = "first_day"


def _clean(value: str) -> str:
    value = " ".join(value.split()) if "\n" not in value else value.strip()
    if not value:
        raise ValueError("must not be blank")
    return value


Name = Annotated[str, Field(min_length=1, max_length=120)]
Text = Annotated[str, Field(min_length=1, max_length=2000)]


class _OnboardingBase(BaseModel):
    name: Name
    email: EmailStr
    organisation: Name
    start_date: date

    @field_validator("name", "organisation", mode="after")
    @classmethod
    def _strip(cls, value: str) -> str:
        return _clean(value)


class VolunteerOnboardingRequest(_OnboardingBase):
    type: Literal[OnboardingType.VOLUNTEER] = OnboardingType.VOLUNTEER
    volunteer_role: Name
    coordinator: Name
    induction_details: Text
    team: Name | None = None

    @field_validator("volunteer_role", "coordinator", "induction_details", "team", mode="after")
    @classmethod
    def _strip_fields(cls, value: str | None) -> str | None:
        return None if value is None else _clean(value)


class EmployeeOnboardingRequest(_OnboardingBase):
    type: Literal[OnboardingType.EMPLOYEE] = OnboardingType.EMPLOYEE
    job_title: Name
    department: Name
    manager: Name
    first_day_arrangements: Text

    @field_validator("job_title", "department", "manager", "first_day_arrangements", mode="after")
    @classmethod
    def _strip_fields(cls, value: str) -> str:
        return _clean(value)


OnboardingRequest = Annotated[
    VolunteerOnboardingRequest | EmployeeOnboardingRequest, Field(discriminator="type")
]


class OnboardingStatus(StrEnum):
    CREATED = "CREATED"  # no drafts yet
    IN_PROGRESS = "IN_PROGRESS"  # at least one draft prepared


class Onboarding(BaseModel):
    id: str
    type: OnboardingType
    status: OnboardingStatus
    person_id: str
    person_name: str
    person_email: EmailStr
    organisation_id: str
    organisation_name: str
    team_id: str
    team_name: str
    role_title: str
    contact_name: str
    start_date: date
    details: dict[str, str]
    created_by: str
    created_at: datetime


class OnboardingCreated(BaseModel):
    onboarding: Onboarding
    created: bool


class DraftRequest(BaseModel):
    template: TemplateKind = TemplateKind.WELCOME


class HistoryEvent(BaseModel):
    at: datetime
    event: str
    actor: str | None = None
    draft_id: str | None = None
    status: DraftStatus | str | None = None
    detail: str | None = None


class OnboardingHistory(BaseModel):
    onboarding: Onboarding
    events: list[HistoryEvent]
