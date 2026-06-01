"""api/v2/exceptions.py -- typed API errors.

Routers raise these instead of ``HTTPException`` so error shapes stay
consistent (mirrors the convention used across the framework's bots).
"""
from __future__ import annotations


class AppError(Exception):
    """Base class for API errors with an HTTP status and a stable code."""

    status_code = 400
    code = "bad_request"

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ValidationError(AppError):
    status_code = 422
    code = "validation_error"


class UnauthorizedError(AppError):
    status_code = 401
    code = "unauthorized"


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"


class RateLimitedError(AppError):
    status_code = 429
    code = "rate_limited"
