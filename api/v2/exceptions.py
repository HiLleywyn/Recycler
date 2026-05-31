from __future__ import annotations


from fastapi import Request
from fastapi.responses import JSONResponse


class AppError(Exception):
    """Base application error that maps to an HTTP response."""

    def __init__(
        self,
        status_code: int = 500,
        error: str = "internal_error",
        code: str = "INTERNAL_ERROR",
        detail: str = "An unexpected error occurred.",
    ) -> None:
        self.status_code = status_code
        self.error = error
        self.code = code
        self.detail = detail
        super().__init__(detail)


class NotFoundError(AppError):
    def __init__(self, detail: str = "Resource not found.") -> None:
        super().__init__(status_code=404, error="not_found", code="NOT_FOUND", detail=detail)


class UnauthorizedError(AppError):
    def __init__(self, detail: str = "Authentication required.") -> None:
        super().__init__(status_code=401, error="unauthorized", code="UNAUTHORIZED", detail=detail)


class ForbiddenError(AppError):
    def __init__(self, detail: str = "Insufficient permissions.") -> None:
        super().__init__(status_code=403, error="forbidden", code="FORBIDDEN", detail=detail)


class InsufficientBalanceError(AppError):
    def __init__(self, detail: str = "Insufficient balance for this operation.") -> None:
        super().__init__(
            status_code=400,
            error="insufficient_balance",
            code="INSUFFICIENT_BALANCE",
            detail=detail,
        )


class RateLimitedError(AppError):
    def __init__(self, detail: str = "Rate limit exceeded. Please try again later.") -> None:
        super().__init__(
            status_code=429,
            error="rate_limited",
            code="RATE_LIMITED",
            detail=detail,
        )


class ValidationError(AppError):
    def __init__(self, detail: str = "Validation failed.") -> None:
        super().__init__(
            status_code=422,
            error="validation_error",
            code="VALIDATION_ERROR",
            detail=detail,
        )


class ModuleDisabledError(AppError):
    def __init__(self, module: str = "module") -> None:
        super().__init__(
            status_code=403,
            error="module_disabled",
            code="MODULE_DISABLED",
            detail=f"The {module} module is disabled on this server.",
        )


class TokenHaltedError(AppError):
    def __init__(self, detail: str = "This token is currently halted from trading.") -> None:
        super().__init__(
            status_code=403,
            error="token_halted",
            code="TOKEN_HALTED",
            detail=detail,
        )


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    """Global exception handler for AppError subclasses."""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": exc.error,
            "code": exc.code,
            "detail": exc.detail,
        },
    )
