"""Application configuration loaded from the environment (and an optional ``.env``)."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, EmailStr, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from email_service.security.permissions import PrincipalKind, Role

ProviderName = Literal["gmail", "mailchimp"]
MEMORY_STORE = ":memory:"
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


class ApiKeyEntry(BaseModel):
    """One API client. Only the SHA-256 hash of the key is configured, never the key."""

    name: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.@:-]+$")
    key_sha256: str
    roles: frozenset[Role] = Field(min_length=1)
    kind: PrincipalKind = PrincipalKind.HUMAN

    @field_validator("key_sha256")
    @classmethod
    def _check_hash(cls, value: str) -> str:
        value = value.strip().lower()
        if not _SHA256_HEX.match(value):
            raise ValueError("key_sha256 must be a 64-character hexadecimal SHA-256 digest")
        return value

    @model_validator(mode="after")
    def _agents_are_not_api_clients(self) -> ApiKeyEntry:
        if self.kind is PrincipalKind.AGENT or self.name.lower().startswith("agent:"):
            raise ValueError("agent identities cannot be issued API keys")
        if self.kind is PrincipalKind.SERVICE and Role.REVIEWER in self.roles:
            raise ValueError(
                "service clients cannot hold the reviewer role; approvals are human-only"
            )
        return self


class Settings(BaseSettings):
    """All runtime settings. Secrets use ``SecretStr`` so they never appear in reprs or logs."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    app_env: Literal["development", "test", "production"] = "development"
    app_name: str = "Gmail Onboarding Assistant"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # --- API security -------------------------------------------------------
    api_keys: list[ApiKeyEntry] = Field(default_factory=list)
    rate_limit_per_minute: int = Field(default=120, ge=0)
    approval_require_four_eyes: bool = True

    # --- Relate by Neo4j ----------------------------------------------------
    relate_store_path: str = "data/graph/onboarding"

    # --- Email --------------------------------------------------------------
    email_primary_provider: ProviderName = "gmail"
    email_secondary_provider: ProviderName | None = "mailchimp"
    email_failover_enabled: bool = True
    email_default_sender: EmailStr | None = None
    email_allowed_senders: Annotated[list[EmailStr], NoDecode] = Field(default_factory=list)
    email_max_recipients: int = Field(default=20, ge=1, le=100)

    provider_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    provider_max_retries: int = Field(default=2, ge=0, le=5)
    provider_retry_backoff_seconds: float = Field(default=1.0, ge=0, le=60)

    # --- Gmail --------------------------------------------------------------
    gmail_credentials_file: Path = Path("secrets/credentials.json")
    gmail_token_file: Path = Path("secrets/token.json")
    gmail_api_base_url: str = "https://gmail.googleapis.com"

    # --- Mailchimp Transactional -------------------------------------------
    mailchimp_transactional_api_key: SecretStr | None = None
    mailchimp_api_base_url: str = "https://mandrillapp.com/api/1.0"
    mailchimp_from_name: str | None = None

    @field_validator("email_secondary_provider", mode="before")
    @classmethod
    def _none_provider(cls, value: object) -> object:
        if isinstance(value, str) and value.strip().lower() in {"", "none", "disabled"}:
            return None
        return value

    @field_validator("email_allowed_senders", mode="before")
    @classmethod
    def _split_senders(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("mailchimp_transactional_api_key", mode="before")
    @classmethod
    def _empty_key_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _check_consistency(self) -> Settings:
        if self.email_secondary_provider == self.email_primary_provider:
            raise ValueError("EMAIL_SECONDARY_PROVIDER must differ from EMAIL_PRIMARY_PROVIDER")
        names = [entry.name for entry in self.api_keys]
        if len(names) != len(set(names)):
            raise ValueError("API key client names must be unique")
        if self.app_env == "production":
            if self.relate_store_path == MEMORY_STORE:
                raise ValueError("an in-memory Relate store is not allowed in production")
            if not self.api_keys:
                raise ValueError("API_KEYS must be configured in production")
        return self

    @property
    def mailchimp_enabled(self) -> bool:
        """Mailchimp stays disabled until an API key is provided."""
        key = self.mailchimp_transactional_api_key
        return key is not None and bool(key.get_secret_value())

    @property
    def uses_memory_store(self) -> bool:
        return self.relate_store_path == MEMORY_STORE


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
