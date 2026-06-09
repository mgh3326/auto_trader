"""ROB-376 — read-only intraday delta computation for investment reports.

Deterministic baseline-vs-live deltas (target/stop touch, per-symbol holdings P/L,
index move) for the next/intraday report. No DB writes, no broker/watch mutation,
no in-process LLM. Every signal is fail-open: one signal's failure never kills the
others.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _created_at_sort_key(raw: Any) -> tuple[bool, datetime]:
    """Sortable key for a journal entry's ``created_at`` isoformat string.

    Returns ``(True, aware_datetime)`` when parseable; ``(False, _EPOCH)`` when
    missing/unparseable so dated journals always rank above undated ones and two
    undated entries compare equal (stable, keeps the first seen).
    """
    if not raw or not isinstance(raw, str):
        return (False, _EPOCH)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return (False, _EPOCH)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return (True, parsed)


def _scope_from_items(items: Iterable[Any]) -> dict[str, set[str] | None]:
    """Build the report's levels scope: ``{symbol: {sides} | None}``.

    ``None`` means "any side" — used when any item for that symbol has a NULL
    side (watch/risk items, or legacy action rows). A symbol with only
    side-bearing items maps to the union of those sides. Symbols without a
    symbol value are skipped. (ROB-454)
    """
    scope: dict[str, set[str] | None] = {}
    for item in items:
        symbol = getattr(item, "symbol", None)
        if not symbol:
            continue
        side = getattr(item, "side", None)
        if symbol not in scope:
            scope[symbol] = None if side is None else {side}
            continue
        current = scope[symbol]
        if current is None:
            continue  # already "any side"
        if side is None:
            scope[symbol] = None  # widen to "any side"
        else:
            current.add(side)
    return scope


def _is_finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _near(current: Any, level: Any, near_pct: float) -> bool:
    """True when ``current`` is within ``near_pct`` percent of ``level``."""
    if not _is_finite_number(current) or not _is_finite_number(level) or level == 0:
        return False
    return abs(current - level) / abs(level) * 100 <= near_pct


def _levels_delta(
    journal_result: Mapping[str, Any],
    scope: Mapping[str, set[str] | None],
    *,
    near_pct: float,
    baseline_created_at: datetime | None = None,
) -> dict[str, Any]:
    """Project the journal's already-computed live enrichment into a delta block.

    Reuses ``target_reached`` / ``stop_reached`` / ``pnl_pct_live`` (computed by
    ``get_trade_journal(enrich_live=True)``); computes per-entry ``near_*`` flags
    here.

    ROB-454 scoping — there is no item↔journal link, so the report's position is
    approximated:

    * In-scope: a journal entry's ``symbol`` must be a ``scope`` key AND
      (``scope[symbol]`` is ``None`` ⇒ any side, else the entry's ``side`` must be
      in that set). An empty ``scope`` keeps every symbol (no filter).
    * Stale cutoff: when ``baseline_created_at`` is given, journals created
      strictly before it are dropped — they predate this baseline report and
      belong to pre-existing positions, not to this report's delta. Entries with
      a missing/unparseable ``created_at`` are kept (fail-open; never fabricate an
      exclusion).
    * Dedup: when multiple in-scope journals share a ``(symbol, side)``, only the
      most-recent (by ``created_at``) is counted, so a stale duplicate does not
      inflate ``stop_hit`` / ``target_hit``.
    """
    selected: dict[tuple[Any, Any], dict[str, Any]] = {}
    for entry in journal_result.get("entries") or []:
        symbol = entry.get("symbol")
        side = entry.get("side")
        if scope:
            if symbol not in scope:
                continue
            allowed = scope[symbol]
            if allowed is not None and side not in allowed:
                continue
        created_key = _created_at_sort_key(entry.get("created_at"))
        if baseline_created_at is not None and created_key[0]:
            if created_key[1] < baseline_created_at:
                continue  # predates the baseline report — stale (ROB-454)
        key = (symbol, side)
        incumbent = selected.get(key)
        if incumbent is None or created_key > _created_at_sort_key(
            incumbent.get("created_at")
        ):
            selected[key] = entry

    entries: list[dict[str, Any]] = []
    near_target = near_stop = target_hit = stop_hit = 0
    for entry in sorted(
        selected.values(),
        key=lambda e: ((e.get("symbol") or ""), (e.get("side") or "")),
    ):
        current = entry.get("current_price")
        target = entry.get("target_price")
        stop = entry.get("stop_loss")
        is_target_reached = bool(entry.get("target_reached"))
        is_stop_reached = bool(entry.get("stop_reached"))
        is_near_target = _near(current, target, near_pct) and not is_target_reached
        is_near_stop = _near(current, stop, near_pct) and not is_stop_reached
        near_target += int(is_near_target)
        near_stop += int(is_near_stop)
        target_hit += int(is_target_reached)
        stop_hit += int(is_stop_reached)
        entries.append(
            {
                "symbol": entry.get("symbol"),
                "side": entry.get("side"),
                "journal_id": entry.get("id"),
                "created_at": entry.get("created_at"),
                "target_price": target,
                "stop_loss": stop,
                "current_price": current,
                "pnl_pct_live": entry.get("pnl_pct_live"),
                "target_reached": entry.get("target_reached"),
                "stop_reached": entry.get("stop_reached"),
                "near_target": is_near_target,
                "near_stop": is_near_stop,
            }
        )
    return {
        "entries": entries,
        "summary": {
            "near_target": near_target,
            "near_stop": near_stop,
            "target_hit": target_hit,
            "stop_hit": stop_hit,
        },
    }


def _baseline_pnl_from_bundle_pairs(
    pairs: list[tuple[Any, Any]],
) -> dict[str, float] | None:
    """Extract ``{ticker: pnl_rate}`` from the bundle's ``portfolio`` snapshot.

    Returns ``None`` when no ``portfolio`` snapshot is present (so the caller can
    record ``baseline_snapshot_absent`` rather than fabricating an empty baseline).
    Holdings without a finite ``pnl_rate`` are skipped (missing != zero).
    """
    for _item, snapshot in pairs:
        if getattr(snapshot, "snapshot_kind", None) != "portfolio":
            continue
        payload = getattr(snapshot, "payload_json", None) or {}
        out: dict[str, float] = {}
        for holding in payload.get("holdings") or []:
            ticker = holding.get("ticker")
            rate = holding.get("pnl_rate")
            if ticker is not None and _is_finite_number(rate):
                out[str(ticker)] = float(rate)
        return out
    return None


def _live_pnl_by_symbol(holdings_result: Mapping[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for account in holdings_result.get("accounts") or []:
        for position in account.get("positions") or []:
            symbol = position.get("symbol")
            rate = position.get("profit_rate")
            if symbol is not None and _is_finite_number(rate):
                out[str(symbol)] = float(rate)
    return out


def _holdings_pnl_delta(
    baseline_pnl: Mapping[str, float],
    holdings_result: Mapping[str, Any],
) -> dict[str, Any]:
    """Per-symbol live P/L vs baseline P/L. Only symbols present on BOTH sides get
    an entry (missing != zero); one-sided symbols are counted in the summary."""
    live_pnl = _live_pnl_by_symbol(holdings_result)
    baseline_keys = set(baseline_pnl)
    live_keys = set(live_pnl)
    both = baseline_keys & live_keys
    entries: list[dict[str, Any]] = []
    for symbol in sorted(both):
        base = baseline_pnl[symbol]
        live = live_pnl[symbol]
        entries.append(
            {
                "symbol": symbol,
                "baseline_pnl_pct": base,
                "live_pnl_pct": live,
                "delta_pp": round(live - base, 6),
            }
        )
    return {
        "entries": entries,
        "summary": {
            "symbols_compared": len(both),
            "symbols_baseline_only": len(baseline_keys - live_keys),
            "symbols_live_only": len(live_keys - baseline_keys),
        },
    }


def _baseline_indices(market_snapshot: Any) -> dict[str, Any] | None:
    """Return the frozen ``baseline.indices`` dict (keyed by index symbol), or
    ``None`` when the snapshot is the ``unavailable`` shape or lacks indices."""
    if not isinstance(market_snapshot, Mapping):
        return None
    if market_snapshot.get("status") == "unavailable":
        return None
    baseline = market_snapshot.get("baseline")
    if not isinstance(baseline, Mapping):
        return None
    indices = baseline.get("indices")
    if not isinstance(indices, Mapping):
        return None
    return dict(indices)


def _index_delta(
    baseline_indices: Mapping[str, Any],
    market_index_result: Mapping[str, Any],
) -> dict[str, Any]:
    """Live index value vs frozen baseline value, per index symbol. ``change_pct``
    is computed only when both values are finite and baseline != 0; otherwise it is
    ``null`` (never fabricated). Indices absent from the live response carry a
    ``null`` ``live_value``."""
    live_by_symbol: dict[str, Any] = {}
    for index in market_index_result.get("indices") or []:
        symbol = index.get("symbol")
        if symbol is not None:
            live_by_symbol[str(symbol)] = index.get("current")
    entries: list[dict[str, Any]] = []
    for symbol, baseline in baseline_indices.items():
        baseline_value = (
            baseline.get("current") if isinstance(baseline, Mapping) else None
        )
        live_value = live_by_symbol.get(symbol)
        change_pct: float | None = None
        if (
            _is_finite_number(baseline_value)
            and _is_finite_number(live_value)
            and baseline_value != 0
        ):
            change_pct = (live_value - baseline_value) / baseline_value * 100
        entries.append(
            {
                "index_symbol": symbol,
                "baseline_value": baseline_value,
                "live_value": live_value,
                "change_pct": change_pct,
            }
        )
    return {"entries": entries}


def _reason(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


class DeltaService:
    """Orchestrates the three read-only deltas. I/O is injectable so the logic is
    unit-testable without a DB or live network. Defaults wire the real loaders/tools."""

    def __init__(
        self,
        session: Any,
        *,
        baseline_loader: Callable[[UUID], Awaitable[dict[str, Any] | None]]
        | None = None,
        journal_fn: Callable[..., Awaitable[dict[str, Any]]] | None = None,
        holdings_fn: Callable[..., Awaitable[dict[str, Any]]] | None = None,
        market_index_fn: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self._session = session
        self._baseline_loader = baseline_loader
        self._journal_fn = journal_fn
        self._holdings_fn = holdings_fn
        self._market_index_fn = market_index_fn

    async def compute_delta(
        self,
        report_uuid: UUID | str,
        *,
        near_pct: float = 1.0,
        account_type: str = "live",
        computed_at_kst: str | None = None,
    ) -> dict[str, Any]:
        parsed = (
            report_uuid if isinstance(report_uuid, UUID) else UUID(str(report_uuid))
        )
        loader = self._baseline_loader or self._default_baseline_loader
        baseline = await loader(parsed)
        if baseline is None:
            return {"success": False, "error": "baseline_not_found"}

        market = baseline["market"]
        scope = baseline.get("scope")
        if scope is None:
            # Back-compat: derive an any-side scope from a plain symbols set.
            scope = dict.fromkeys(baseline.get("symbols") or set())
        baseline_created_at = baseline.get("baseline_created_at")
        market_snapshot = baseline["market_snapshot"]
        baseline_pnl = baseline["baseline_pnl"]
        unavailable: dict[str, str] = {}

        levels_delta: dict[str, Any] | None = None
        try:
            journal_fn = self._journal_fn or _default_journal_fn
            journal_result = await journal_fn(account_type=account_type, market=market)
            levels_delta = _levels_delta(
                journal_result,
                scope,
                near_pct=near_pct,
                baseline_created_at=baseline_created_at,
            )
        except Exception as exc:  # noqa: BLE001 — fail-open per signal
            logger.info("levels_delta failed: %r", exc)
            unavailable["levels"] = _reason(exc)

        holdings_pnl_delta: dict[str, Any] | None = None
        if baseline_pnl is None:
            unavailable["holdings"] = "baseline_snapshot_absent"
        else:
            try:
                holdings_fn = self._holdings_fn or _default_holdings_fn
                holdings_result = await holdings_fn(market=market)
                holdings_pnl_delta = _holdings_pnl_delta(baseline_pnl, holdings_result)
            except Exception as exc:  # noqa: BLE001 — fail-open per signal
                logger.info("holdings_pnl_delta failed: %r", exc)
                unavailable["holdings"] = _reason(exc)

        index_delta: dict[str, Any] | None = None
        baseline_indices = _baseline_indices(market_snapshot)
        if baseline_indices is None:
            unavailable["index"] = "baseline_snapshot_absent"
        else:
            try:
                market_index_fn = self._market_index_fn or _default_market_index_fn
                index_result = await market_index_fn()
                index_delta = _index_delta(baseline_indices, index_result)
            except Exception as exc:  # noqa: BLE001 — fail-open per signal
                logger.info("index_delta failed: %r", exc)
                unavailable["index"] = _reason(exc)

        out: dict[str, Any] = {
            "success": True,
            "baseline_report_uuid": str(parsed),
            "market": market,
            "levels_delta": levels_delta,
            "holdings_pnl_delta": holdings_pnl_delta,
            "index_delta": index_delta,
        }
        if computed_at_kst is not None:
            out["computed_at_kst"] = computed_at_kst
        if unavailable:
            out["unavailable"] = unavailable
        return out

    async def _default_baseline_loader(
        self, report_uuid: UUID
    ) -> dict[str, Any] | None:
        from app.services.investment_reports.query_service import (
            InvestmentReportQueryService,
        )
        from app.services.investment_snapshots.repository import (
            InvestmentSnapshotsRepository,
        )

        query_service = InvestmentReportQueryService(self._session)
        bundle = await query_service.get_bundle(report_uuid)
        if bundle is None:
            return None
        report = bundle["report"]
        scope = _scope_from_items(bundle.get("items") or [])
        baseline_pnl: dict[str, float] | None = None
        bundle_uuid = getattr(report, "snapshot_bundle_uuid", None)
        if bundle_uuid is not None:
            snapshots_repo = InvestmentSnapshotsRepository(self._session)
            snapshot_bundle = await snapshots_repo.get_bundle_by_uuid(bundle_uuid)
            if snapshot_bundle is not None:
                pairs = await snapshots_repo.list_bundle_items_with_snapshots(
                    snapshot_bundle.id
                )
                baseline_pnl = _baseline_pnl_from_bundle_pairs(pairs)
        return {
            "market": report.market,
            "symbols": set(scope),
            "scope": scope,
            "baseline_created_at": getattr(report, "created_at", None),
            "market_snapshot": report.market_snapshot or {},
            "baseline_pnl": baseline_pnl,
        }


async def _default_journal_fn(*, account_type: str, market: str) -> dict[str, Any]:
    from app.mcp_server.tooling.trade_journal_tools import get_trade_journal

    return await get_trade_journal(
        enrich_live=True, account_type=account_type, market=market
    )


async def _default_holdings_fn(*, market: str) -> dict[str, Any]:
    from app.mcp_server.tooling.portfolio_holdings import _get_holdings_impl

    return await _get_holdings_impl(market=market, include_current_price=True)


async def _default_market_index_fn() -> dict[str, Any]:
    from app.mcp_server.tooling.fundamentals._market_index import (
        handle_get_market_index,
    )

    return await handle_get_market_index()
