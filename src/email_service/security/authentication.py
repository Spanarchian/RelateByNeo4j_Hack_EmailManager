"""API key authentication.

Clients send ``X-API-Key``. Only SHA-256 digests of keys are configured, and
comparison is constant-time. Generate a key with ``uv run task api-key``.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict

from email_service.security.permissions import Permission, PrincipalKind, Role, permissions_for

API_KEY_HEADER = "X-API-Key"


class Principal(BaseModel):
    model_config = ConfigDict(frozen=True)

    subject: str
    roles: frozenset[Role]
    kind: PrincipalKind = PrincipalKind.HUMAN

    @property
    def permissions(self) -> frozenset[Permission]:
        return permissions_for(self.roles, self.kind)

    def has(self, permission: Permission) -> bool:
        return permission in self.permissions


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_api_key() -> tuple[str, str]:
    """Return ``(key, sha256)``. Show the key once; store only the digest."""
    key = "goa_" + secrets.token_urlsafe(32)
    return key, hash_api_key(key)


class ApiKeyRecord(BaseModel):
    name: str
    key_sha256: str
    roles: frozenset[Role]
    kind: PrincipalKind


class ApiKeyAuthenticator:
    def __init__(self, records: Iterable[ApiKeyRecord]) -> None:
        self._records = list(records)

    def authenticate(self, raw_key: str | None) -> Principal | None:
        if not raw_key or len(raw_key) > 256:
            return None
        digest = hash_api_key(raw_key)
        match: ApiKeyRecord | None = None
        for record in self._records:  # no early exit: constant work per request
            if hmac.compare_digest(digest, record.key_sha256):
                match = record
        if match is None:
            return None
        return Principal(subject=match.name, roles=match.roles, kind=match.kind)
