"""Typed MCP registration for the isolated ROB-845 paper façade."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

from pydantic import ConfigDict, TypeAdapter

from app.services.brokers.paper.composition import build_paper_execution_application
from app.services.brokers.paper.contracts import PaperOrderRequest

if TYPE_CHECKING:
    from fastmcp import FastMCP


PAPER_EXECUTION_TOOL_NAMES: set[str] = {
    "paper_execution_get_capabilities",
    "paper_execution_preview_order",
    "paper_execution_submit_order",
    "paper_execution_cancel_order",
    "paper_execution_get_order",
    "paper_execution_reconcile",
}


class PaperOrderToolInput(PaperOrderRequest):
    """JSON transport form of the strict canonical domain request.

    FastMCP validates an already-decoded Python mapping, so the domain model's
    strict enum/Decimal/datetime fields would reject their normal JSON string
    representations before a handler could run. This inherited transport model
    performs only JSON-to-typed coercion; every canonical field validator and
    the extra-field prohibition remain inherited from ``PaperOrderRequest``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=False)


class _PaperExecutionApplication(Protocol):
    async def preview(self, request: PaperOrderRequest) -> object: ...

    async def submit(self, request: PaperOrderRequest) -> object: ...

    async def cancel(self, request: PaperOrderRequest) -> object: ...

    async def get_order(self, request: PaperOrderRequest) -> object: ...

    async def reconcile(self, request: PaperOrderRequest) -> object: ...


ApplicationProvider = Callable[[], _PaperExecutionApplication]


def _default_application_provider() -> _PaperExecutionApplication:
    """Build the current fail-closed production composition.

    ROB-849 will inject the concrete provenance verifier. Until then, the
    canonical application deliberately has no verifier and therefore returns
    ``provenance_verifier_unavailable`` before adapter resolution.
    """
    return build_paper_execution_application(verifier=None)


def _model_to_json(value: object) -> dict[str, Any]:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return cast(dict[str, Any], model_dump(mode="json"))
    if is_dataclass(value) and not isinstance(value, type):
        normalized = _normalize_sets(asdict(value))
        return cast(
            dict[str, Any],
            TypeAdapter(Any).dump_python(normalized, mode="json"),
        )
    if isinstance(value, dict):
        return cast(dict[str, Any], value)
    raise TypeError(f"paper execution result is not serializable: {type(value)!r}")


def _normalize_sets(value: object) -> object:
    """Make frozen capability collections deterministic before JSON encoding."""
    if isinstance(value, dict):
        return {key: _normalize_sets(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(
            (_normalize_sets(item) for item in value),
            key=lambda item: repr(item),
        )
    if isinstance(value, (list, tuple)):
        return [_normalize_sets(item) for item in value]
    return value


def _capability_payload(venue: str | None) -> dict[str, Any]:
    from app.services.brokers.capabilities import (
        PAPER_BROKER_CAPABILITIES,
        get_paper_capabilities,
    )

    selected_venue = (venue or "").strip()
    if selected_venue:
        capability = get_paper_capabilities(selected_venue)
        if capability is None:
            return {
                "status": "blocked",
                "reason_code": "unsupported_venue",
                "venue": selected_venue,
                "capabilities": [],
            }
        return {
            "status": "ok",
            "reason_code": None,
            "venue": selected_venue,
            "capabilities": [_model_to_json(capability)],
        }

    capabilities = [
        _model_to_json(capability)
        for _, capability in sorted(
            PAPER_BROKER_CAPABILITIES.items(),
            key=lambda item: str(item[0]),
        )
    ]
    return {
        "status": "ok",
        "reason_code": None,
        "venue": None,
        "capabilities": capabilities,
    }


def register_paper_execution_tools(
    mcp: FastMCP,
    *,
    application_provider: ApplicationProvider | None = None,
) -> None:
    """Register the exact canonical paper-execution façade allowlist."""
    application = (application_provider or _default_application_provider)()

    @mcp.tool(
        name="paper_execution_get_capabilities",
        description=(
            "Read the exact ROB-845 Binance Demo and Alpaca Paper capability "
            "contracts. This tool never creates a broker client or mutates a ledger."
        ),
    )
    async def paper_execution_get_capabilities(
        venue: str | None = None,
    ) -> dict[str, Any]:
        return _capability_payload(venue)

    @mcp.tool(
        name="paper_execution_preview_order",
        description=(
            "Preview a canonical experiment paper order after trusted provenance "
            "verification. Origin and idempotency are server-owned."
        ),
    )
    async def paper_execution_preview_order(
        request: PaperOrderToolInput,
    ) -> dict[str, Any]:
        return _model_to_json(await application.preview(request))

    @mcp.tool(
        name="paper_execution_submit_order",
        description=(
            "Submit a canonical experiment paper order through its guarded venue "
            "adapter. Origin and idempotency are server-owned."
        ),
    )
    async def paper_execution_submit_order(
        request: PaperOrderToolInput,
    ) -> dict[str, Any]:
        return _model_to_json(await application.submit(request))

    @mcp.tool(
        name="paper_execution_cancel_order",
        description=(
            "Cancel a verified canonical paper order only when the venue advertises "
            "that capability."
        ),
    )
    async def paper_execution_cancel_order(
        request: PaperOrderToolInput,
    ) -> dict[str, Any]:
        return _model_to_json(await application.cancel(request))

    @mcp.tool(
        name="paper_execution_get_order",
        description="Read verified venue-native paper-order evidence.",
    )
    async def paper_execution_get_order(
        request: PaperOrderToolInput,
    ) -> dict[str, Any]:
        return _model_to_json(await application.get_order(request))

    @mcp.tool(
        name="paper_execution_reconcile",
        description=(
            "Reconcile a verified canonical paper order only when the venue "
            "advertises external reconciliation."
        ),
    )
    async def paper_execution_reconcile(
        request: PaperOrderToolInput,
    ) -> dict[str, Any]:
        return _model_to_json(await application.reconcile(request))


__all__ = [
    "ApplicationProvider",
    "PAPER_EXECUTION_TOOL_NAMES",
    "PaperOrderToolInput",
    "register_paper_execution_tools",
]
