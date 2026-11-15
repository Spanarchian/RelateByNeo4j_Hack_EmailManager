"""Structured error responses: ``{"error": {"code", "message", "details"}}``."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from email_service.exceptions import AppError

logger = logging.getLogger(__name__)


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = {}


class ErrorResponse(BaseModel):
    error: ErrorBody


def error_responses(*codes: int) -> dict[int | str, dict[str, Any]]:
    return {code: {"model": ErrorResponse} for code in codes}


def _payload(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details or {}}}


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error(_: Request, exc: AppError) -> JSONResponse:
        headers = {"WWW-Authenticate": "APIKey"} if exc.status_code == 401 else None
        return JSONResponse(
            status_code=exc.status_code,
            content=_payload(exc.code, exc.message, exc.details),
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"loc": list(e.get("loc", ())), "msg": e.get("msg"), "type": e.get("type")}
            for e in exc.errors()
        ]  # input values are deliberately not echoed back
        return JSONResponse(
            status_code=422,
            content=_payload("validation_failed", "request validation failed", {"errors": errors}),
        )

    @app.exception_handler(ValidationError)
    async def model_validation_error(_: Request, exc: ValidationError) -> JSONResponse:
        errors = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        return JSONResponse(
            status_code=422,
            content=_payload("validation_failed", "content validation failed", {"errors": errors}),
        )

    @app.exception_handler(Exception)
    async def unexpected(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error")
        return JSONResponse(status_code=500, content=_payload("internal_error", "internal error"))
