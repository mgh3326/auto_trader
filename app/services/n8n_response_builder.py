"""Helpers for building n8n error JSONResponse objects.

Only the JSONResponse(status_code=500) construction is extracted here.
Error payload (Pydantic model) construction remains in each endpoint
because field sets differ per response type.
"""

from __future__ import annotations

from fastapi.responses import JSONResponse
from pydantic import BaseModel


def n8n_error_response(payload: BaseModel) -> JSONResponse:
    """Return a 500 JSONResponse from a pre-built Pydantic error payload.

    The caller is responsible for constructing the payload with
    success=False and errors=[...] before calling this helper.
    """
    return JSONResponse(status_code=500, content=payload.model_dump())
