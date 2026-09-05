"""Application handlers for the ROB-848 paper-validation MCP tools."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from sqlalchemy.inspection import inspect as sqlalchemy_inspect

from app.services.paper_validation.contracts import (
    ActorIdentity,
    ActorRole,
    ActorRoleProvider,
)


class ConfiguredActorRoleProvider(ActorRoleProvider):
    """Resolve only authenticated request identities from operator configuration."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = dict(mapping)

    async def resolve(self, caller_id: str) -> ActorIdentity:
        try:
            role = ActorRole(self._mapping[caller_id])
        except (KeyError, ValueError) as exc:
            raise LookupError("caller role mapping unavailable") from exc
        return ActorIdentity(actor_id=caller_id, role=role)


def jsonable(value: object) -> Any:
    """Convert application results, including ORM rows, to MCP-safe values."""
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    try:
        mapper = sqlalchemy_inspect(type(value))
    except Exception:
        return value
    return {
        column.key: jsonable(getattr(value, column.key)) for column in mapper.columns
    }


__all__ = ["ConfiguredActorRoleProvider", "jsonable"]
