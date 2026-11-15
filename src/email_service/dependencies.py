"""Dependency wiring (composition root) and FastAPI dependency providers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

import httpx
from fastapi import Depends, Request, Security
from fastapi.security import APIKeyHeader

from email_service.agents.base import ALL_AGENTS
from email_service.agents.onboarding_agent import OnboardingAgent
from email_service.agents.review_agent import ReviewAgent
from email_service.approvals.service import ApprovalService
from email_service.config import Settings
from email_service.exceptions import AuthenticationError, PermissionDeniedError
from email_service.graph.database import RelateDatabase
from email_service.graph.repositories import GraphRepository
from email_service.onboarding.templates import DEFAULT_TEMPLATES, TemplateRenderer
from email_service.providers.base import EmailProvider
from email_service.providers.gmail import GmailCredentialStore, GmailProvider
from email_service.providers.mailchimp import MailchimpTransactionalProvider
from email_service.security.authentication import (
    API_KEY_HEADER,
    ApiKeyAuthenticator,
    ApiKeyRecord,
    Principal,
)
from email_service.security.permissions import Permission
from email_service.services.email_service import EmailService
from email_service.services.onboarding_service import OnboardingService
from email_service.services.provider_service import ProviderService, RetryPolicy

Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Container:
    settings: Settings
    db: RelateDatabase
    repository: GraphRepository
    http: httpx.AsyncClient
    provider_service: ProviderService
    review_agent: ReviewAgent
    onboarding_agent: OnboardingAgent
    email_service: EmailService
    approval_service: ApprovalService
    onboarding_service: OnboardingService
    authenticator: ApiKeyAuthenticator

    async def start(self) -> None:
        await self.db.open()
        await self.repository.ensure_schema()
        provider_rows = [(self.provider_service.primary, "primary", True)]
        if self.provider_service.secondary:
            provider_rows.append(
                (
                    self.provider_service.secondary,
                    "secondary",
                    self.provider_service.failover_enabled,
                )
            )
        await self.repository.seed_reference_data(
            DEFAULT_TEMPLATES,
            [(a.id, a.name, a.kind, a.description) for a in ALL_AGENTS],
            provider_rows,
        )
        await self.repository.recover_interrupted_sends()

    async def stop(self) -> None:
        await self.http.aclose()
        await self.db.close()


def build_providers(settings: Settings, http: httpx.AsyncClient) -> dict[str, EmailProvider]:
    return {
        "gmail": GmailProvider(
            GmailCredentialStore(settings.gmail_token_file),
            http,
            base_url=settings.gmail_api_base_url,
            timeout=settings.provider_timeout_seconds,
        ),
        "mailchimp": MailchimpTransactionalProvider(
            settings.mailchimp_transactional_api_key,
            http,
            base_url=settings.mailchimp_api_base_url,
            from_name=settings.mailchimp_from_name,
            timeout=settings.provider_timeout_seconds,
        ),
    }


def build_container(
    settings: Settings,
    *,
    providers: Mapping[str, EmailProvider] | None = None,
    http: httpx.AsyncClient | None = None,
    clock: Clock = utc_now,
    sleep: Sleep | None = None,
) -> Container:
    http = http or httpx.AsyncClient(timeout=settings.provider_timeout_seconds)
    db = RelateDatabase(settings.relate_store_path)
    repository = GraphRepository(db, clock)
    allowed = tuple(str(s) for s in settings.email_allowed_senders)
    provider_map = dict(providers) if providers is not None else build_providers(settings, http)
    provider_service = ProviderService(
        provider_map,
        primary=settings.email_primary_provider,
        secondary=settings.email_secondary_provider,
        failover_enabled=settings.email_failover_enabled,
        retry=RetryPolicy(
            max_retries=settings.provider_max_retries,
            backoff_seconds=settings.provider_retry_backoff_seconds,
        ),
        sleep=sleep or asyncio.sleep,
        clock=clock,
    )
    review_agent = ReviewAgent(clock=clock)
    onboarding_agent = OnboardingAgent(
        repository,
        TemplateRenderer(),
        review_agent,
        sender=str(settings.email_default_sender) if settings.email_default_sender else None,
        allowed_senders=allowed,
        max_recipients=settings.email_max_recipients,
        clock=clock,
    )
    return Container(
        settings=settings,
        db=db,
        repository=repository,
        http=http,
        provider_service=provider_service,
        review_agent=review_agent,
        onboarding_agent=onboarding_agent,
        email_service=EmailService(
            repository,
            review_agent,
            provider_service,
            max_recipients=settings.email_max_recipients,
            allowed_senders=allowed,
            clock=clock,
        ),
        approval_service=ApprovalService(
            repository, four_eyes=settings.approval_require_four_eyes, clock=clock
        ),
        onboarding_service=OnboardingService(repository, onboarding_agent, clock),
        authenticator=ApiKeyAuthenticator(
            ApiKeyRecord(name=e.name, key_sha256=e.key_sha256, roles=e.roles, kind=e.kind)
            for e in settings.api_keys
        ),
    )


# --- FastAPI dependencies --------------------------------------------------------

api_key_scheme = APIKeyHeader(name=API_KEY_HEADER, auto_error=False, description="API key")


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container


ContainerDep = Annotated[Container, Depends(get_container)]


def get_principal(
    container: ContainerDep, api_key: Annotated[str | None, Security(api_key_scheme)]
) -> Principal:
    principal = container.authenticator.authenticate(api_key)
    if principal is None:
        raise AuthenticationError("a valid X-API-Key header is required")
    return principal


def require(permission: Permission) -> Callable[[Principal], Principal]:
    def checker(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
        if not principal.has(permission):
            raise PermissionDeniedError(
                f"permission '{permission.value}' is required",
                details={"permission": permission.value},
            )
        return principal

    return checker


def get_email_service(container: ContainerDep) -> EmailService:
    return container.email_service


def get_approval_service(container: ContainerDep) -> ApprovalService:
    return container.approval_service


def get_onboarding_service(container: ContainerDep) -> OnboardingService:
    return container.onboarding_service


EmailServiceDep = Annotated[EmailService, Depends(get_email_service)]
ApprovalServiceDep = Annotated[ApprovalService, Depends(get_approval_service)]
OnboardingServiceDep = Annotated[OnboardingService, Depends(get_onboarding_service)]
