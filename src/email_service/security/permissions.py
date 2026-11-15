"""Role-based authorisation.

Roles are granted to API clients. Agents are internal principals with their own
identity; they can never approve or send email, whatever roles are claimed.
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    SUBMITTER = "submitter"
    REVIEWER = "reviewer"
    ADMIN = "admin"


class PrincipalKind(StrEnum):
    HUMAN = "human"
    SERVICE = "service"
    AGENT = "agent"


class Permission(StrEnum):
    READ = "read"
    CREATE_ONBOARDING = "create_onboarding"
    PREPARE_DRAFT = "prepare_draft"
    SUBMIT_EMAIL = "submit_email"
    EDIT_DRAFT = "edit_draft"
    CANCEL_DRAFT = "cancel_draft"
    APPROVE = "approve"
    SEND = "send"
    RECONCILE = "reconcile"


ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.SUBMITTER: frozenset(
        {
            Permission.READ,
            Permission.CREATE_ONBOARDING,
            Permission.PREPARE_DRAFT,
            Permission.SUBMIT_EMAIL,
            Permission.EDIT_DRAFT,
            Permission.CANCEL_DRAFT,
        }
    ),
    Role.REVIEWER: frozenset(
        {
            Permission.READ,
            Permission.EDIT_DRAFT,
            Permission.CANCEL_DRAFT,
            Permission.APPROVE,
            Permission.SEND,
            Permission.RECONCILE,
        }
    ),
}
ROLE_PERMISSIONS[Role.ADMIN] = frozenset(Permission) - {Permission.APPROVE, Permission.SEND}

#: Permissions that require a human being, regardless of role.
HUMAN_ONLY: frozenset[Permission] = frozenset(
    {Permission.APPROVE, Permission.SEND, Permission.RECONCILE}
)


def permissions_for(roles: frozenset[Role], kind: PrincipalKind) -> frozenset[Permission]:
    granted: set[Permission] = set()
    for role in roles:
        granted |= ROLE_PERMISSIONS[role]
    if kind is not PrincipalKind.HUMAN:
        granted -= HUMAN_ONLY
    return frozenset(granted)
