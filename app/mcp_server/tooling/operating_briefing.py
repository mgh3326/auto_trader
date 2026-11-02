"""Read-only operating briefing MCP tools for ROB-517."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import sentry_sdk

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.mcp_server.tooling.pending_orders_snapshot import (
    DEFAULT_PENDING_ORDERS_ACCOUNT_SCOPE,
    collect_pending_orders_snapshot,
)
from app.mcp_server.tooling.portfolio_cash import get_account_costs_setting
from app.mcp_server.tooling.portfolio_holdings import _get_portfolio_summary_impl
from app.schemas.analysis_artifact import AnalysisArtifactMeta
from app.schemas.investment_reports import (
    ActiveWatchesListResponse,
    InvestmentWatchAlertResponse,
    OperatingBriefingResponse,
)
from app.schemas.session_context import SessionContextResponse
from app.services.account_routing import compact_cost_profile
from app.services.analysis_artifact import AnalysisArtifactService
from app.services.invest_home_service import PortfolioSnapshotUnavailableError
from app.services.investment_reports.query_service import (
    InvestmentReportQueryService,
    _advisory_draft_profiles,
)
from app.services.investment_reports.repository import InvestmentReportsRepository
from app.services.session_context import SessionContextService
from app.services.trade_journal.negative_class import (
    load_negative_class_health,
)
from app.services.trading_policy_service import policy_version_stamp

_CONSTRAINT_CONTEXT_LOOKBACK_DAYS = 3
_CONSTRAINT_CONTEXT_LIMIT = 100

#: Sentry span op for one briefing phase. The traces view groups on the ``op``
#: prefix, so every phase of ``get_operating_briefing`` lands in one bucket and
#: the ``name`` says which phase was slow (ROB-1487).
_PHASE_SPAN_OP = "operating_briefing.phase"


@contextlib.contextmanager
def _phase(name: str) -> Iterator[Any]:
    """Child span for one briefing phase (ROB-1487).

    ``get_operating_briefing`` was p95 45 s with no attribution inside the
    transaction. Spans are cheap and are the only way the next trace can say
    which phase burned the time. Never raises: span creation is best-effort
    observability and must not be able to break the briefing.
    """
    with sentry_sdk.start_span(
        op=_PHASE_SPAN_OP, name=f"operating_briefing.{name}"
    ) as span:
        span.set_data("phase", name)
        yield span


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
    if account_scope == "db_simulated":
        # #1171 — DB paper accounts only (the h3-crypto-paper profile pins
        # this scope): the paper short-circuit never reaches a broker client.
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
            "position_count": account.get(
                "position_count", len(account.get("positions") or [])
            ),
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
    # ROB-1487: the summary consumes items only, but this used to call
    # ``get_bundle`` — ten sequential queries per briefing (report, items,
    # decisions, alerts, events, citations, linked orders, forecasts,
    # retrospectives). The other nine were thrown away. Same item list, one
    # query.
    items = await service.list_items_for_report(report.id)
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
    # An operator instruction stored with account_scope=NULL is market-wide and
    # must reach every account briefing for that market. Strict equality hid
    # those rows from the run-start briefing entirely.
    rows = await service.get_recent(
        market=market,  # type: ignore[arg-type]
        account_scope=account_scope,  # type: ignore[arg-type]
        limit=max(1, min(int(limit), 100)),
        include_market_wide=True,
    )
    entries = [
        SessionContextResponse.model_validate(row).model_dump(mode="json")
        for row in rows
    ]
    # Constraints are a separate, short-lived operator instruction surface.
    # Keep this query independent of the general recent-entry window so normal
    # session churn cannot evict an active constraint before the next session.
    # CancelledError is BaseException and must propagate. TimeoutError is an
    # Exception and stays in this section as an unavailable constraints payload
    # so the general entries already fetched are not discarded.
    try:
        constraint_rows = await service.get_recent(
            market=market,  # type: ignore[arg-type]
            account_scope=account_scope,  # type: ignore[arg-type]
            kst_date_from=(
                now_kst().date() - timedelta(days=_CONSTRAINT_CONTEXT_LOOKBACK_DAYS - 1)
            ),
            entry_type="constraint",
            limit=_CONSTRAINT_CONTEXT_LIMIT,
            include_market_wide=True,
        )
        constraints = {
            "count": len(constraint_rows),
            "entries": [
                SessionContextResponse.model_validate(row).model_dump(mode="json")
                for row in constraint_rows
            ],
        }
    except Exception as exc:  # noqa: BLE001
        constraints = _unavailable_constraints(
            _section_unavailable_reason("session_context_constraints", exc)
        )
    return {
        "count": len(rows),
        "entries": entries,
        "constraints": constraints,
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


def _unavailable_constraints(reason: str) -> dict[str, Any]:
    return {
        "count": 0,
        "entries": [],
        "unavailable_reason": reason,
    }


def _session_context_staleness(session_context: dict[str, Any]) -> dict[str, Any]:
    """Reuse the section unavailable pair when only constraints failed.

    A clean read stays ``{freshness_status: db_read}``. A constraint-only
    failure must not look like that clean read: the briefing already reports
    a failed optional section as ``freshness_status=unavailable`` plus
    ``unavailable_reason``. ``success`` is unchanged by the caller.
    """
    constraints = session_context.get("constraints")
    reason = (
        constraints.get("unavailable_reason") if isinstance(constraints, dict) else None
    )
    if isinstance(reason, str) and reason:
        return {
            "freshness_status": "unavailable",
            "unavailable_reason": reason,
        }
    return {"freshness_status": "db_read"}


@dataclass(slots=True)
class _BriefingDbSections:
    """Everything the briefing reads on its primary DB session (ROB-1487)."""

    pending: Any
    latest_report: dict[str, Any] | None
    latest_report_staleness: dict[str, Any]
    session_context: dict[str, Any]
    session_context_staleness: dict[str, Any]
    analysis_artifacts: dict[str, Any]
    analysis_artifacts_staleness: dict[str, Any]
    negative_class_recording: dict[str, Any]
    trading_scoreboards: dict[str, Any]


async def _collect_db_sections(
    *,
    market: str,
    account_scope: str,
    session_context_limit: int,
    cohort: str,
    include_counterfactual_delta: bool,
    as_of: Any,
) -> _BriefingDbSections:
    """Read the primary-session briefing sections in their historical order.

    Sections stay sequential here because they share one ``AsyncSession``
    (concurrent use of a single session is not allowed). What changed in
    ROB-1487 is that this whole block now runs concurrently with the two
    session-owning sections below via ``asyncio.gather``.
    """
    async with AsyncSessionLocal() as db:
        with _phase("pending_orders"):
            pending = await collect_pending_orders_snapshot(
                db,
                market=market,
                account_scope=account_scope,
            )
        with _phase("latest_report"):
            try:
                latest_report = await _latest_report_summary(
                    db,
                    market=market,
                    account_scope=account_scope,
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

        with _phase("session_context"):
            try:
                session_context = await _recent_session_context(
                    db,
                    market=market,
                    account_scope=account_scope,
                    limit=session_context_limit,
                )
                session_context_staleness = _session_context_staleness(session_context)
            except Exception as exc:  # noqa: BLE001
                reason = _section_unavailable_reason("session_context", exc)
                session_context = {
                    "count": 0,
                    "entries": [],
                    "unavailable_reason": reason,
                    "constraints": _unavailable_constraints(reason),
                }
                session_context_staleness = {
                    "freshness_status": "unavailable",
                    "unavailable_reason": reason,
                }

        with _phase("analysis_artifacts"):
            try:
                analysis_artifacts = await _recent_analysis_artifacts(
                    db,
                    market=market,
                    limit=10,
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

        # ROB-1283 — negative-class recording health. Fail-open: the briefing is
        # the run-start surface and must still return if this probe errors, but
        # a failure is reported as such rather than as a clean "ok".
        with _phase("negative_class_recording"):
            try:
                negative_class_recording = (
                    await load_negative_class_health(db, market=market, now=as_of)
                ).to_dict()
            except Exception as exc:  # noqa: BLE001
                negative_class_recording = {
                    "status": "unavailable",
                    "market": market,
                    "unavailable_reason": _section_unavailable_reason(
                        "negative_class_recording", exc
                    ),
                }

        with _phase("trading_scoreboards"):
            try:
                from app.services.trade_journal.aggregates import (
                    build_counterfactual_delta_scoreboard,
                    build_trading_scoreboard,
                )

                if include_counterfactual_delta:
                    trading_scoreboards = await build_counterfactual_delta_scoreboard(
                        db,
                        market=market,
                    )
                else:
                    trading_scoreboards = await build_trading_scoreboard(
                        db,
                        market=market,
                        cohort=cohort,
                    )
            except Exception as exc:  # noqa: BLE001
                trading_scoreboards = {"error": str(exc)}

    return _BriefingDbSections(
        pending=pending,
        latest_report=latest_report,
        latest_report_staleness=latest_report_staleness,
        session_context=session_context,
        session_context_staleness=session_context_staleness,
        analysis_artifacts=analysis_artifacts,
        analysis_artifacts_staleness=analysis_artifacts_staleness,
        negative_class_recording=negative_class_recording,
        trading_scoreboards=trading_scoreboards,
    )


async def _active_watches_section(
    market: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Active watches plus its staleness block (owns its own session)."""
    with _phase("active_watches"):
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
    return active_watches, active_watches_staleness


async def _account_costs_section() -> tuple[Any, Exception | None]:
    """Account-cost setting plus the error, if any (owns its own session).

    The caller keeps the historical error surfacing: the failure is appended
    to ``holdings["errors"]`` after the gathered sections return.
    """
    with _phase("account_costs"):
        try:
            return await get_account_costs_setting(), None
        except Exception as exc:  # noqa: BLE001
            return None, exc


async def _gather_sections(*coros: Any) -> list[Any]:
    """``asyncio.gather`` that cancels siblings when one section raises.

    Plain ``gather`` leaves the other tasks running after the first exception
    and their results/exceptions are never retrieved. The briefing used to run
    these sections strictly sequentially, so an exception must not leave work
    dangling behind the caller's back.
    """
    tasks = [asyncio.ensure_future(coro) for coro in coros]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def get_operating_briefing_impl(
    market: str,
    account_scope: str | None = None,
    session_context_limit: int = 10,
    include_current_price: bool = True,
    cohort: str = "live_gated",
    include_counterfactual_delta: bool = False,
) -> dict[str, Any]:
    as_of = now_kst()
    effective_scope = _default_account_scope(market, account_scope)
    # ROB-1310: briefing consumes a bounded summary read model. It must not
    # reuse the full holdings projection or fan out to sellable/current-price
    # endpoints; the shared Toss snapshot supplies only the balance metrics.
    try:
        with _phase("holdings"):
            holdings = await _get_portfolio_summary_impl(
                **_holdings_kwargs(market, effective_scope, False)
            )
    except PortfolioSnapshotUnavailableError as exc:
        # BLOCKER-2: a hung whole-snapshot owner past the hard wait bound
        # must degrade the briefing's holdings section rather than raise a
        # raw/unhandled error out of the run-start briefing entrypoint.
        holdings = {
            "accounts": [],
            "errors": [
                {
                    "source": "portfolio_snapshot",
                    "error": exc.error_code,
                    "unavailable_reason": exc.reason,
                    "degraded": True,
                }
            ],
        }
    # ROB-1487: the primary-session block, active watches and account costs are
    # independent reads. They used to run back-to-back (three serial session
    # acquisitions); now they run concurrently. Within the primary block the
    # sections stay sequential because they share one AsyncSession.
    (
        db_sections,
        (active_watches, active_watches_staleness),
        (account_costs, account_costs_error),
    ) = await _gather_sections(
        _collect_db_sections(
            market=market,
            account_scope=effective_scope,
            session_context_limit=session_context_limit,
            cohort=cohort,
            include_counterfactual_delta=include_counterfactual_delta,
            as_of=as_of,
        ),
        _active_watches_section(market),
        _account_costs_section(),
    )
    pending = db_sections.pending
    latest_report = db_sections.latest_report
    latest_report_staleness = db_sections.latest_report_staleness
    session_context = db_sections.session_context
    session_context_staleness = db_sections.session_context_staleness
    analysis_artifacts = db_sections.analysis_artifacts
    analysis_artifacts_staleness = db_sections.analysis_artifacts_staleness
    negative_class_recording = db_sections.negative_class_recording
    trading_scoreboards = db_sections.trading_scoreboards

    active_watches_unavailable_reason = active_watches.get("unavailable_reason")
    if account_costs_error is not None:
        account_costs = None
        holdings.setdefault("errors", []).append(
            {"source": "account_costs", "error": str(account_costs_error)}
        )

    with _phase("policy_version"):
        try:
            policy_version = policy_version_stamp()
        except Exception as exc:  # noqa: BLE001 — fail-open, briefing must still return
            policy_version = {"error": str(exc)}

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
            "top_movers": (
                list(holdings.get("top_movers") or [])
                if "top_movers" in holdings
                else _top_movers(holdings)
            ),
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
        "policy_version": policy_version,
        "trading_scoreboards": trading_scoreboards,
        "negative_class_recording": negative_class_recording,
    }
    return OperatingBriefingResponse.model_validate(response).model_dump(mode="json")


__all__ = [
    "get_operating_briefing_impl",
    "list_active_watches_impl",
]
