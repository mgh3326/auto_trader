"""Read-only one-call session bootstrap pack.

The pack deliberately composes the existing tool implementations.  It does
not own a second projection or persistence path.
"""

from __future__ import annotations

import copy
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.mcp_server.tooling import (
    forecast_tools,
    operating_briefing,
    order_proposal_tools,
    portfolio_holdings,
    session_context_tools,
    trade_retrospective_tools,
    trading_policy_tools,
)
from app.mcp_server.tooling.account_modes import (
    apply_account_routing_metadata,
    normalize_account_mode,
)
from app.mcp_server.tooling.pending_orders_snapshot import (
    collect_pending_orders_snapshot,
)
from app.mcp_server.tooling.shared import logger

DEFAULT_SECTIONS = (
    "briefing",
    "holdings",
    "cash",
    "resting",
    "pending_retros",
    "due_forecasts",
    "policy",
    "recent_context",
)

SECTION_SOURCE_TOOLS: dict[str, str] = {
    "briefing": "get_operating_briefing",
    "holdings": "get_holdings",
    "cash": "get_available_capital",
    "resting": "order_proposal_list",
    "pending_retros": "trade_retrospective_pending",
    "due_forecasts": "forecast_resolve",
    "policy": "get_trading_policy",
    "recent_context": "session_context_get_recent",
}

_MAX_RESPONSE_BYTES = 65536
_POLICY_LANES = ("buy", "sell", "discovery")


def _source_state(response: dict[str, Any]) -> str:
    """Classify source freshness without changing the source value."""

    def has_stale_signal(value: object) -> bool:
        if isinstance(value, Mapping):
            if (
                value.get("unavailable_reason")
                or value.get("degraded")
                or value.get("stale")
            ):
                return True
            freshness = value.get("freshness_status")
            if freshness is not None and freshness not in {"db_read", "fresh"}:
                return True
            return any(has_stale_signal(item) for item in value.values())
        if isinstance(value, list):
            return any(has_stale_signal(item) for item in value)
        return False

    return "stale" if has_stale_signal(response) else "fresh"


def _missing(error: object) -> dict[str, str]:
    return {"error": str(error), "state": "missing"}


def _denied(tool: str) -> dict[str, str]:
    return {"state": "denied_by_profile", "tool": tool}


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _safe_elapsed_ms(started: float) -> int:
    try:
        return _elapsed_ms(started)
    except Exception:  # noqa: BLE001 -- timing is only observability
        return 0


async def _holdings(market: str) -> dict[str, Any]:
    # Match get_holdings' closure: normalize first, then attach routing metadata.
    routing = normalize_account_mode()
    response = await portfolio_holdings._get_holdings_impl(
        market=market,
        include_current_price=True,
        is_mock=routing.is_kis_mock,
        routing_account_mode=routing.account_mode,
    )
    response = apply_account_routing_metadata(response, routing)
    if market == "crypto":
        response["account_mode"] = portfolio_holdings.UPBIT_LIVE_PROVENANCE
    return response


async def _cash() -> dict[str, Any]:
    # Match get_available_capital's closure: normalize first, then attach metadata.
    routing = normalize_account_mode()
    response = await portfolio_holdings._get_available_capital_impl(
        is_mock=routing.is_kis_mock,
    )
    return apply_account_routing_metadata(response, routing)


async def _resting(
    *,
    market: str,
    account_scope: str,
    briefing: dict[str, Any] | None,
    briefing_requested: bool,
    briefing_tool_registered: bool,
) -> dict[str, Any]:
    pending, resting = await _proposal_lists()
    live_orders: dict[str, Any]
    if briefing is not None:
        pending_orders = briefing.get("pending_orders")
        live_orders = (
            dict(pending_orders) if isinstance(pending_orders, Mapping) else {}
        )
    elif briefing_requested:
        # The requested briefing owns this fan-out.  If it failed, do not
        # recreate its pending-order read while preparing another section.
        live_orders = {}
    elif not briefing_tool_registered:
        live_orders = _denied("get_operating_briefing")
    else:
        async with AsyncSessionLocal() as db:
            snapshot = await collect_pending_orders_snapshot(
                db,
                market=market,
                account_scope=account_scope,
            )
        live_orders = {
            "count": len(snapshot.orders or []),
            "orders": snapshot.orders or [],
            "unavailable_reason": snapshot.unavailable_reason,
        }

    proposals = [
        *list(pending.get("proposals") or []),
        *list(resting.get("proposals") or []),
    ]
    return {
        "success": bool(pending.get("success")) and bool(resting.get("success")),
        "count": len(proposals),
        "proposals": proposals,
        "live_orders": live_orders,
    }


async def _proposal_lists() -> tuple[dict[str, Any], dict[str, Any]]:
    pending = await order_proposal_tools.order_proposal_list(lifecycle_state="pending")
    resting = await order_proposal_tools.order_proposal_list(lifecycle_state="resting")
    return pending, resting


async def _policy(market: str) -> dict[str, Any]:
    policies = {
        lane: await trading_policy_tools.get_trading_policy(market=market, lane=lane)
        for lane in _POLICY_LANES
    }
    first = policies[_POLICY_LANES[0]]
    if not all(item.get("success") for item in policies.values()):
        failed = next(item for item in policies.values() if not item.get("success"))
        return failed
    return {
        "success": True,
        "version": first["version"],
        "content_hash": first["content_hash"],
        "policies": policies,
    }


async def _section_source(
    section: str,
    *,
    market: str,
    account_scope: str,
    briefing: dict[str, Any] | None,
    briefing_requested: bool,
    briefing_tool_registered: bool,
) -> dict[str, Any]:
    if section == "briefing":
        return await operating_briefing.get_operating_briefing_impl(market=market)
    if section == "holdings":
        return await _holdings(market)
    if section == "cash":
        return await _cash()
    if section == "resting":
        return await _resting(
            market=market,
            account_scope=account_scope,
            briefing=briefing,
            briefing_requested=briefing_requested,
            briefing_tool_registered=briefing_tool_registered,
        )
    if section == "pending_retros":
        return await trade_retrospective_tools.trade_retrospective_pending(limit=20)
    if section == "due_forecasts":
        return await forecast_tools.forecast_resolve(dry_run=True)
    if section == "policy":
        return await _policy(market)
    if section == "recent_context":
        return await session_context_tools.session_context_get_recent(
            market=market,
            account_scope=account_scope,
            limit=10,
        )
    raise AssertionError(f"unreachable section {section}")


def _truncate_list(value: list[Any], limit: int) -> tuple[list[Any], int | None]:
    if len(value) <= limit:
        return value, None
    return value[:limit], len(value)


def _compact_section(
    section: str, value: dict[str, Any]
) -> tuple[dict[str, Any], int | None]:
    """Apply the deterministic compact limits to a copied section value."""

    result = copy.deepcopy(value)
    candidates: list[tuple[dict[str, Any], str, int]] = []
    if section == "holdings":
        candidates.append((result, "positions", 20))
        for account in result.get("accounts") or []:
            if isinstance(account, dict):
                candidates.append((account, "positions", 20))
    elif section == "resting":
        candidates.append((result, "proposals", 20))
    elif section == "pending_retros":
        candidates.extend((result, key, 20) for key in ("pending", "results", "items"))
    elif section == "recent_context":
        candidates.append((result, "entries", 10))
    elif section == "due_forecasts":
        candidates.append((result, "results", 20))
    elif section == "briefing":
        candidates.extend(
            (result, key, 10)
            for key in (
                "accounts",
                "top_movers",
                "active_watches",
                "analysis_artifacts",
            )
        )
        for nested in result.values():
            if isinstance(nested, dict):
                candidates.extend((nested, key, 10) for key in tuple(nested))

    truncated_from: int | None = None
    for container, key, limit in candidates:
        values = container.get(key)
        if not isinstance(values, list):
            continue
        compacted, original_count = _truncate_list(values, limit)
        if original_count is not None:
            container[key] = compacted
            truncated_from = max(truncated_from or 0, original_count)
    return result, truncated_from


def _serialized_bytes(response: dict[str, Any]) -> int:
    return len(json.dumps(response, ensure_ascii=False).encode("utf-8"))


def _finalize_bytes(response: dict[str, Any]) -> int:
    size = _serialized_bytes(response)
    for _ in range(3):
        response["meta"]["bytes"] = size
        next_size = _serialized_bytes(response)
        if next_size == size:
            break
        size = next_size
    response["meta"]["bytes"] = size
    return size


async def _registered_names(
    resolver: Callable[[], set[str] | Awaitable[set[str]]] | None,
) -> set[str]:
    if resolver is None:
        return set()
    try:
        resolved = resolver()
        if isinstance(resolved, Awaitable):
            resolved = await resolved
        names = set(resolved)
        if not all(isinstance(name, str) for name in names):
            raise TypeError("registered tool names must be strings")
        return names
    except Exception:  # noqa: BLE001 -- inability to attest must deny, not leak
        return set()


async def _session_bootstrap_pack(
    market: str,
    include: list[str] | None,
    compact: bool,
    *,
    registered_tool_names: Callable[[], set[str] | Awaitable[set[str]]] | None,
) -> dict[str, Any]:
    if market not in {"kr", "us", "crypto"}:
        return {"success": False, "error": "unknown_market"}
    requested = list(DEFAULT_SECTIONS if include is None else include)
    unknown = [section for section in requested if section not in DEFAULT_SECTIONS]
    if unknown:
        return {"success": False, "error": "unknown_section", "unknown": unknown}

    try:
        started = time.monotonic()
    except Exception:  # noqa: BLE001 -- pack observability cannot block reads
        started = 0.0
    account_scope = operating_briefing._default_account_scope(market, None)
    registered = await _registered_names(registered_tool_names)
    resolver_unavailable = registered == set()
    sections: dict[str, dict[str, Any]] = {}
    section_meta: dict[str, dict[str, Any]] = {}
    briefing: dict[str, Any] | None = None

    for section in requested:
        source_tool = SECTION_SOURCE_TOOLS[section]
        if source_tool not in registered:
            sections[section] = _denied(source_tool)
            section_meta[section] = {
                "source": source_tool,
                "state": "denied_by_profile",
                "elapsed_ms": 0,
            }
            continue
        section_started = started
        try:
            section_started = time.monotonic()
            value = await _section_source(
                section,
                market=market,
                account_scope=account_scope,
                briefing=briefing,
                briefing_requested="briefing" in requested,
                briefing_tool_registered="get_operating_briefing" in registered,
            )
            logger.info("session_bootstrap_pack section=%s", section)
            elapsed = _elapsed_ms(section_started)
            if not value.get("success", True):
                raise RuntimeError(str(value.get("error", "source_unsuccessful")))
            state = _source_state(value)
            sections[section] = value
            section_meta[section] = {
                "source": source_tool,
                "state": state,
                "elapsed_ms": elapsed,
            }
            if section == "briefing":
                briefing = value
        except Exception as exc:  # noqa: BLE001 -- per-section isolation is contractual
            try:
                logger.exception("session_bootstrap_pack section failed: %s", section)
            except Exception:  # noqa: BLE001 -- observability must not break the pack
                pass
            sections[section] = _missing(exc)
            section_meta[section] = {
                "source": source_tool,
                "state": "missing",
                "elapsed_ms": _safe_elapsed_ms(section_started),
            }

    response: dict[str, Any] = {
        "success": True,
        "market": market,
        "compact": False,
        "sections": sections,
        "meta": {
            "as_of": now_kst().isoformat(),
            "sections": section_meta,
            "elapsed_ms": _safe_elapsed_ms(started),
            "bytes": 0,
            "compact_downgraded": False,
        },
    }
    if resolver_unavailable:
        response["meta"]["resolver_unavailable"] = True

    should_compact = compact or _finalize_bytes(response) > _MAX_RESPONSE_BYTES
    if should_compact:
        compact_sections: dict[str, dict[str, Any]] = {}
        for section, value in sections.items():
            if value.get("state") in {"missing", "denied_by_profile"}:
                compact_sections[section] = value
                continue
            compacted, truncated_from = _compact_section(section, value)
            compact_sections[section] = compacted
            if truncated_from is not None:
                section_meta[section]["truncated_from"] = truncated_from
        response["sections"] = compact_sections
        response["compact"] = True
        response["meta"]["compact_downgraded"] = not compact
        response["meta"]["elapsed_ms"] = _safe_elapsed_ms(started)
        if _finalize_bytes(response) > _MAX_RESPONSE_BYTES:
            response["meta"]["over_limit"] = True
    _finalize_bytes(response)
    return response


async def session_bootstrap_pack_impl(
    market: str,
    include: list[str] | None = None,
    compact: bool = False,
) -> dict[str, Any]:
    """Compose the read-only pack for direct/internal callers."""

    return await _session_bootstrap_pack(
        market,
        include,
        compact,
        registered_tool_names=None,
    )


__all__ = [
    "DEFAULT_SECTIONS",
    "SECTION_SOURCE_TOOLS",
    "session_bootstrap_pack_impl",
]
