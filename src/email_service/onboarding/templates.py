"""The four onboarding email templates and a safe renderer.

Rendering uses Jinja2's ``SandboxedEnvironment`` with ``StrictUndefined``: a
template can only read the values it is given, a missing value is an error
(never an empty string), and the HTML variant is auto-escaped. After rendering,
any remaining placeholder syntax is rejected.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from jinja2 import StrictUndefined, TemplateError, UndefinedError
from jinja2.sandbox import SandboxedEnvironment

from email_service.exceptions import TemplateRenderError
from email_service.models.onboarding import OnboardingType, TemplateKind

UNRESOLVED_PATTERN = re.compile(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}|\{\{|\}\}", re.DOTALL)


@dataclass(frozen=True, slots=True)
class EmailTemplate:
    id: str
    version: int
    name: str
    kind: TemplateKind
    onboarding_type: OnboardingType
    subject: str
    text: str
    html: str
    required_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RenderedEmail:
    subject: str
    text: str
    html: str


_HTML_OPEN = (
    '<div style="font-family: Arial, Helvetica, sans-serif; font-size: 15px; '
    'line-height: 1.5; color: #1f2933; max-width: 600px;">'
)
_HTML_CLOSE = "</div>"

VOLUNTEER_WELCOME = EmailTemplate(
    id="volunteer-welcome",
    version=1,
    name="Volunteer welcome",
    kind=TemplateKind.WELCOME,
    onboarding_type=OnboardingType.VOLUNTEER,
    subject="Welcome to {{ organisation_name }}!",
    text="""Dear {{ first_name }},

Welcome to {{ organisation_name }}! We are delighted that you have chosen to give your time to us as a {{ volunteer_role }}.

Your volunteering begins on {{ start_date }}, and you will be part of the {{ team_name }} team. {{ coordinator_name }} will be your volunteer coordinator and your first point of contact for any questions before and after you start.

Over the next few days you will receive details of your induction. In the meantime, if anything about your role or availability changes, please reply to this email and let us know.

Thank you for joining us. We are looking forward to working with you.

Kind regards,
{{ coordinator_name }}
{{ organisation_name }}
""",
    html=_HTML_OPEN
    + """
<p>Dear {{ first_name }},</p>
<p>Welcome to <strong>{{ organisation_name }}</strong>! We are delighted that you have chosen to give your time to us as a <strong>{{ volunteer_role }}</strong>.</p>
<p>Your volunteering begins on <strong>{{ start_date }}</strong>, and you will be part of the {{ team_name }} team. {{ coordinator_name }} will be your volunteer coordinator and your first point of contact for any questions before and after you start.</p>
<p>Over the next few days you will receive details of your induction. In the meantime, if anything about your role or availability changes, please reply to this email and let us know.</p>
<p>Thank you for joining us. We are looking forward to working with you.</p>
<p>Kind regards,<br>{{ coordinator_name }}<br>{{ organisation_name }}</p>
"""
    + _HTML_CLOSE,
    required_fields=(
        "first_name",
        "organisation_name",
        "volunteer_role",
        "start_date",
        "team_name",
        "coordinator_name",
    ),
)

VOLUNTEER_INDUCTION = EmailTemplate(
    id="volunteer-induction",
    version=1,
    name="Volunteer induction",
    kind=TemplateKind.INDUCTION,
    onboarding_type=OnboardingType.VOLUNTEER,
    subject="Your volunteer induction – {{ organisation_name }}",
    text="""Dear {{ first_name }},

As you prepare to start as a {{ volunteer_role }} with {{ organisation_name }} on {{ start_date }}, here are the details of your induction:

{{ induction_details }}

During the induction you will meet the {{ team_name }} team, learn about our safeguarding and health and safety procedures, and have time to ask any questions.

Please bring comfortable clothing and let us know in advance about any access requirements so that we can make suitable arrangements.

If you are unable to attend, please contact {{ coordinator_name }} as soon as possible so that we can arrange an alternative session.

Best wishes,
{{ coordinator_name }}
Volunteer Coordinator, {{ organisation_name }}
""",
    html=_HTML_OPEN
    + """
<p>Dear {{ first_name }},</p>
<p>As you prepare to start as a <strong>{{ volunteer_role }}</strong> with {{ organisation_name }} on <strong>{{ start_date }}</strong>, here are the details of your induction:</p>
<p style="white-space: pre-line; background: #f5f7fa; padding: 12px; border-radius: 4px;">{{ induction_details }}</p>
<p>During the induction you will meet the {{ team_name }} team, learn about our safeguarding and health and safety procedures, and have time to ask any questions.</p>
<p>Please bring comfortable clothing and let us know in advance about any access requirements so that we can make suitable arrangements.</p>
<p>If you are unable to attend, please contact {{ coordinator_name }} as soon as possible so that we can arrange an alternative session.</p>
<p>Best wishes,<br>{{ coordinator_name }}<br>Volunteer Coordinator, {{ organisation_name }}</p>
"""
    + _HTML_CLOSE,
    required_fields=(
        "first_name",
        "organisation_name",
        "volunteer_role",
        "start_date",
        "induction_details",
        "team_name",
        "coordinator_name",
    ),
)

EMPLOYEE_WELCOME = EmailTemplate(
    id="employee-welcome",
    version=1,
    name="Employee welcome",
    kind=TemplateKind.WELCOME,
    onboarding_type=OnboardingType.EMPLOYEE,
    subject="Welcome to {{ organisation_name }} – your new role",
    text="""Dear {{ first_name }},

On behalf of everyone at {{ organisation_name }}, welcome! We are very pleased that you are joining us as {{ job_title }} in the {{ department }} department.

Your employment starts on {{ start_date }}. {{ manager_name }} will be your line manager and will guide you through your first weeks, including your objectives and the people you will be working with.

Before your first day you will receive a separate email with practical arrangements. If you have any questions in the meantime, please reply to this email.

We look forward to welcoming you to the team.

Kind regards,
{{ manager_name }}
{{ department }}, {{ organisation_name }}
""",
    html=_HTML_OPEN
    + """
<p>Dear {{ first_name }},</p>
<p>On behalf of everyone at <strong>{{ organisation_name }}</strong>, welcome! We are very pleased that you are joining us as <strong>{{ job_title }}</strong> in the {{ department }} department.</p>
<p>Your employment starts on <strong>{{ start_date }}</strong>. {{ manager_name }} will be your line manager and will guide you through your first weeks, including your objectives and the people you will be working with.</p>
<p>Before your first day you will receive a separate email with practical arrangements. If you have any questions in the meantime, please reply to this email.</p>
<p>We look forward to welcoming you to the team.</p>
<p>Kind regards,<br>{{ manager_name }}<br>{{ department }}, {{ organisation_name }}</p>
"""
    + _HTML_CLOSE,
    required_fields=(
        "first_name",
        "organisation_name",
        "job_title",
        "department",
        "start_date",
        "manager_name",
    ),
)

EMPLOYEE_FIRST_DAY = EmailTemplate(
    id="employee-first-day",
    version=1,
    name="Employee first day",
    kind=TemplateKind.FIRST_DAY,
    onboarding_type=OnboardingType.EMPLOYEE,
    subject="Your first day at {{ organisation_name }}",
    text="""Dear {{ first_name }},

Your first day as {{ job_title }} at {{ organisation_name }} is {{ start_date }}. Here is what to expect:

{{ first_day_arrangements }}

{{ manager_name }} will meet you on arrival and introduce you to the {{ department }} team. Your first day will include an introduction to our policies, setting up your equipment and accounts, and time with your new colleagues.

If anything is unclear or you are delayed on the day, please contact {{ manager_name }}.

We look forward to seeing you.

Kind regards,
{{ manager_name }}
{{ department }}, {{ organisation_name }}
""",
    html=_HTML_OPEN
    + """
<p>Dear {{ first_name }},</p>
<p>Your first day as <strong>{{ job_title }}</strong> at {{ organisation_name }} is <strong>{{ start_date }}</strong>. Here is what to expect:</p>
<p style="white-space: pre-line; background: #f5f7fa; padding: 12px; border-radius: 4px;">{{ first_day_arrangements }}</p>
<p>{{ manager_name }} will meet you on arrival and introduce you to the {{ department }} team. Your first day will include an introduction to our policies, setting up your equipment and accounts, and time with your new colleagues.</p>
<p>If anything is unclear or you are delayed on the day, please contact {{ manager_name }}.</p>
<p>We look forward to seeing you.</p>
<p>Kind regards,<br>{{ manager_name }}<br>{{ department }}, {{ organisation_name }}</p>
"""
    + _HTML_CLOSE,
    required_fields=(
        "first_name",
        "organisation_name",
        "job_title",
        "department",
        "start_date",
        "first_day_arrangements",
        "manager_name",
    ),
)

DEFAULT_TEMPLATES: tuple[EmailTemplate, ...] = (
    VOLUNTEER_WELCOME,
    VOLUNTEER_INDUCTION,
    EMPLOYEE_WELCOME,
    EMPLOYEE_FIRST_DAY,
)


class TemplateRenderer:
    def __init__(self) -> None:
        self._text_env = SandboxedEnvironment(
            undefined=StrictUndefined, autoescape=False, keep_trailing_newline=True
        )
        self._html_env = SandboxedEnvironment(
            undefined=StrictUndefined, autoescape=True, keep_trailing_newline=True
        )

    def render(self, template: EmailTemplate, context: Mapping[str, str]) -> RenderedEmail:
        missing = [f for f in template.required_fields if not str(context.get(f, "")).strip()]
        if missing:
            raise TemplateRenderError(
                "required onboarding information is missing", details={"missing_fields": missing}
            )
        for key, value in context.items():
            if UNRESOLVED_PATTERN.search(str(value)):
                raise TemplateRenderError(
                    "template values must not contain template syntax", details={"field": key}
                )
        try:
            subject = self._text_env.from_string(template.subject).render(context)
            text = self._text_env.from_string(template.text).render(context)
            html = self._html_env.from_string(template.html).render(context)
        except UndefinedError as exc:
            raise TemplateRenderError(f"unresolved placeholder: {exc.message}") from exc
        except TemplateError as exc:
            raise TemplateRenderError(f"template could not be rendered: {exc}") from exc
        subject = " ".join(subject.split())
        for part, rendered in (("subject", subject), ("text", text), ("html", html)):
            if UNRESOLVED_PATTERN.search(rendered):
                raise TemplateRenderError(
                    "rendered email still contains placeholders", details={"part": part}
                )
        return RenderedEmail(subject=subject, text=text.strip() + "\n", html=html.strip())
