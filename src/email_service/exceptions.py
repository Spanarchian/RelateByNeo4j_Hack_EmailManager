"""Domain exceptions. Each maps to a stable error code and HTTP status."""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class InvalidStateError(ConflictError):
    code = "invalid_state"


class DuplicateSubmissionError(ConflictError):
    code = "duplicate_submission"


class ApprovalIntegrityError(ConflictError):
    code = "approval_integrity"


class ValidationFailedError(AppError):
    status_code = 422
    code = "validation_failed"


class TemplateRenderError(ValidationFailedError):
    code = "template_error"


class AuthenticationError(AppError):
    status_code = 401
    code = "unauthenticated"


class PermissionDeniedError(AppError):
    status_code = 403
    code = "forbidden"


class RateLimitedError(AppError):
    status_code = 429
    code = "rate_limited"


class GraphUnavailableError(AppError):
    status_code = 503
    code = "graph_unavailable"


class CredentialsUnavailableError(AppError):
    """Raised by providers when credentials are missing or cannot be refreshed."""

    status_code = 503
    code = "credentials_unavailable"
