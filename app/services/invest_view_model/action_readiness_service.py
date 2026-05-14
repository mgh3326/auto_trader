"""ROB-256 — read-only KR action-report readiness view model.

This module intentionally maps existing /invest read models and the existing
InvestHomeService account-panel path into readiness metadata. It does not call
order submission/cancel/modify services, run backfills, activate schedulers, or
scrape external sites from the request path.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.kr_symbol_universe import KRSymbolUniverse
from app.models.market_quote_snapshot import MarketQuoteSnapshot
from app.models.market_valuation_snapshot import MarketValuationSnapshot
from app.models.trade_journal import TradeJournal
from app.schemas.invest_action_readiness import (
    ActionReadinessAuthority,
    ActionReadinessFamily,
    ActionReadinessLink,
    ActionReadinessState,
    ActionReportImpact,
    KrActionReadinessResponse,
)
from app.schemas.invest_coverage import (
    CoverageActionability,
    CoverageState,
    InvestCoverageResponse,
    InvestCoverageSurface,
)
from app.schemas.invest_home import InvestHomeResponse
from app.services.invest_coverage_service import build_invest_coverage
from app.services.invest_home_service import InvestHomeService

_SOURCE_POLICY = [
    "KIS live broker values are authoritative for tradeable KR holdings, cash, open orders, and sellable quantity.",
    "/invest DB/read-model state is the product authority for market, screener, Naver/Toss-derived reference, news, calendar, valuation, flow, and historical ledger readiness.",
    "Toss/Naver/external sources are displayed only as reference, candidate, or supporting signals and are never source-of-truth for action readiness.",
    "Unavailable data is surfaced as stale/missing/partial/failed/unsupported/확인 불가 rather than estimated.",
]


def _safe_actionability(reason: str, *, priority: str = "high") -> CoverageActionability:
    if priority == "none":
        return CoverageActionability(
            priority="none",
            action="none",
            queue=None,
            approvalGates=[],
            reason=reason,
            safeByDefault=True,
        )
    return CoverageActionability(
        priority=priority,  # type: ignore[arg-type]
        action="investigate" if priority != "blocked" else "provider_contract_needed",
        queue="invest-action-readiness-review",
        approvalGates=["code_review"],
        reason=reason,
        safeByDefault=True,
    )


def _coverage_state_to_readiness(
    state: CoverageState,
    *, critical: bool = False,
) -> ActionReadinessState:
    if state == "fresh":
        return "ready"
    if state in {"partial", "stale"}:
        return "degraded"
    if state == "unsupported":
        return "unsupported"
    if state == "provider_unwired":
        return "blocked" if critical else "unknown"
    if state == "error":
        return "blocked"
    if state == "missing":
        return "blocked" if critical else "missing"
    return "unknown"


def _surface_index(
    coverage: InvestCoverageResponse,
) -> dict[tuple[str, str | None], InvestCoverageSurface]:
    return {(surface.surface, surface.market): surface for surface in coverage.surfaces}


def _surface_family(
    *,
    key: str,
    label_ko: str,
    category: str,
    surface: InvestCoverageSurface | None,
    authority: ActionReadinessAuthority = "auto_trader_read_model",
    impact: ActionReportImpact = "degrades_report",
    critical: bool = False,
    source_override: str | None = None,
    extra_notes: Sequence[str] = (),
    extra_references: Sequence[str] = (),
    links: Sequence[ActionReadinessLink] = (),
) -> ActionReadinessFamily:
    if surface is None:
        state: ActionReadinessState = "unknown" if not critical else "blocked"
        blockers = [f"{key}: 확인 불가 — durable /invest read-model surface is not wired."]
        return ActionReadinessFamily(
            key=key,
            labelKo=label_ko,
            category=category,
            state=state,
            impact="blocks_all_action_reports" if critical else impact,
            authority=authority,
            sourceOfTruth=source_override or "auto_trader_read_model/unwired",
            references=list(extra_references),
            actionability=_safe_actionability(blockers[0], priority="blocked"),
            blockers=blockers if critical else [],
            warnings=[] if critical else blockers,
            notes=list(extra_notes),
            links=list(links),
        )

    readiness = _coverage_state_to_readiness(surface.state, critical=critical)
    blockers: list[str] = []
    warnings = list(surface.warnings)
    if readiness == "blocked":
        blockers.extend(surface.warnings or [f"{key}: 확인 불가"])
    elif readiness in {"degraded", "missing", "unknown"} and not warnings:
        warnings.append(f"{key}: {surface.state} 상태입니다.")
    return ActionReadinessFamily(
        key=key,
        labelKo=label_ko,
        category=category,
        state=readiness,
        impact=("blocks_all_action_reports" if critical and readiness == "blocked" else impact),
        authority=authority,
        sourceOfTruth=source_override or surface.sourceOfTruth,
        references=surface.references,
        latestAt=surface.latestAt,
        latestDate=surface.latestDate,
        counts=surface.counts,
        coverageState=surface.state,
        actionability=surface.actionability,
        blockers=blockers,
        warnings=warnings,
        notes=list(surface.notes) + list(extra_notes),
        links=list(links),
    )


def _symbol_state_from_latest_date(
    latest_date: dt.date | None,
    trading_day: dt.date,
    *,
    critical: bool = False,
) -> ActionReadinessState:
    if latest_date is None:
        return "blocked" if critical else "missing"
    return "ready" if latest_date >= trading_day else "degraded"


def _symbol_blocker_or_warning(
    *,
    key: str,
    symbol: str,
    state: ActionReadinessState,
    source: str,
) -> tuple[list[str], list[str]]:
    message = f"{key}: {symbol} {source} 확인 불가"
    if state == "blocked":
        return [message], []
    if state in {"degraded", "missing", "unknown"}:
        return [], [f"{key}: {symbol} symbol-scoped read-model is {state}/확인 불가"]
    return [], []


async def _symbol_quote_family(
    db: AsyncSession,
    *,
    symbol: str,
    trading_day: dt.date,
) -> ActionReadinessFamily:
    latest_at = (
        await db.execute(
            sa.select(sa.func.max(MarketQuoteSnapshot.snapshot_at)).where(
                MarketQuoteSnapshot.market == "kr",
                MarketQuoteSnapshot.symbol == symbol,
            )
        )
    ).scalar_one_or_none()
    latest_date = latest_at.date() if latest_at else None
    state = _symbol_state_from_latest_date(latest_date, trading_day, critical=True)
    blockers, warnings = _symbol_blocker_or_warning(
        key="quotes", symbol=symbol, state=state, source="market_quote_snapshots"
    )
    return ActionReadinessFamily(
        key="quotes",
        labelKo="시세 / 현재가",
        category="Market/read-model data",
        state=state,
        impact="blocks_all_action_reports" if state == "blocked" else "degrades_report",
        authority="auto_trader_read_model",
        sourceOfTruth="market_quote_snapshots",
        references=["toss", "naver_reference"],
        latestAt=latest_at,
        latestDate=latest_date,
        coverageState=None,
        actionability=_safe_actionability(
            blockers[0] if blockers else warnings[0] if warnings else f"{symbol} quote read-model is visible.",
            priority="blocked" if blockers else "high" if warnings else "none",
        ),
        blockers=blockers,
        warnings=warnings,
        notes=["Symbol-scoped quote readiness is checked from the durable read-model only; no request-path provider fetch is made."],
        links=[ActionReadinessLink(label="Stock detail", href=f"/invest/stocks/kr/{symbol}")],
    )


async def _symbol_ohlcv_family(
    db: AsyncSession,
    *,
    symbol: str,
    trading_day: dt.date,
) -> ActionReadinessFamily:
    try:
        row = (
            await db.execute(
                sa.text(
                    """
                    SELECT MAX(time) AS latest_time,
                           MAX(time::date) AS latest_date
                    FROM public.kr_candles_1m
                    WHERE symbol = :symbol
                    """
                ),
                {"symbol": symbol},
            )
        ).one()
        latest_at = row[0]
        latest_date = row[1]
    except Exception:  # noqa: BLE001 - readiness must fail closed/read-only
        await db.rollback()
        latest_at = None
        latest_date = None
    state = _symbol_state_from_latest_date(latest_date, trading_day)
    blockers, warnings = _symbol_blocker_or_warning(
        key="ohlcv", symbol=symbol, state=state, source="kr_candles_1m"
    )
    return ActionReadinessFamily(
        key="ohlcv",
        labelKo="OHLCV 캔들",
        category="Market/read-model data",
        state=state,
        impact="degrades_report",
        authority="auto_trader_read_model",
        sourceOfTruth="kr_candles_1m",
        references=[],
        latestAt=latest_at,
        latestDate=latest_date,
        coverageState=None,
        actionability=_safe_actionability(
            warnings[0] if warnings else f"{symbol} OHLCV read-model is visible.",
            priority="high" if warnings else "none",
        ),
        blockers=blockers,
        warnings=warnings,
        notes=["Symbol-scoped OHLCV readiness is checked from kr_candles_1m only; no request-path provider fetch is made."],
    )


async def _symbol_valuation_family(
    db: AsyncSession,
    *,
    symbol: str,
    trading_day: dt.date,
) -> ActionReadinessFamily:
    latest_date = (
        await db.execute(
            sa.select(sa.func.max(MarketValuationSnapshot.snapshot_date)).where(
                MarketValuationSnapshot.market == "kr",
                MarketValuationSnapshot.symbol == symbol,
            )
        )
    ).scalar_one_or_none()
    state = _symbol_state_from_latest_date(latest_date, trading_day)
    blockers, warnings = _symbol_blocker_or_warning(
        key="valuation_fundamentals",
        symbol=symbol,
        state=state,
        source="market_valuation_snapshots",
    )
    return ActionReadinessFamily(
        key="valuation_fundamentals",
        labelKo="밸류에이션 / 펀더멘털",
        category="News/calendar/research context",
        state=state,
        impact="degrades_report",
        authority="auto_trader_read_model",
        sourceOfTruth="market_valuation_snapshots",
        references=["naver_reference"],
        latestDate=latest_date,
        coverageState=None,
        actionability=_safe_actionability(
            warnings[0] if warnings else f"{symbol} valuation read-model is visible.",
            priority="high" if warnings else "none",
        ),
        blockers=blockers,
        warnings=warnings,
        notes=["Symbol-scoped valuation readiness is checked from the durable read-model only; no request-path provider fetch is made."],
    )


def _apply_symbol_surface_diagnostics(
    families: list[ActionReadinessFamily],
    coverage: InvestCoverageResponse,
    *,
    symbol: str,
) -> None:
    symbol_row = next((row for row in coverage.symbols if row.symbol == symbol), None)
    if symbol_row is None:
        return
    by_key = {family.key: family for family in families}
    for key in ("screener_snapshots", "news_feed", "investor_flow"):
        state = symbol_row.surfaces.get(key)
        family = by_key.get(key)
        if state is None or family is None:
            continue
        readiness = _coverage_state_to_readiness(state)
        if readiness == "ready":
            family.notes.append(f"{symbol} symbol-scoped {key} diagnostic is ready.")
            continue
        family.state = readiness
        family.coverageState = state
        family.latestDate = symbol_row.latestDates.get(key)
        warning = f"{key}: {symbol} symbol-scoped diagnostic is {state}/확인 불가"
        if readiness == "blocked":
            family.blockers.append(warning)
        else:
            family.warnings.append(warning)
        family.notes.append("Aggregate market coverage was overridden by symbol-scoped coverage diagnostics for action-readiness.")


def _broker_family(
    *,
    key: str,
    label_ko: str,
    impact: ActionReportImpact,
    home: InvestHomeResponse | None,
    symbol: str | None,
) -> ActionReadinessFamily:
    live_accounts = [account for account in (home.accounts if home else []) if account.source == "kis" and account.accountKind == "live"]
    live_kr_holdings = [
        holding
        for holding in (home.holdings if home else [])
        if holding.source == "kis" and holding.accountKind == "live" and holding.market == "KR"
    ]
    if symbol:
        live_kr_holdings = [holding for holding in live_kr_holdings if holding.symbol == symbol]

    warnings = [w.message for w in (home.meta.warnings if home else []) if w.source == "kis"]
    has_kis_warning = bool(warnings)
    state: ActionReadinessState = "ready"
    blockers: list[str] = []
    notes: list[str] = []
    latest_note = "Existing InvestHomeService/account-panel read path only; no new broker mutation path."

    if home is None or has_kis_warning:
        state = "blocked"
        blockers.append(f"{label_ko}: KIS live 확인 불가")
    elif key == "kis_live_cash_orderable":
        has_orderable = any(
            (account.buyingPower.krw is not None or account.cashBalances.krw is not None)
            for account in live_accounts
        )
        if not has_orderable:
            state = "blocked"
            blockers.append("KIS live 주문가능 현금 확인 불가")
    elif key in {"kis_live_holdings", "kis_live_sellable_quantity"}:
        if symbol:
            if not live_kr_holdings:
                state = "blocked" if key == "kis_live_sellable_quantity" else "missing"
                blockers.append(f"{symbol} KIS live 보유/매도가능 수량 확인 불가")
            elif key == "kis_live_sellable_quantity" and all(
                holding.sellableQuantity is None for holding in live_kr_holdings
            ):
                state = "blocked"
                blockers.append(f"{symbol} KIS live 매도가능 수량 확인 불가")
        elif key == "kis_live_sellable_quantity":
            state = "blocked"
            blockers.append("심볼 미지정으로 KIS live 매도가능 수량 확인 불가")
            notes.append("심볼을 지정하면 매도가능 수량 readiness를 평가합니다.")
    elif key == "kis_live_open_orders":
        # Current open-order authority is represented by the pending_orders
        # read-model/reconciliation family below. The live broker remains the
        # authority, so absence of a live open-order snapshot is fail-closed.
        state = "blocked"
        blockers.append("KIS live 미체결 주문 확인 불가")
        notes.append("pending_orders read-model과 reconcile 상태는 별도 가족에서 함께 표시합니다.")

    if state == "ready" and not live_accounts:
        state = "blocked"
        blockers.append("KIS live 계좌 확인 불가")

    return ActionReadinessFamily(
        key=key,
        labelKo=label_ko,
        category="Broker authority",
        state=state,
        impact=impact if state in {"blocked", "unknown", "missing"} else "none",
        authority="kis_live_broker",
        sourceOfTruth="KIS live via existing InvestHomeService/account-panel",
        references=["manual_or_paper_reference"],
        actionability=_safe_actionability(blockers[0] if blockers else latest_note, priority="blocked" if blockers else "none"),
        blockers=blockers,
        warnings=warnings,
        notes=[latest_note, *notes],
        links=[ActionReadinessLink(label="Account panel", href="/invest/api/account-panel")],
    )


async def _trade_journal_family(db: AsyncSession, symbol: str | None) -> ActionReadinessFamily:
    stmt = sa.select(sa.func.count(), sa.func.max(TradeJournal.updated_at)).where(
        TradeJournal.status == "active",
        TradeJournal.account_type == "live",
    )
    if symbol:
        stmt = stmt.where(TradeJournal.symbol == symbol)
    count, latest_at = (await db.execute(stmt)).one()
    active_count = int(count or 0)
    state: ActionReadinessState = "ready" if active_count else "missing"
    warnings = [] if active_count else ["활성 live trade journal 확인 불가 — sell report는 thesis/target/stop 확인이 필요합니다."]
    return ActionReadinessFamily(
        key="trade_journals",
        labelKo="투자 저널 / thesis",
        category="Broker authority",
        state=state,
        impact="degrades_report",
        authority="auto_trader_read_model",
        sourceOfTruth="review.trade_journals",
        references=[],
        latestAt=latest_at,
        counts=None,
        coverageState=None,
        actionability=_safe_actionability(warnings[0] if warnings else "Active live trade journals are visible.", priority="high" if warnings else "none"),
        blockers=[],
        warnings=warnings,
        notes=["Sell action reports must check active journals before any sell recommendation."],
    )


async def build_kr_action_readiness(
    *,
    db: AsyncSession,
    user_id: int,
    home_service: InvestHomeService,
    symbol: str | None = None,
) -> KrActionReadinessResponse:
    normalized_symbol = symbol.strip().upper() if symbol and symbol.strip() else None
    if normalized_symbol is not None and not (normalized_symbol.isdigit() and len(normalized_symbol) == 6):
        now = dt.datetime.now(dt.UTC)
        blocker = "symbol_not_kr_equity: KR 심볼은 6자리 종목코드여야 합니다. 확인 불가"
        return KrActionReadinessResponse(
            asOf=now,
            symbol=normalized_symbol,
            overallState="blocked",
            canGenerateBuyReport=False,
            canGenerateSellReport=False,
            families=[],
            blockers=[blocker],
            sourcePolicy=_SOURCE_POLICY,
            notes=["No provider or broker request was made for unsupported symbol input."],
        )

    symbol_exists = True
    if normalized_symbol is not None:
        symbol_exists = bool(
            (
                await db.execute(
                    sa.select(KRSymbolUniverse.symbol).where(
                        KRSymbolUniverse.symbol == normalized_symbol,
                        KRSymbolUniverse.is_active.is_(True),
                    )
                )
            ).scalar_one_or_none()
        )

    if normalized_symbol is not None and not symbol_exists:
        now = dt.datetime.now(dt.UTC)
        blocker = f"symbol_not_in_kr_universe: {normalized_symbol} 확인 불가"
        return KrActionReadinessResponse(
            asOf=now,
            symbol=normalized_symbol,
            overallState="blocked",
            canGenerateBuyReport=False,
            canGenerateSellReport=False,
            families=[
                ActionReadinessFamily(
                    key="symbol_resolution",
                    labelKo="KR 심볼 확인",
                    category="Market/read-model data",
                    state="blocked",
                    impact="blocks_all_action_reports",
                    authority="auto_trader_read_model",
                    sourceOfTruth="kr_symbol_universe",
                    references=[],
                    actionability=_safe_actionability(
                        "KR symbol universe에서 심볼 확인 불가", priority="blocked"
                    ),
                    blockers=[f"{normalized_symbol} not found in active kr_symbol_universe"],
                    warnings=[],
                    notes=[
                        "No coverage, account-panel, broker, or provider request was made for unresolved symbol input.",
                        "Do not infer or fabricate KR symbol identity.",
                    ],
                )
            ],
            blockers=[blocker],
            sourcePolicy=_SOURCE_POLICY,
            notes=["No provider or broker request was made for unresolved symbol input."],
        )

    coverage = await build_invest_coverage(
        db, market="kr", symbols=[normalized_symbol] if normalized_symbol else []
    )
    surfaces = _surface_index(coverage)
    home: InvestHomeResponse | None = None
    home_unavailable = False
    try:
        home = await home_service.get_home(user_id=user_id)
    except Exception:  # read-only partial failure: surface 확인 불가
        home_unavailable = True

    families: list[ActionReadinessFamily] = [
        _broker_family(
            key="kis_live_holdings",
            label_ko="KIS live 보유",
            impact="blocks_sell_report",
            home=home,
            symbol=normalized_symbol,
        ),
        _broker_family(
            key="kis_live_cash_orderable",
            label_ko="KIS live 주문가능 현금",
            impact="blocks_buy_report",
            home=home,
            symbol=normalized_symbol,
        ),
        _broker_family(
            key="kis_live_open_orders",
            label_ko="KIS live 미체결 주문",
            impact="blocks_all_action_reports",
            home=home,
            symbol=normalized_symbol,
        ),
        _broker_family(
            key="kis_live_sellable_quantity",
            label_ko="KIS live 매도가능 수량",
            impact="blocks_sell_report",
            home=home,
            symbol=normalized_symbol,
        ),
        await _trade_journal_family(db, normalized_symbol),
    ]

    if home_unavailable:
        for family in families[:4]:
            family.state = "blocked"
            family.blockers.append("KIS/account panel 확인 불가")

    def s(name: str) -> InvestCoverageSurface | None:
        return surfaces.get((name, "kr")) or surfaces.get((name, None))

    if normalized_symbol:
        symbol_market_families = [
            await _symbol_quote_family(db, symbol=normalized_symbol, trading_day=coverage.tradingDate),
            await _symbol_ohlcv_family(db, symbol=normalized_symbol, trading_day=coverage.tradingDate),
            _surface_family(key="technical_indicators", label_ko="기술지표", category="Market/read-model data", surface=None, impact="degrades_report", extra_notes=["No separate durable indicator readiness surface is wired; do not calculate indicators in this request path."]),
            _surface_family(key="support_resistance", label_ko="지지/저항", category="Market/read-model data", surface=None, impact="degrades_report", extra_notes=["No durable support/resistance readiness surface is wired; values must not be fabricated."]),
            _surface_family(key="orderbook_session", label_ko="호가 / 세션", category="Market/read-model data", surface=s("orderbook_nxt_capability"), impact="degrades_report", extra_notes=["This reports local NXT/session capability, not a request-path orderbook fetch."]),
            _surface_family(key="nxt_eligibility", label_ko="NXT 대상 여부", category="Market/read-model data", surface=s("orderbook_nxt_capability")),
            _surface_family(key="screener_snapshots", label_ko="스크리너 스냅샷", category="Market/read-model data", surface=s("screener_snapshots")),
            _surface_family(key="naver_momentum_events", label_ko="Naver 모멘텀 이벤트", category="Market/read-model data", surface=None, impact="degrades_report", extra_references=["naver_reference"], extra_notes=["Naver momentum events are reference/candidate only and not source-of-truth."]),
            _surface_family(key="naver_momentum_candidates", label_ko="Naver 모멘텀 후보", category="Market/read-model data", surface=None, impact="degrades_report", extra_references=["naver_reference"], extra_notes=["Momentum candidates are aggregate reference data only, not trading instructions."]),
            _surface_family(key="naver_theme_events", label_ko="Naver 테마 이벤트", category="Market/read-model data", surface=None, impact="degrades_report", extra_references=["naver_reference"], extra_notes=["Theme events are aggregate reference data only."]),
            _surface_family(key="investor_flow", label_ko="투자자 수급", category="Market/read-model data", surface=s("investor_flow")),
            _surface_family(key="news_feed", label_ko="뉴스 피드", category="News/calendar/research context", surface=s("news_feed")),
            _surface_family(key="issue_clusters", label_ko="시장 이슈 클러스터", category="News/calendar/research context", surface=None, impact="degrades_report"),
            _surface_family(key="disclosures", label_ko="공시", category="News/calendar/research context", surface=None, impact="degrades_report"),
            _surface_family(key="calendar_events", label_ko="캘린더 이벤트", category="News/calendar/research context", surface=s("calendar_events")),
            await _symbol_valuation_family(db, symbol=normalized_symbol, trading_day=coverage.tradingDate),
            _surface_family(key="research_reports", label_ko="리서치 리포트", category="News/calendar/research context", surface=s("research_reports")),
            _surface_family(key="research_consensus", label_ko="리서치 컨센서스", category="News/calendar/research context", surface=None, impact="degrades_report", extra_notes=["No distinct durable research-consensus readiness surface is wired; research report freshness is not treated as consensus availability."]),
            _surface_family(key="execution_ledger", label_ko="체결 / 실행 이력", category="Execution/history", surface=None, impact="degrades_report", extra_notes=["No distinct execution/fill ledger readiness surface is wired; pending orders are not treated as historical fills."]),
            _surface_family(key="sell_history", label_ko="매도 이력", category="Execution/history", surface=None, impact="degrades_report", extra_notes=["No distinct sell-history readiness surface is wired; do not infer sell history from pending orders."]),
            _surface_family(key="pending_order_reconciliation", label_ko="미체결 주문 reconcile", category="Execution/history", surface=None, critical=True, impact="blocks_all_action_reports", extra_notes=["Pending-order table freshness alone does not prove live open-order reconciliation; fail closed until a reconciliation read-model is wired."]),
        ]
        families.extend(symbol_market_families)
        _apply_symbol_surface_diagnostics(families, coverage, symbol=normalized_symbol)
    else:
        families.extend(
            [
                _surface_family(key="quotes", label_ko="시세 / 현재가", category="Market/read-model data", surface=s("quotes"), critical=False, impact="degrades_report", links=[ActionReadinessLink(label="Coverage", href="/invest/coverage")]),
                _surface_family(key="ohlcv", label_ko="OHLCV 캔들", category="Market/read-model data", surface=s("ohlcv")),
                _surface_family(key="technical_indicators", label_ko="기술지표", category="Market/read-model data", surface=None, impact="degrades_report", extra_notes=["No separate durable indicator readiness surface is wired; do not calculate indicators in this request path."]),
                _surface_family(key="support_resistance", label_ko="지지/저항", category="Market/read-model data", surface=None, impact="degrades_report", extra_notes=["No durable support/resistance readiness surface is wired; values must not be fabricated."]),
                _surface_family(key="orderbook_session", label_ko="호가 / 세션", category="Market/read-model data", surface=s("orderbook_nxt_capability"), impact="degrades_report", extra_notes=["This reports local NXT/session capability, not a request-path orderbook fetch."]),
                _surface_family(key="nxt_eligibility", label_ko="NXT 대상 여부", category="Market/read-model data", surface=s("orderbook_nxt_capability")),
                _surface_family(key="screener_snapshots", label_ko="스크리너 스냅샷", category="Market/read-model data", surface=s("screener_snapshots")),
                _surface_family(key="naver_momentum_events", label_ko="Naver 모멘텀 이벤트", category="Market/read-model data", surface=None, impact="degrades_report", extra_references=["naver_reference"], extra_notes=["Naver momentum events are reference/candidate only and not source-of-truth."]),
                _surface_family(key="naver_momentum_candidates", label_ko="Naver 모멘텀 후보", category="Market/read-model data", surface=None, impact="degrades_report", extra_references=["naver_reference"], extra_notes=["Momentum candidates are aggregate reference data only, not trading instructions."]),
                _surface_family(key="naver_theme_events", label_ko="Naver 테마 이벤트", category="Market/read-model data", surface=None, impact="degrades_report", extra_references=["naver_reference"], extra_notes=["Theme events are aggregate reference data only."]),
                _surface_family(key="investor_flow", label_ko="투자자 수급", category="Market/read-model data", surface=s("investor_flow")),
                _surface_family(key="news_feed", label_ko="뉴스 피드", category="News/calendar/research context", surface=s("news_feed")),
                _surface_family(key="issue_clusters", label_ko="시장 이슈 클러스터", category="News/calendar/research context", surface=None, impact="degrades_report"),
                _surface_family(key="disclosures", label_ko="공시", category="News/calendar/research context", surface=None, impact="degrades_report"),
                _surface_family(key="calendar_events", label_ko="캘린더 이벤트", category="News/calendar/research context", surface=s("calendar_events")),
                _surface_family(key="valuation_fundamentals", label_ko="밸류에이션 / 펀더멘털", category="News/calendar/research context", surface=s("valuation_fundamentals")),
                _surface_family(key="research_reports", label_ko="리서치 리포트", category="News/calendar/research context", surface=s("research_reports")),
                _surface_family(key="research_consensus", label_ko="리서치 컨센서스", category="News/calendar/research context", surface=None, impact="degrades_report", extra_notes=["No distinct durable research-consensus readiness surface is wired; research report freshness is not treated as consensus availability."]),
                _surface_family(key="execution_ledger", label_ko="체결 / 실행 이력", category="Execution/history", surface=None, impact="degrades_report", extra_notes=["No distinct execution/fill ledger readiness surface is wired; pending orders are not treated as historical fills."]),
                _surface_family(key="sell_history", label_ko="매도 이력", category="Execution/history", surface=None, impact="degrades_report", extra_notes=["No distinct sell-history readiness surface is wired; do not infer sell history from pending orders."]),
                _surface_family(key="pending_order_reconciliation", label_ko="미체결 주문 reconcile", category="Execution/history", surface=None, critical=True, impact="blocks_all_action_reports", extra_notes=["Pending-order table freshness alone does not prove live open-order reconciliation; fail closed until a reconciliation read-model is wired."]),
            ]
        )

    blockers: list[str] = []
    degraded: list[str] = []
    for family in families:
        if family.blockers:
            blockers.extend(f"{family.key}: {blocker}" for blocker in family.blockers)
        elif family.state in {"degraded", "missing", "unknown"}:
            degraded.append(f"{family.key}: {family.warnings[0] if family.warnings else family.state}")

    buy_blocked = any(
        family.state == "blocked"
        and family.impact in {"blocks_buy_report", "blocks_all_action_reports"}
        for family in families
    ) or bool(normalized_symbol and not symbol_exists)
    sell_blocked = any(
        family.state == "blocked"
        and family.impact in {"blocks_sell_report", "blocks_all_action_reports"}
        for family in families
    ) or bool(normalized_symbol and not symbol_exists)
    overall: ActionReadinessState
    if buy_blocked and sell_blocked:
        overall = "blocked"
    elif buy_blocked or sell_blocked:
        overall = "degraded"
    elif degraded:
        overall = "degraded"
    else:
        overall = "ready"

    return KrActionReadinessResponse(
        asOf=coverage.asOf,
        symbol=normalized_symbol,
        overallState=overall,
        canGenerateBuyReport=not buy_blocked,
        canGenerateSellReport=not sell_blocked,
        families=families,
        blockers=blockers,
        degradedSignals=degraded,
        sourcePolicy=_SOURCE_POLICY,
        notes=[
            "Read-only readiness only: no order, watch/order-intent, scheduler, backfill, or broker mutation is performed.",
            "Actionability metadata is advisory and approval-gated; it is not an execution control.",
        ],
    )
