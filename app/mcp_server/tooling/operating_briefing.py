"""Read-only operating briefing MCP tools for ROB-517."""

from __future__ import annotations

from typing import Any

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.mcp_server.tooling.pending_orders_snapshot import (
    DEFAULT_PENDING_ORDERS_ACCOUNT_SCOPE,
    collect_pending_orders_snapshot,
)
from app.mcp_server.tooling.portfolio_cash import get_account_costs_setting
from app.mcp_server.tooling.portfolio_holdings import _get_holdings_impl
from app.schemas.analysis_artifact import AnalysisArtifactMeta
from app.schemas.investment_reports import (
    ActiveWatchesListResponse,
    InvestmentWatchAlertResponse,
    OperatingBriefingResponse,
)
from app.schemas.session_context import SessionContextResponse
from app.services.account_routing import compact_cost_profile
from app.services.analysis_artifact import AnalysisArtifactService
from app.services.investment_reports.query_service import (
    InvestmentReportQueryService,
    _advisory_draft_profiles,
)
from app.services.investment_reports.repository import InvestmentReportsRepository
from app.services.session_context import SessionContextService


def _normalize_watch_symbol(symbol: str | None, market: str | None) -> str | None:
    if symbol is None:
        return None
    stripped = str(symbol).strip()
    if not stripped:
        return None
    if market in {"us", "crypto"}:
        return stripped.upper()
    return stripped


async def list_active_watches_impl(
    market: str | None = None,
    symbol: str | None = None,
    include_expired_status_rows: bool = False,
    limit: int = 100,
) -> dict[str, Any]:
    as_of = now_kst()
    capped_limit = max(1, min(int(limit), 250))
    normalized_symbol = _normalize_watch_symbol(symbol, market)
    async with AsyncSessionLocal() as db:
        repo = InvestmentReportsRepository(db)
        rows = await repo.list_active_alerts(
            market=market,
            symbol=normalized_symbol,
            valid_at=as_of,
            include_expired_status_rows=include_expired_status_rows,
            limit=capped_limit,
        )
        response = ActiveWatchesListResponse(
            count=len(rows),
            as_of=as_of,
            filters={
                "market": market,
                "symbol": normalized_symbol,
                "include_expired_status_rows": include_expired_status_rows,
                "limit": capped_limit,
            },
            active_watches=[
                InvestmentWatchAlertResponse.model_validate(row) for row in rows
            ],
        )
    return response.model_dump(mode="json", by_alias=True)


def _default_account_scope(market: str, account_scope: str | None) -> str:
    if account_scope:
        return account_scope
    default = DEFAULT_PENDING_ORDERS_ACCOUNT_SCOPE.get(market)
    if default is None:
        raise ValueError(f"unsupported market for operating briefing: {market}")
    return default


def _holdings_kwargs(
    market: str, account_scope: str, include_current_price: bool
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "market": market,
        "include_current_price": include_current_price,
        "routing_account_mode": account_scope,
    }
    if account_scope == "kis_mock":
        kwargs["is_mock"] = True
    if account_scope == "upbit_live":
        kwargs["account"] = "upbit"
    if account_scope == "alpaca_paper":
        kwargs["account"] = "paper"
    return kwargs


def _flatten_positions(holdings: dict[str, Any]) -> list[dict[str, Any]]:
    positions: list[dict[str, Any]] = []
    for account in holdings.get("accounts") or []:
        account_name = account.get("account")
        for position in account.get("positions") or []:
            row = dict(position)
            row.setdefault("account", account_name)
            positions.append(row)
    return positions


def _account_routability(
    holdings: dict[str, Any],
    *,
    market: str,
    account_costs: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Compact per-account routability summary for the briefing (ROB-541).

    Surfaces ``account_mode`` (ROB-357 provenance label) and the authoritative
    ``order_routable`` flag per account so a toss-held (reference-only) symbol is
    distinguishable from a kis_live-sellable one without dumping full positions.
    """
    accounts: list[dict[str, Any]] = []
    for account in holdings.get("accounts") or []:
        row = {
            "account": account.get("account"),
            "account_name": account.get("account_name"),
            "account_mode": account.get("account_mode"),
            "order_routable": account.get("order_routable"),
            "position_count": len(account.get("positions") or []),
        }
        if market in {"kr", "us"}:
            profile = compact_cost_profile(
                str(account.get("account") or ""),
                market,  # type: ignore[arg-type]
                account_costs,
            )
            if profile is not None:
                row["cost_profile"] = profile
        accounts.append(row)
    return accounts


def _top_movers(holdings: dict[str, Any], *, limit: int = 5) -> list[dict[str, Any]]:
    candidates = []
    for position in _flatten_positions(holdings):
        profit_rate = position.get("profit_rate")
        if profit_rate is None:
            continue
        try:
            abs_rate = abs(float(profit_rate))
        except (TypeError, ValueError):
            continue
        candidates.append((abs_rate, position))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [
        {
            "symbol": row.get("symbol"),
            "account": row.get("account"),
            "profit_rate": row.get("profit_rate"),
            "profit_loss": row.get("profit_loss"),
            "evaluation_amount": row.get("evaluation_amount"),
        }
        for _, row in candidates[:limit]
    ]


async def _latest_report_summary(
    db: Any,
    *,
    market: str,
    account_scope: str,
) -> dict[str, Any] | None:
    service = InvestmentReportQueryService(db)
    report = await service.latest_report(
        market=market,
        account_scope=account_scope,
        created_by_profiles=_advisory_draft_profiles(),
        exclude_statuses={"superseded"},
    )
    if report is None:
        return None
    bundle = await service.get_bundle(report.report_uuid)
    items = bundle["items"] if bundle is not None else []
    by_status: dict[str, int] = {}
    top_items: list[dict[str, Any]] = []
    for item in items:
        status = str(getattr(item, "status", "unknown"))
        by_status[status] = by_status.get(status, 0) + 1
        if len(top_items) < 5:
            top_items.append(
                {
                    "item_uuid": str(item.item_uuid),
                    "symbol": item.symbol,
                    "item_kind": item.item_kind,
                    "intent": item.intent,
                    "status": item.status,
                    "rationale": item.rationale,
                }
            )
    return {
        "report_uuid": str(report.report_uuid),
        "title": report.title,
        "status": report.status,
        "created_by_profile": report.created_by_profile,
        "created_at": report.created_at.isoformat() if report.created_at else None,
        "items": {
            "total": len(items),
            "by_status": by_status,
            "top": top_items,
        },
    }


async def _recent_session_context(
    db: Any,
    *,
    market: str,
    account_scope: str,
    limit: int,
) -> dict[str, Any]:
    service = SessionContextService(db)
    rows = await service.get_recent(
        market=market,  # type: ignore[arg-type]
        account_scope=account_scope,  # type: ignore[arg-type]
        limit=max(1, min(int(limit), 100)),
    )
    return {
        "count": len(rows),
        "entries": [
            SessionContextResponse.model_validate(row).model_dump(mode="json")
            for row in rows
        ],
    }


async def _recent_analysis_artifacts(
    db: Any,
    *,
    market: str,
    limit: int = 10,
) -> dict[str, Any]:
    """Metadata-only recent valid artifacts (ROB-637 briefing surfacing).

    Payloads are intentionally excluded — a new session learns what analysis
    already exists and fetches bodies via analysis_artifact_get on demand.
    """
    service = AnalysisArtifactService(db)
    rows = await service.list_artifacts(
        market=market,  # type: ignore[arg-type]
        include_stale=False,
        limit=max(1, min(int(limit), 20)),
    )
    return {
        "count": len(rows),
        "artifacts": [
            AnalysisArtifactMeta.model_validate(row).model_dump(mode="json")
            for row in rows
        ],
    }


def _section_unavailable_reason(section: str, exc: Exception) -> str:
    return f"{section}_failed:{type(exc).__name__}:{exc}"


async def get_operating_briefing_impl(
    market: str,
    account_scope: str | None = None,
    session_context_limit: int = 10,
    include_current_price: bool = True,
) -> dict[str, Any]:
    as_of = now_kst()
    effective_scope = _default_account_scope(market, account_scope)
    holdings = await _get_holdings_impl(
        **_holdings_kwargs(market, effective_scope, include_current_price)
    )
    async with AsyncSessionLocal() as db:
        pending = await collect_pending_orders_snapshot(
            db,
            market=market,
            account_scope=effective_scope,
        )
        try:
            latest_report = await _latest_report_summary(
                db,
                market=market,
                account_scope=effective_scope,
            )
            latest_report_staleness = {
                "freshness_status": "db_read" if latest_report else "not_found",
            }
        except Exception as exc:  # noqa: BLE001
            reason = _section_unavailable_reason("latest_report", exc)
            latest_report = None
            latest_report_staleness = {
                "freshness_status": "unavailable",
                "unavailable_reason": reason,
            }

        try:
            session_context = await _recent_session_context(
                db,
                market=market,
                account_scope=effective_scope,
                limit=session_context_limit,
            )
            session_context_staleness = {
                "freshness_status": "db_read",
            }
        except Exception as exc:  # noqa: BLE001
            reason = _section_unavailable_reason("session_context", exc)
            session_context = {
                "count": 0,
                "entries": [],
                "unavailable_reason": reason,
            }
            session_context_staleness = {
                "freshness_status": "unavailable",
                "unavailable_reason": reason,
            }

        try:
            analysis_artifacts = await _recent_analysis_artifacts(
                db,
                market=market,
            )
            analysis_artifacts_staleness = {
                "freshness_status": "db_read",
            }
        except Exception as exc:  # noqa: BLE001
            reason = _section_unavailable_reason("analysis_artifacts", exc)
            analysis_artifacts = {
                "count": 0,
                "artifacts": [],
                "unavailable_reason": reason,
            }
            analysis_artifacts_staleness = {
                "freshness_status": "unavailable",
                "unavailable_reason": reason,
            }

    try:
        active_watches = await list_active_watches_impl(market=market)
        active_watches_staleness = {
            "as_of": active_watches.get("as_of"),
            "freshness_status": "db_read",
        }
    except Exception as exc:  # noqa: BLE001
        reason = _section_unavailable_reason("active_watches", exc)
        active_watches = {
            "count": 0,
            "active_watches": [],
            "unavailable_reason": reason,
        }
        active_watches_staleness = {
            "as_of": None,
            "freshness_status": "unavailable",
            "unavailable_reason": reason,
        }

    active_watches_unavailable_reason = active_watches.get("unavailable_reason")
    try:
        account_costs = await get_account_costs_setting()
    except Exception as exc:  # noqa: BLE001
        account_costs = None
        holdings.setdefault("errors", []).append(
            {"source": "account_costs", "error": str(exc)}
        )

    response = {
        "success": True,
        "market": market,
        "account_scope": effective_scope,
        "as_of": as_of.isoformat(),
        "staleness": {
            "holdings": {
                "as_of": as_of.isoformat(),
                "freshness_status": "live_or_best_effort",
                "errors": holdings.get("errors") or [],
            },
            "pending_orders": {
                "as_of": pending.as_of,
                "freshness_status": pending.freshness_status,
                "unavailable_reason": pending.unavailable_reason,
            },
            "active_watches": active_watches_staleness,
            "latest_report": latest_report_staleness,
            "session_context": session_context_staleness,
            "analysis_artifacts": analysis_artifacts_staleness,
        },
        "holdings": {
            "filters": holdings.get("filters"),
            "total_accounts": holdings.get("total_accounts"),
            "total_positions": holdings.get("total_positions"),
            "summary": holdings.get("summary"),
            "top_movers": _top_movers(holdings),
            # ROB-541 — per-account routable/account_mode so a reference-only
            # (toss/manual) holding is distinguishable from a kis_live-sellable one.
            "accounts": _account_routability(
                holdings,
                market=market,
                account_costs=account_costs,
            ),
            "errors": holdings.get("errors") or [],
        },
        "pending_orders": {
            "count": len(pending.orders or []),
            "orders": pending.orders,
            "unavailable_reason": pending.unavailable_reason,
        },
        "active_watches": {
            "count": active_watches.get("count", 0),
            "watches": active_watches.get("active_watches", []),
            **(
                {"unavailable_reason": active_watches_unavailable_reason}
                if active_watches_unavailable_reason
                else {}
            ),
        },
        "latest_report": latest_report,
        "session_context": session_context,
        "analysis_artifacts": analysis_artifacts,
    }
    return OperatingBriefingResponse.model_validate(response).model_dump(mode="json")


__all__ = [
    "get_operating_briefing_impl",
    "list_active_watches_impl",
]
