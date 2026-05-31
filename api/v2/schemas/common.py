from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PaginatedResponse(BaseModel, Generic[T]):
    """Generic paginated response wrapper."""

    items: list[Any] = Field(..., description="List of result items.")
    total: int = Field(..., description="Total number of items matching the query.")
    limit: int = Field(..., description="Maximum items returned per page.")
    offset: int = Field(..., description="Offset from the start of the result set.")



class SuccessResponse(BaseModel):
    """Generic success acknowledgement."""

    success: bool = True
    message: str = Field(..., description="Human-readable success message.")


