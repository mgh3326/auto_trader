"""Deterministic labels for active order proposals.

This surface reads proposal rows and current quotes.  It has one deliberately
narrow optional action: a non-dry-run result labelled ``filled_or_expired`` is
passed to the existing proposal-void surface.  All other labels are reports.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.mcp_server.tooling import market_data_quotes, order_proposal_tools
from app.mcp_server.tooling.session_bootstrap_pack import _registered_names
from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals.state_machine import GROUP_STATES
from app.services.order_proposals.void_authorization import extract_loss_guard_violation
from app.services.trading_policy_service import policy_version_stamp

OPEN_PROPOSAL_STATES = (
    "proposed",
    "approved",
    "partially_submitted",
    "submitted",
)
LABEL_PRIORITY = (
    "filled_or_expired",
    "stale_policy",
    "guard_blocked",
    "dead_anchor",
    "keep",
)
VOID_VALUES = frozenset({"voided", "refused", "skipped_dry_run", "not_applicable"})

_MARKET_STORAGE = {"kr": "equity_kr", "us": "equity_us", "crypto": "crypto"}
_DEFAULT_ANCHOR_BAND_BPS = Decimal("100")


def _decimal(value: object) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value.normalize(), "f")


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


async def _list_for_state(lifecycle_state: str) -> dict[str, Any]:
    if lifecycle_state not in GROUP_STATES:
        raise ValueError(f"unsupported proposal lifecycle_state {lifecycle_state!r}")
    return await order_proposal_tools.order_proposal_list(
        limit=200,
        lifecycle_state=lifecycle_state,
    )


async def _active_rows(market: str) -> list[dict[str, Any]]:
    stored_market = _MARKET_STORAGE[market]
    rows_by_id: dict[str, dict[str, Any]] = {}
    for state in OPEN_PROPOSAL_STATES:
        response = await _list_for_state(state)
        if not response.get("success"):
            raise RuntimeError(str(response.get("error", "proposal_list_failed")))
        for row in response.get("proposals") or []:
            if not isinstance(row, dict) or row.get("market") != stored_market:
                continue
            proposal_id = row.get("proposal_id")
            if isinstance(proposal_id, str):
                rows_by_id[proposal_id] = row
    return [rows_by_id[proposal_id] for proposal_id in sorted(rows_by_id)]


async def _proposal_detail(proposal_id: str) -> tuple[Any, list[Any]]:
    async with AsyncSessionLocal() as session:
        return await OrderProposalsService(session).get_proposal(UUID(proposal_id))


def _stored_policy(source_asof: object) -> dict[str, str | None]:
    source = _mapping(source_asof)
    policy = _mapping(source.get("policy"))
    return {
        "version": _text(policy.get("version") or source.get("policy_version")),
        "content_hash": _text(
            policy.get("content_hash") or source.get("policy_content_hash")
        ),
    }


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _anchor(source_asof: object, rungs: list[Any]) -> tuple[Decimal | None, Decimal]:
    source = _mapping(source_asof)
    revalidation = _mapping(source.get("proposal_revalidate"))
    anchor = _mapping(revalidation.get("anchor"))
    price = _decimal(
        anchor.get("price")
        or revalidation.get("anchor_price")
        or source.get("anchor_price")
    )
    if price is None:
        for rung in sorted(rungs, key=lambda item: item.rung_index):
            price = _decimal(getattr(rung, "limit_price", None))
            if price is not None:
                break
    band = _decimal(
        anchor.get("band_bps")
        or revalidation.get("anchor_band_bps")
        or source.get("anchor_band_bps")
    )
    return price, band if band is not None and band >= 0 else _DEFAULT_ANCHOR_BAND_BPS


def _declared_guard(source_asof: object) -> dict[str, Any] | None:
    recorded = extract_loss_guard_violation(source_asof)
    if recorded is None:
        return None
    return {
        "rule": "loss_sell_guard",
        "value": recorded.get("loss_guard_error"),
        **recorded,
    }


def _prioritized_label(candidates: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    """Choose the sole report label from the explicit, ordered policy."""

    for label in LABEL_PRIORITY:
        if label in candidates:
            return {"label": label, "evidence": candidates[label]}
    raise ValueError("proposal revalidation produced no label candidate")


def _terminal_evidence(
    group: Any, rungs: list[Any], now: datetime
) -> dict[str, Any] | None:
    terminal_rungs = [
        rung for rung in rungs if getattr(rung, "state", None) in {"filled", "expired"}
    ]
    expired = group.valid_until is not None and now >= group.valid_until
    if not terminal_rungs and not expired:
        return None
    timestamps = [
        rung.updated_at.isoformat()
        for rung in terminal_rungs
        if getattr(rung, "updated_at", None) is not None
    ]
    return {
        "lifecycle_state": group.lifecycle_state,
        "rung_states": [
            rung.state for rung in sorted(rungs, key=lambda item: item.rung_index)
        ],
        "valid_until": group.valid_until.isoformat() if group.valid_until else None,
        "observed_at": now.isoformat(),
        "terminal_at": max(timestamps) if timestamps else None,
    }


async def _label(
    group: Any, rungs: list[Any], market: str, policy: dict[str, str]
) -> dict[str, Any]:
    now = now_kst()
    terminal = _terminal_evidence(group, rungs, now)
    stored_policy = _stored_policy(group.source_asof)
    candidates: dict[str, dict[str, Any]] = {}
    if terminal is not None:
        candidates["filled_or_expired"] = terminal
    if (
        stored_policy["content_hash"] is not None
        and stored_policy["content_hash"] != policy["content_hash"]
    ):
        candidates["stale_policy"] = {
            "proposal_policy": stored_policy,
            "current_policy": policy,
        }
    if candidates:
        return _prioritized_label(candidates)

    quote = await market_data_quotes._get_quote_impl(group.symbol, market)
    current_price = _decimal(quote.get("price") if isinstance(quote, dict) else None)
    anchor, band_bps = _anchor(group.source_asof, rungs)
    declared_guard = _declared_guard(group.source_asof)
    if declared_guard is not None:
        return _prioritized_label(
            {
                "guard_blocked": {
                    **declared_guard,
                    "current_price": _decimal_text(current_price),
                    "anchor": _decimal_text(anchor),
                }
            }
        )
    if current_price is None or current_price <= 0:
        return _prioritized_label(
            {
                "guard_blocked": {
                    "rule": "current_price_unavailable",
                    "current_price": _decimal_text(current_price),
                    "rung_count": len(rungs),
                }
            }
        )
    if anchor is None or anchor <= 0:
        return _prioritized_label(
            {
                "guard_blocked": {
                    "rule": "anchor_unavailable",
                    "current_price": _decimal_text(current_price),
                    "rung_count": len(rungs),
                }
            }
        )
    distance_bps = (current_price - anchor) / anchor * Decimal("10000")
    evidence = {
        "current_price": _decimal_text(current_price),
        "anchor": _decimal_text(anchor),
        "distance_bps": _decimal_text(distance_bps),
        "band_bps": _decimal_text(band_bps),
    }
    if abs(distance_bps) > band_bps:
        return _prioritized_label({"dead_anchor": evidence})
    return _prioritized_label({"keep": evidence})


async def _proposal_revalidate(
    market: str,
    proposal_ids: list[str] | None = None,
    dry_run: bool = True,
    confirm: bool = False,
    *,
    registered_tool_names: Callable[[], set[str] | Awaitable[set[str]]] | None,
) -> dict[str, Any]:
    if market not in _MARKET_STORAGE:
        return {"success": False, "error": "unknown_market"}
    registered = await _registered_names(registered_tool_names)
    if "order_proposal_list" not in registered:
        return {"state": "denied_by_profile", "tool": "order_proposal_list"}
    if not dry_run and "order_proposal_void" not in registered:
        return {"state": "denied_by_profile", "tool": "order_proposal_void"}
    if not dry_run and not confirm:
        return {"success": False, "error": "confirm_required"}
    if proposal_ids is not None and (
        not all(
            isinstance(proposal_id, str) and proposal_id for proposal_id in proposal_ids
        )
    ):
        return {"success": False, "error": "invalid_proposal_ids"}

    policy = policy_version_stamp()
    requested = set(proposal_ids) if proposal_ids is not None else None
    results: list[dict[str, Any]] = []
    for row in await _active_rows(market):
        proposal_id = row["proposal_id"]
        if requested is not None and proposal_id not in requested:
            continue
        group, rungs = await _proposal_detail(proposal_id)
        verdict = await _label(group, rungs, market, policy)
        result = {"proposal_id": proposal_id, **verdict, "void": "not_applicable"}
        if verdict["label"] == "filled_or_expired":
            if dry_run:
                result["void"] = "skipped_dry_run"
            else:
                void_result = await order_proposal_tools.order_proposal_void(
                    proposal_id,
                    reason="proposal revalidation found terminal evidence",
                )
                if void_result.get("success"):
                    result["void"] = "voided"
                else:
                    result["void"] = "refused"
                    result["error"] = str(void_result.get("error", "void_failed"))
        results.append(result)
    return {
        "success": True,
        "market": market,
        "dry_run": dry_run,
        "policy": policy,
        "label_priority": list(LABEL_PRIORITY),
        "count": len(results),
        "results": results,
    }


async def proposal_revalidate_impl(
    market: str,
    proposal_ids: list[str] | None = None,
    dry_run: bool = True,
    confirm: bool = False,
    *,
    registered_tool_names: Callable[[], set[str] | Awaitable[set[str]]],
) -> dict[str, Any]:
    """Label active proposals using only supplied profile capabilities."""

    return await _proposal_revalidate(
        market,
        proposal_ids,
        dry_run,
        confirm,
        registered_tool_names=registered_tool_names,
    )


__all__ = ["LABEL_PRIORITY", "OPEN_PROPOSAL_STATES", "proposal_revalidate_impl"]
