"""ROB-147 — read-only view-model wrapper around the screening service.

Public API:
- build_screener_presets() -> ScreenerPresetsResponse
- build_screener_results(preset_id, screening_service, resolver) -> ScreenerResultsResponse

The service intentionally takes its dependencies as parameters so the router
can inject the existing `app.services.screener_service.ScreenerService` (and
tests can inject mocks). It must not import any broker / order / mutation
modules — see tests/test_invest_view_model_safety.py.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from datetime import time as _time
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.invest_screener import (
    ChangeDirection,
    ScreenerCandidateContext,
    ScreenerFreshness,
    ScreenerFreshnessDependency,
    ScreenerFreshnessPrimary,
    ScreenerInvestorFlowChip,
    ScreenerPresetsResponse,
    ScreenerResultRow,
    ScreenerResultsResponse,
    ScreenerRiskContext,
    ScreenerSourceContext,
)
from app.schemas.investor_flow import InvestorFlowItem
from app.services.invest_view_model.fundamentals_screener import (
    FUNDAMENTALS_PRESET_SPECS,
)
from app.services.invest_view_model.investor_flow_service import (
    latest_items_for_symbols as _latest_investor_flow_items,
)
from app.services.invest_view_model.screener_presets import (
    CRYPTO_DEFAULT_PRESET_ID,
    DEFAULT_PRESET_ID,
    get_preset,
    preset_definitions,
    screening_filters_for,
)

_VALID_MARKETS = {"kr", "us", "crypto"}
_KR_ABSURD_MARKET_CAP_KRW = 10_000_000_000_000_000

_KST = ZoneInfo("Asia/Seoul")
_KR_OPEN = _time(9, 0)
_KR_CLOSE = _time(15, 30)
_CACHE_HIT_FRESH_SECONDS = 300
_SNAPSHOT_FIRST_LIMIT = 80
_MAX_WARNING_CHARS = 240
_US_SCREENER_DATA_NOT_READY_WARNING = (
    "미국 스크리너 데이터 준비중 — 일부 결과만 표시됩니다."
)
_KR_TOSS_ETF_PREFIXES = (
    "ACE ",
    "ARIRANG ",
    "FOCUS ",
    "HANARO ",
    "KBSTAR ",
    "KODEX ",
    "KOSEF ",
    "PLUS ",
    "RISE ",
    "SOL ",
    "TIGER ",
    "TIMEFOLIO ",
    "TREX ",
    "WON ",
    "마이티 ",
    "히어로즈 ",
)
_KR_TOSS_EXCLUDED_NAME_TOKENS = (
    " ETF",
    " ETN",
    "스팩",
    "기업인수목적",
    "리츠",
    "인프라펀드",
    "맥쿼리인프라",
    "선박투자",
    "레버리지",
    "인버스",
    "액티브",
    "합성",
    "채권",
    "국고채",
    "회사채",
    "CD금리",
    "머니마켓",
    "MMF",
    "TDF",
)
_KR_PREFERRED_SUFFIXES = ("우", "우B", "우C")
_INVESTOR_FLOW_MIN_STREAK = 3
_INVESTOR_FLOW_STALE_SUFFIX = " · 1일 지연"
logger = logging.getLogger(__name__)


@dataclass
class _SnapshotLoadResult:
    """ROB-277 follow-up: snapshot loaders must surface latest-partition metadata
    even when no rows qualified, so freshness.primary can still report the
    correct snapshot date."""

    rows: list[dict[str, Any]]
    partition_date: dt.date | None
    partition_computed_at: datetime | None = None


def _investor_flow_chip_for_item(
    item: InvestorFlowItem,
) -> ScreenerInvestorFlowChip | None:
    if item.dataState == "missing":
        return None

    def _annotate(label: str) -> str:
        return (
            label + _INVESTOR_FLOW_STALE_SUFFIX if item.dataState == "stale" else label
        )

    snapshot = item.snapshotDate.isoformat() if item.snapshotDate else None

    if item.doubleBuy:
        streak = max(
            item.foreignConsecutiveBuyDays or 0,
            item.institutionConsecutiveBuyDays or 0,
        )
        label = "쌍끌이 매수" + (f" {streak}일" if streak >= 2 else "")
        return ScreenerInvestorFlowChip(
            label=_annotate(label),
            tone="double_buy",
            dataState=item.dataState,
            snapshotDate=snapshot,
        )
    if item.doubleSell:
        streak = max(
            item.foreignConsecutiveSellDays or 0,
            item.institutionConsecutiveSellDays or 0,
        )
        label = "쌍끌이 매도" + (f" {streak}일" if streak >= 2 else "")
        return ScreenerInvestorFlowChip(
            label=_annotate(label),
            tone="double_sell",
            dataState=item.dataState,
            snapshotDate=snapshot,
        )
    if (item.foreignConsecutiveBuyDays or 0) >= _INVESTOR_FLOW_MIN_STREAK:
        return ScreenerInvestorFlowChip(
            label=_annotate(f"외국인 {item.foreignConsecutiveBuyDays}일 순매수"),
            tone="foreign_buy",
            dataState=item.dataState,
            snapshotDate=snapshot,
        )
    if (item.foreignConsecutiveSellDays or 0) >= _INVESTOR_FLOW_MIN_STREAK:
        return ScreenerInvestorFlowChip(
            label=_annotate(f"외국인 {item.foreignConsecutiveSellDays}일 순매도"),
            tone="foreign_sell",
            dataState=item.dataState,
            snapshotDate=snapshot,
        )
    if (item.institutionConsecutiveBuyDays or 0) >= _INVESTOR_FLOW_MIN_STREAK:
        return ScreenerInvestorFlowChip(
            label=_annotate(f"기관 {item.institutionConsecutiveBuyDays}일 순매수"),
            tone="institution_buy",
            dataState=item.dataState,
            snapshotDate=snapshot,
        )
    if (item.institutionConsecutiveSellDays or 0) >= _INVESTOR_FLOW_MIN_STREAK:
        return ScreenerInvestorFlowChip(
            label=_annotate(f"기관 {item.institutionConsecutiveSellDays}일 순매도"),
            tone="institution_sell",
            dataState=item.dataState,
            snapshotDate=snapshot,
        )
    return None


def _investor_flow_item_from_screener_row(
    row: dict[str, Any],
) -> InvestorFlowItem | None:
    symbol = str(row.get("symbol") or "").strip().upper()
    if not symbol:
        return None

    snapshot_state = str(row.get("_screener_snapshot_state") or "").strip()
    data_state = (
        snapshot_state if snapshot_state in {"fresh", "stale", "missing"} else "fresh"
    )
    return InvestorFlowItem(
        symbol=symbol,
        market="kr",
        dataState=data_state,
        snapshotDate=row.get("snapshot_date"),
        collectedAt=row.get("collected_at"),  # ROB-277: passes through when available
        foreignNet=row.get("foreign_net"),
        institutionNet=row.get("institution_net"),
        individualNet=row.get("individual_net"),
        doubleBuy=bool(row.get("double_buy")),
        doubleSell=bool(row.get("double_sell")),
        foreignConsecutiveBuyDays=row.get("foreign_consecutive_buy_days"),
        foreignConsecutiveSellDays=row.get("foreign_consecutive_sell_days"),
        institutionConsecutiveBuyDays=row.get("institution_consecutive_buy_days"),
        institutionConsecutiveSellDays=row.get("institution_consecutive_sell_days"),
        individualConsecutiveBuyDays=row.get("individual_consecutive_buy_days"),
        individualConsecutiveSellDays=row.get("individual_consecutive_sell_days"),
    )


async def _hydrate_investor_flow_chips(
    *, db: Any, market: str, rows: list[dict[str, Any]]
) -> dict[str, ScreenerInvestorFlowChip]:
    if market != "kr" or not rows or db is None:
        return {}
    chips: dict[str, ScreenerInvestorFlowChip] = {}
    for row in rows:
        item = _investor_flow_item_from_screener_row(row)
        if item is None:
            continue
        chip = _investor_flow_chip_for_item(item)
        if chip is not None:
            chips[item.symbol] = chip

    symbols = sorted(
        {str(r.get("symbol")) for r in rows if r.get("symbol")} - set(chips.keys())
    )
    if not symbols:
        return chips
    try:
        items = await _latest_investor_flow_items(db=db, symbols=symbols, market="kr")
    except Exception as exc:  # noqa: BLE001
        logger.warning("screener investor-flow hydrate failed: %s", exc, exc_info=True)
        return chips
    for symbol, item in items.items():
        chip = _investor_flow_chip_for_item(item)
        if chip is not None:
            chips[symbol] = chip
            # ROB-277 follow-up: stash full dependency metadata on the row so
            # build_screener_results can classify the investor-flow dependency
            # via classify_investor_flow_partition.
            for r in rows:
                if str(r.get("symbol") or "").upper() == symbol:
                    r["_investor_flow_snapshot_date"] = getattr(
                        item, "snapshotDate", None
                    )
                    r["_investor_flow_collected_at"] = getattr(
                        item, "collectedAt", None
                    )
                    r["_investor_flow_data_state"] = getattr(item, "dataState", None)
                    break
    return chips


class _ScreeningServiceProto(Protocol):
    async def list_screening(self, /, **kwargs: Any) -> dict[str, Any]: ...


class _ResolverProto(Protocol):
    def relation(self, market: str, symbol: str) -> str: ...


def _safe_warning(message: Any) -> str:
    text = _clean_text(message)
    if not text:
        return "스크리너 데이터 소스가 일시적으로 응답하지 않습니다."
    normalized_text = text.lower()
    noisy_markers = (
        "httpsconnectionpool(",
        "connectionerror",
        "connecterror",
        "nameresolutionerror",
        "nodename nor servname",
        "could not resolve host",
        "getaddrinfo()",
        "max retries exceeded",
        "api.finnhub.io",
        "api.upbit.com",
        "upbit",
        "coingecko",
        "scanner.tradingview.com",
        "query1.finance.yahoo.com",
        "query2.finance.yahoo.com",
        "token=",
    )
    if any(marker in normalized_text for marker in noisy_markers):
        return "외부 시세/스크리너 데이터 소스 연결이 일시적으로 불안정해 일부 결과를 갱신하지 못했습니다."
    if len(text) > _MAX_WARNING_CHARS:
        return text[: _MAX_WARNING_CHARS - 1].rstrip() + "…"
    return text


def _safe_warnings(messages: Sequence[Any]) -> list[str]:
    warnings: list[str] = []
    for message in messages:
        warning = _safe_warning(message)
        if warning not in warnings:
            warnings.append(warning)
    return warnings


def _is_kr_toss_common_stock(symbol: str, name: str | None) -> bool:
    """Best-effort Toss-compatible KR common-stock universe guard.

    `kr_symbol_universe` currently has no instrument-type column, so use the
    exchange universe plus conservative name/symbol heuristics to keep obvious
    ETFs/ETNs/preferred/SPAC/fund-like products out of Toss-like presets.  When
    a name is unavailable, allow the row rather than accidentally hiding ordinary
    stocks; production snapshot rows are name-hydrated from KRX metadata first.
    """
    normalized_symbol = _clean_text(symbol)
    normalized_name = _clean_text(name)
    if not normalized_symbol:
        return False
    if not normalized_name:
        return True

    compact_name = normalized_name.replace(" ", "").upper()
    display_name = normalized_name.upper()

    if normalized_name.endswith(_KR_PREFERRED_SUFFIXES):
        return False
    if any(display_name.startswith(prefix) for prefix in _KR_TOSS_ETF_PREFIXES):
        return False
    if any(token.upper() in display_name for token in _KR_TOSS_EXCLUDED_NAME_TOKENS):
        return False
    if compact_name.endswith("ETN") or compact_name.endswith("ETF"):
        return False
    return True


def _external_failure_warning(exc: BaseException) -> str:
    # Keep provider hostnames/tokens out of logs and user-facing warnings; the
    # error class is enough to diagnose transient DNS/network failures here.
    logger.warning("invest screener upstream failed: %s", type(exc).__name__)
    return "외부 시세/스크리너 데이터 소스 연결이 일시적으로 불안정해 캐시된 결과만 표시합니다."


def _should_use_snapshot_first(screening_service: Any) -> bool:
    return (
        screening_service.__class__.__name__ == "ScreenerService"
        and screening_service.__class__.__module__ == "app.services.screener_service"
    )


def _double_buy_dependency_warnings(
    *,
    snapshot_rows: list[dict[str, Any]],
    now_market_date: dt.date,
) -> tuple[list[str], str | None]:
    """Return (warnings, state_override) for double_buy by dependency.

    snapshot_rows produced by load_double_buy_from_snapshots carry:
      - `_screener_snapshot_state`: "fresh" iff price_snapshot_date == flow_snapshot_date,
        else "stale" (price/flow partition divergence)
      - `snapshot_date`: the PRICE snapshot date used in the join
      - `flow_snapshot_date`: the investor-flow snapshot date used in the join

    The two warnings target distinct dependencies so the UI can tell whether
    price or flow is behind today's market date.
    """
    if not snapshot_rows:
        return [], None
    warnings: list[str] = []
    price_states = {row.get("_screener_snapshot_state") for row in snapshot_rows}
    if "stale" in price_states:
        warnings.append(
            "시세 스냅샷이 직전 영업일 기준이라 일부 데이터가 1일 지연되었습니다."
        )
    flow_dates = {row.get("flow_snapshot_date") for row in snapshot_rows}
    if flow_dates and all(
        isinstance(d, dt.date) and d < now_market_date for d in flow_dates
    ):
        warnings.append(
            "수급 스냅샷이 직전 영업일 기준이라 외인/기관 정보가 1일 지연되었습니다."
        )
    state_override = "stale" if warnings else None
    return warnings, state_override


async def _load_consecutive_gainers_from_snapshots(
    session: AsyncSession | None,
    *,
    market: str,
    limit: int = _SNAPSHOT_FIRST_LIMIT,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> _SnapshotLoadResult | None:
    """Return qualifying rows from the latest snapshot partition only.

    Returns None when the check could not be performed (no session, wrong market,
    DB error, or no snapshots exist in the table at all). Returns a
    _SnapshotLoadResult with an empty rows list when the latest partition was found
    but contains no qualifying rows — callers must NOT fall back to external
    screening in that case, because historical qualifying rows from older partitions
    must not be surfaced as current results. The partition_date is always populated
    when a non-None result is returned so freshness.primary has the correct date
    even when 0 rows qualify.
    """
    if session is None or market not in {"kr", "us"}:
        return None

    from app.models.invest_screener_snapshot import InvestScreenerSnapshot
    from app.services.invest_screener_snapshots.freshness import (
        classify_state,
        expected_baseline_date,
    )

    # ROB-281: session-aware baseline. In the KR 07:40–16:19 KST pre-market
    # window (or US pre-17:20 ET window), the expected baseline is the prior
    # trading session, not today — fixes the regression where a fresh
    # prior-day partition is labeled stale after KST/ET midnight rollover.
    now_utc = now()
    today = expected_baseline_date(market, now=now_utc)

    # Step 1: resolve the latest snapshot partition date.
    # This prevents older qualifying partitions from leaking into current results
    # when the latest partition has zero qualifiers (the known stale-data bug).
    latest_date_stmt = sa.select(
        sa.func.max(InvestScreenerSnapshot.snapshot_date)
    ).where(InvestScreenerSnapshot.market == market)
    try:
        latest_date_result = await session.execute(latest_date_stmt)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "failed to read invest_screener_snapshots max date: %s", exc, exc_info=True
        )
        return None
    latest_snapshot_date = latest_date_result.scalar_one_or_none()
    if latest_snapshot_date is None:
        return None  # no snapshots in the table; fall through to external

    # Step 2: qualify rows only within that one partition.  KR rows are
    # over-fetched because the Toss-compatible common-stock guard runs after
    # KRX-universe name hydration; limiting before that filter would let ETF/ETN
    # rows crowd out ordinary stocks below the first page.
    candidate_limit = limit
    if market == "kr":
        candidate_limit = max(limit * 5, limit + 120)
    stmt = (
        sa.select(InvestScreenerSnapshot)
        .where(
            InvestScreenerSnapshot.market == market,
            InvestScreenerSnapshot.snapshot_date == latest_snapshot_date,
            InvestScreenerSnapshot.consecutive_up_days >= 5,
            InvestScreenerSnapshot.week_change_rate >= 0,
        )
        .order_by(
            InvestScreenerSnapshot.week_change_rate.desc().nullslast(),
            InvestScreenerSnapshot.consecutive_up_days.desc(),
            InvestScreenerSnapshot.change_rate.desc().nullslast(),
            InvestScreenerSnapshot.symbol.asc(),
        )
        .limit(candidate_limit)
    )
    try:
        result = await session.execute(stmt)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "failed to read invest_screener_snapshots: %s", exc, exc_info=True
        )
        return None

    candidate_snaps = result.scalars().all()

    symbol_names: dict[str, str] = {}
    if market == "kr" and candidate_snaps:
        from app.models.kr_symbol_universe import KRSymbolUniverse

        candidate_symbols = [snap.symbol for snap in candidate_snaps]
        try:
            name_result = await session.execute(
                sa.select(KRSymbolUniverse.symbol, KRSymbolUniverse.name).where(
                    KRSymbolUniverse.symbol.in_(candidate_symbols),
                    KRSymbolUniverse.is_active.is_(True),
                )
            )
            symbol_names = {row.symbol: row.name for row in name_result.all()}
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "failed to read kr_symbol_universe for screener filtering: %s",
                exc,
                exc_info=True,
            )

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for snap in candidate_snaps:
        if snap.symbol in seen:
            continue
        if market == "kr" and not _is_kr_toss_common_stock(
            snap.symbol, symbol_names.get(snap.symbol)
        ):
            continue
        seen.add(snap.symbol)
        state = classify_state(
            snapshot_date=snap.snapshot_date,
            computed_at=snap.computed_at,
            closes_window_len=len(snap.closes_window or []),
            today_trading_date_value=today,
            now=now_utc,
        )
        rows.append(
            {
                "symbol": snap.symbol,
                "market": market,
                "name": symbol_names.get(snap.symbol),
                "close": float(snap.latest_close)
                if snap.latest_close is not None
                else None,
                "change_rate": float(snap.change_rate)
                if snap.change_rate is not None
                else None,
                "change_amount": float(snap.change_amount)
                if snap.change_amount is not None
                else None,
                "consecutive_up_days": snap.consecutive_up_days,
                "week_change_rate": float(snap.week_change_rate)
                if snap.week_change_rate is not None
                else None,
                "volume": snap.daily_volume,
                "daily_closes": list(snap.closes_window or []),
                "snapshot_date": snap.snapshot_date,  # ROB-277
                "computed_at": snap.computed_at,  # ROB-277
                "_screener_snapshot_state": state,
            }
        )
        if len(rows) >= limit:
            break
    # Rows are already ordered by the SQL ORDER BY; no Python re-sort needed.
    # ROB-277 follow-up: always return partition metadata even when rows is empty,
    # so freshness.primary surfaces the correct snapshot date.
    partition_computed_at: datetime | None = None
    if rows:
        partition_computed_at = rows[0].get("computed_at")
    elif candidate_snaps:
        # No qualifying rows after filtering, but we can grab computed_at from
        # any snap in the partition (they share the same snapshot_date).
        partition_computed_at = getattr(candidate_snaps[0], "computed_at", None)
    return _SnapshotLoadResult(
        rows=rows,
        partition_date=latest_snapshot_date,
        partition_computed_at=partition_computed_at,
    )


async def load_consecutive_gainers_from_snapshots(
    session: AsyncSession | None,
    *,
    market: str,
    limit: int = _SNAPSHOT_FIRST_LIMIT,
) -> list[dict[str, Any]] | None:
    """Public, uniform-contract wrapper over ``_load_consecutive_gainers_from_snapshots``.

    Returns the qualifying snapshot rows (each carrying ``_screener_snapshot_state``),
    ``[]`` when the latest partition has no qualifiers, or ``None`` when no partition
    exists / the check could not run — matching the ``load_double_buy_from_snapshots``
    and ``load_high_yield_value_from_snapshots`` contract so the candidate_universe
    collector can treat all three Toss-parity presets uniformly (ROB-363).
    """
    result = await _load_consecutive_gainers_from_snapshots(
        session, market=market, limit=limit
    )
    if result is None:
        return None
    return result.rows


async def _load_investor_flow_discovery_from_snapshots(
    session: AsyncSession | None,
    *,
    market: str,
    limit: int = 20,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> _SnapshotLoadResult | None:
    """Return MVP 수급 discovery rows from persisted investor_flow_snapshots.

    This preset is intentionally snapshot-only/read-only: if durable snapshots are
    unavailable, callers may fall back to the generic screener service, but no
    request-time Naver scraping is introduced here.

    Returns a _SnapshotLoadResult with partition_date populated even when no rows
    qualify, so freshness.primary can surface the correct snapshot date.
    """
    if session is None or market != "kr":
        return None

    from app.models.investor_flow_snapshot import InvestorFlowSnapshot

    latest_date_stmt = sa.select(sa.func.max(InvestorFlowSnapshot.snapshot_date)).where(
        InvestorFlowSnapshot.market == "kr"
    )
    try:
        latest_date_result = await session.execute(latest_date_stmt)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "failed to read investor_flow_snapshots max date: %s", exc, exc_info=True
        )
        return None
    latest_snapshot_date = latest_date_result.scalar_one_or_none()
    if latest_snapshot_date is None:
        return None

    candidate_limit = max(limit * 5, limit + 60)
    stmt = (
        sa.select(InvestorFlowSnapshot)
        .where(
            InvestorFlowSnapshot.market == "kr",
            InvestorFlowSnapshot.snapshot_date == latest_snapshot_date,
            sa.or_(
                InvestorFlowSnapshot.double_buy.is_(True),
                InvestorFlowSnapshot.foreign_consecutive_buy_days
                >= _INVESTOR_FLOW_MIN_STREAK,
                InvestorFlowSnapshot.foreign_net_buy_rank.is_not(None),
            ),
        )
        .order_by(
            InvestorFlowSnapshot.double_buy.desc(),
            InvestorFlowSnapshot.foreign_consecutive_buy_days.desc().nullslast(),
            InvestorFlowSnapshot.foreign_net_buy_rank.asc().nullslast(),
            InvestorFlowSnapshot.foreign_net.desc().nullslast(),
            InvestorFlowSnapshot.symbol.asc(),
        )
        .limit(candidate_limit)
    )
    try:
        result = await session.execute(stmt)
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to read investor_flow_snapshots: %s", exc, exc_info=True)
        return None
    candidate_snaps = result.scalars().all()

    symbol_names: dict[str, str] = {}
    if candidate_snaps:
        from app.models.kr_symbol_universe import KRSymbolUniverse

        candidate_symbols = [snap.symbol for snap in candidate_snaps]
        try:
            name_result = await session.execute(
                sa.select(KRSymbolUniverse.symbol, KRSymbolUniverse.name).where(
                    KRSymbolUniverse.symbol.in_(candidate_symbols),
                    KRSymbolUniverse.is_active.is_(True),
                )
            )
            symbol_names = {row.symbol: row.name for row in name_result.all()}
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "failed to read kr_symbol_universe for investor-flow screener: %s",
                exc,
                exc_info=True,
            )

    from app.services.invest_screener_snapshots.freshness import (
        classify_investor_flow_partition,
        today_trading_date,
    )

    now_utc = now()
    today = today_trading_date(market, now=now_utc)

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for snap in candidate_snaps:
        if snap.symbol in seen:
            continue
        if not _is_kr_toss_common_stock(snap.symbol, symbol_names.get(snap.symbol)):
            continue
        seen.add(snap.symbol)
        state = classify_investor_flow_partition(
            snapshot_date=snap.snapshot_date,
            collected_at=snap.collected_at,
            today_trading_date_value=today,
            now=now_utc,
        )
        rows.append(
            {
                "symbol": snap.symbol,
                "market": "kr",
                "name": symbol_names.get(snap.symbol),
                "foreign_net": snap.foreign_net,
                "institution_net": snap.institution_net,
                "individual_net": snap.individual_net,
                "foreign_consecutive_buy_days": snap.foreign_consecutive_buy_days,
                "institution_consecutive_buy_days": snap.institution_consecutive_buy_days,
                "double_buy": snap.double_buy,
                "snapshot_date": snap.snapshot_date,
                "collected_at": snap.collected_at,
                "_screener_snapshot_state": state,
            }
        )
        if len(rows) >= limit:
            break
    # ROB-277 follow-up: surface partition metadata even when rows is empty so
    # freshness.primary has the correct snapshot date.
    partition_collected_at: datetime | None = None
    if rows:
        partition_collected_at = rows[0].get("collected_at")
    elif candidate_snaps:
        partition_collected_at = getattr(candidate_snaps[0], "collected_at", None)
    return _SnapshotLoadResult(
        rows=rows,
        partition_date=latest_snapshot_date,
        partition_computed_at=partition_collected_at,  # investor_flow has no computed_at; use collected_at
    )


async def _load_crypto_rows_from_snapshots(
    session: AsyncSession | None,
    *,
    preset_id: str,
    limit: int = 20,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> tuple[list[dict[str, Any]], str] | None:
    """Return crypto rows from the latest crypto snapshot partition.

    None means there is no usable snapshot partition and live provider fallback is
    allowed.  An empty list with a state means a latest fresh/partial partition
    exists but has no qualifiers for the preset; callers must not query older
    partitions or silently fall back.
    """
    if session is None:
        return None

    from app.services.invest_crypto_screener_snapshots.freshness import (
        classify_crypto_partition,
        today_crypto_snapshot_date,
    )
    from app.services.invest_crypto_screener_snapshots.repository import (
        InvestCryptoScreenerSnapshotsRepository,
    )

    repo = InvestCryptoScreenerSnapshotsRepository(session)
    try:
        latest_date = await repo.latest_partition()
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to read crypto screener snapshot partition: %s", exc)
        return None
    if latest_date is None:
        return None

    try:
        rows = await repo.list_latest(
            preset_id=preset_id,
            limit=limit,
            snapshot_date=latest_date,
        )
        coverage = await repo.coverage(today=today_crypto_snapshot_date(now()))
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to read crypto screener snapshots: %s", exc)
        return None

    state = classify_crypto_partition(
        latest_partition_date=coverage.latest_partition_date,
        row_count=coverage.latest_partition_count,
        last_computed_at=coverage.last_computed_at,
        now=now(),
    )
    if state in {"missing", "stale"}:
        return None

    mapped: list[dict[str, Any]] = []
    for snap in rows:
        mapped.append(
            {
                "symbol": snap.symbol,
                "market": "crypto",
                "name": snap.name,
                "close": float(snap.latest_close)
                if snap.latest_close is not None
                else None,
                "change_rate": float(snap.change_rate)
                if snap.change_rate is not None
                else None,
                "change_amount": float(snap.change_amount)
                if snap.change_amount is not None
                else None,
                "trade_amount_24h": float(snap.trade_amount_24h)
                if snap.trade_amount_24h is not None
                else None,
                "volume_24h": float(snap.volume_24h)
                if snap.volume_24h is not None
                else None,
                "volume_24h_usd": float(snap.volume_24h_usd)
                if snap.volume_24h_usd is not None
                else None,
                "market_cap": float(snap.market_cap)
                if snap.market_cap is not None
                else None,
                "rsi": float(snap.rsi) if snap.rsi is not None else None,
                "adx": float(snap.adx) if snap.adx is not None else None,
                "source": "tvscreener_upbit",
                "computed_at": snap.computed_at.isoformat()
                if getattr(snap, "computed_at", None) is not None
                else None,
                "_screener_snapshot_state": state,
            }
        )
    return mapped, state


def build_screener_presets(market: str = "kr") -> ScreenerPresetsResponse:
    requested_market = _normalize_market(market)
    return ScreenerPresetsResponse(
        presets=preset_definitions(requested_market),
        selectedPresetId=CRYPTO_DEFAULT_PRESET_ID
        if requested_market == "crypto"
        else DEFAULT_PRESET_ID,
    )


_METRIC_FIELD: dict[str, str] = {
    "consecutive_gainers": "week_change_rate",
    "cheap_value": "per",
    "steady_dividend": "dividend_yield",
    "oversold_recovery": "rsi",
    "kr_high_volume_surge": "volume",
    "growth_expectation": "change_rate",
    "high_yield_value": "roe",
    "undervalued_breakout": "high_52w_proximity",
    "investor_flow_momentum": "foreign_net",
    "double_buy": "change_rate",  # NEW — placeholder; Task 3 wires snapshot-first branch
    "crypto_high_volume": "trade_amount_24h",
    "crypto_oversold": "rsi",
    "crypto_momentum": "change_rate",
    "profitable_company": "roe",
    "undervalued_growth": "earnings_growth_3y_avg",
    "stable_growth": "roe",
    "future_dividend_king": "dividend_yield",
    "growth_expectation_toss": "earnings_growth_qoq",
}


def _format_change_pct(rate: float | None) -> tuple[str, ChangeDirection]:
    if rate is None:
        return "-", "flat"
    direction: ChangeDirection = "up" if rate > 0 else "down" if rate < 0 else "flat"
    sign = "+" if rate > 0 else ""
    return f"{sign}{rate:.2f}%", direction


def _format_change_amount(amount: float | None, market: str = "kr") -> str:
    if amount is None:
        return "-"
    sign = "+" if amount > 0 else "-" if amount < 0 else ""
    if market == "us":
        return f"{sign}${abs(float(amount)):,.2f}"
    return f"{sign}{abs(int(amount)):,}원"


def _is_krw_crypto_symbol(symbol: str) -> bool:
    return symbol.upper().startswith("KRW-")


def _format_crypto_price(close: float | None, symbol: str) -> str:
    if close is None:
        return "-"
    if _is_krw_crypto_symbol(symbol):
        if close >= 1:
            return f"{float(close):,.0f}원"
        return f"{float(close):,.4f}원"
    return f"${float(close):,.4f}"


def _format_crypto_change_amount(amount: float | None, symbol: str) -> str:
    if amount is None:
        return "-"
    sign = "+" if amount > 0 else "-" if amount < 0 else ""
    value = abs(float(amount))
    if _is_krw_crypto_symbol(symbol):
        if value >= 1:
            return f"{sign}{value:,.0f}원"
        return f"{sign}{value:,.4f}원"
    return f"{sign}${value:,.4f}"


def _format_price(close: float | None, market: str = "kr") -> str:
    if close is None:
        return "-"
    if market == "us":
        return f"${float(close):,.2f}"
    return f"{int(close):,}원"


def _format_market_cap_kr(market_cap: float | None) -> str:
    if market_cap is None:
        return "-"
    eok = market_cap / 100_000_000.0
    if eok >= 10_000:
        jo = eok / 10_000.0
        return f"{jo:,.1f}조원"
    return f"{eok:,.0f}억원"


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_market(raw: Any) -> str:
    market = _clean_text(raw).lower()
    return market if market in _VALID_MARKETS else "kr"


def _normalize_symbol(row: dict[str, Any], market: str) -> tuple[str, list[str]]:
    symbol = ""
    for key in ("symbol", "code", "short_code", "ticker"):
        symbol = _clean_text(row.get(key))
        if symbol:
            break

    if not symbol:
        return "", ["종목코드 데이터 준비중"]

    if market == "kr":
        _, sep, suffix = symbol.rpartition(":")
        if sep and suffix.isdigit() and len(suffix) == 6:
            symbol = suffix
        return symbol, []

    return symbol.upper(), []


def _normalize_crypto_symbol(row: dict[str, Any]) -> tuple[str, list[str]]:
    raw_values = [
        _clean_text(row.get(key))
        for key in ("symbol", "original_market", "code", "ticker", "market_code")
    ]
    for raw in raw_values:
        if not raw:
            continue
        symbol = raw.upper().replace("/", "-").replace("_", "-")
        if symbol.startswith("KRW-") and len(symbol) > 4:
            return symbol, []
        if symbol.endswith("-KRW") and len(symbol) > 4:
            return f"KRW-{symbol[:-4]}", []
        if symbol.startswith("UPBIT:"):
            pair = symbol.split(":", 1)[1]
            if pair.endswith("KRW") and len(pair) > 3:
                return f"KRW-{pair[:-3]}", []
        if symbol.endswith("KRW") and "-" not in symbol and ":" not in symbol:
            return f"KRW-{symbol[:-3]}", []
    return "", ["비KRW 가상자산 행은 제외했습니다."]


def _coerce_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "")
        if not cleaned:
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _market_cap_from_market_cap_field(value: float | None, market: str) -> float | None:
    if value is None or value <= 0:
        return None
    if market == "kr":
        # KR upstream rows can contain either KRW (TradingView-style) or 억원
        # (KRX-style). A KRW market cap under 1조 is still plausible, so don't
        # require a 1조 threshold before treating the value as already-KRW.
        if value >= 100_000_000:
            return value
        return value * 100_000_000
    if value >= 1_000_000_000_000:
        return value
    return None


def _normalize_market_cap_krw(
    row: dict[str, Any], market: str
) -> tuple[float | None, list[str]]:
    """Return display-safe KRW market cap plus row warnings.

    Upstream screener rows can mix `market_cap_krw` (already KRW) with
    source-dependent `market_cap` units. Values above 10,000조 KRW are
    implausible for a single screener row, so prefer a plausible fallback or
    hide the label instead of rendering absurd values such as 414,671.4조원.
    """
    market_cap_krw = _coerce_float(row.get("market_cap_krw"))
    market_cap = _coerce_float(row.get("market_cap"))
    fallback = _market_cap_from_market_cap_field(market_cap, market)

    if market_cap_krw is not None and market_cap_krw > 0:
        if market_cap_krw <= _KR_ABSURD_MARKET_CAP_KRW:
            return market_cap_krw, []
        if fallback is not None and fallback <= _KR_ABSURD_MARKET_CAP_KRW:
            return fallback, ["시가총액 단위 보정됨"]
        return None, ["시가총액 데이터 확인 필요"]

    if fallback is not None and fallback <= _KR_ABSURD_MARKET_CAP_KRW:
        return fallback, []
    if fallback is not None:
        return None, ["시가총액 데이터 확인 필요"]
    return None, []


def _format_market_cap_us(market_cap: float | None) -> str:
    if market_cap is None or market_cap <= 0:
        return "-"
    if market_cap >= 1_000_000_000_000:
        return f"${market_cap / 1_000_000_000_000:.2f}T"
    if market_cap >= 1_000_000_000:
        return f"${market_cap / 1_000_000_000:.1f}B"
    if market_cap >= 1_000_000:
        return f"${market_cap / 1_000_000:.1f}M"
    return f"${market_cap:,.0f}"


def _format_market_cap(row: dict[str, Any], market: str) -> tuple[str, list[str]]:
    if market in {"us", "crypto"}:
        market_cap = _coerce_float(row.get("market_cap_usd"))
        if market_cap is None:
            market_cap = _coerce_float(row.get("market_cap"))
        return _format_market_cap_us(market_cap), []
    market_cap, warnings = _normalize_market_cap_krw(row, market)
    return _format_market_cap_kr(market_cap), warnings


def _format_volume(volume: float | None) -> str:
    if volume is None:
        return "-"
    return f"{int(volume):,}"


def _format_volume_label(row: dict[str, Any], market: str) -> str:
    if market == "crypto":
        for key in ("trade_amount_24h", "value_traded", "volume_24h_usd", "volume"):
            value = _coerce_float(row.get(key))
            if value is not None:
                return _format_volume(value)
        return "-"
    return _format_volume(_coerce_float(row.get("volume")))


def _metric_raw_value(field: str, row: dict[str, Any]) -> Any:
    if field == "trade_amount_24h":
        for key in ("trade_amount_24h", "value_traded", "volume_24h_usd", "volume"):
            value = row.get(key)
            if value is not None:
                return value
        return None
    return row.get(field)


def calculate_consecutive_up_days(closes: Sequence[float | int | None]) -> int | None:
    values = [_coerce_float(v) for v in closes]
    values = [v for v in values if v is not None]
    if len(values) < 2:
        return None
    streak = 0
    for current, previous in zip(
        reversed(values[1:]), reversed(values[:-1]), strict=False
    ):
        if current > previous:
            streak += 1
            continue
        break
    return streak


def _enrich_consecutive_up_days(preset_id: str, row: dict[str, Any]) -> None:
    if preset_id != "consecutive_gainers" or row.get("consecutive_up_days") is not None:
        return
    history = row.get("daily_closes") or row.get("close_history") or row.get("closes")
    if isinstance(history, Sequence) and not isinstance(history, (str, bytes)):
        row["consecutive_up_days"] = calculate_consecutive_up_days(history)


def _metric_value_label(preset_id: str, row: dict[str, Any]) -> tuple[str, list[str]]:
    field = _METRIC_FIELD.get(preset_id)
    if not field:
        return "-", []
    value = _metric_raw_value(field, row)
    if value is None:
        if field == "consecutive_up_days":
            return "-", ["연속상승 데이터 준비중"]
        if field == "week_change_rate":
            return "-", ["주가등락률 데이터 준비중"]
        return "-", [f"{field.upper()} 데이터 준비중"]
    if field == "consecutive_up_days":
        return f"{int(value)}일", []
    if field in ("week_change_rate", "change_rate"):
        sign = "+" if value > 0 else ""
        return f"{sign}{value:.2f}%", []
    if field in ("per", "pbr", "rsi"):
        return f"{float(value):.1f}", []
    if field == "roe":
        return f"{float(value):.1f}%", []
    if field == "dividend_yield":
        return f"{float(value):.2f}%", []
    if field in ("volume", "trade_amount_24h"):
        return f"{int(float(value)):,}", []
    if field == "foreign_net":
        sign = "+" if int(value) > 0 else "−" if int(value) < 0 else ""
        return f"{sign}{abs(int(value)):,}주", []
    return str(value), []


def _source_label(source: str) -> str:
    return {
        "snapshot_cache": "스냅샷 캐시",
        "tvscreener_upbit": "TV Screener Upbit",
        "mcp_screen_stocks": "MCP screen_stocks",
        "upbit_official": "Upbit 공식 데이터",
        "coingecko_reference": "CoinGecko 참고 데이터",
        "naver_reference": "Naver 참고 데이터",
        "external_reference": "외부 참고 데이터",
    }.get(source, source)


def _source_context(
    source: str,
    *,
    state: str = "supported",
    fetched_at: str | None = None,
    detail: str | None = None,
) -> ScreenerSourceContext:
    return ScreenerSourceContext(
        source=source,  # type: ignore[arg-type]
        label=_source_label(source),
        state=state,  # type: ignore[arg-type]
        fetchedAt=fetched_at,
        detail=detail,
    )


def _dedupe_sources(
    sources: Sequence[ScreenerSourceContext],
) -> list[ScreenerSourceContext]:
    deduped: list[ScreenerSourceContext] = []
    seen: set[tuple[str, str]] = set()
    for source in sources:
        key = (source.source, source.state)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(source)
    return deduped


def _crypto_row_source_context(
    row: dict[str, Any], *, snapshot_was_checked: bool, raw: dict[str, Any]
) -> list[ScreenerSourceContext]:
    sources: list[ScreenerSourceContext] = []
    fetched_at = _clean_text(row.get("computed_at") or row.get("fetched_at")) or None
    if snapshot_was_checked:
        sources.append(
            _source_context("snapshot_cache", state="cached", fetched_at=fetched_at)
        )
    else:
        sources.append(_source_context("mcp_screen_stocks", state="fallback"))

    row_source = _clean_text(row.get("source")).lower()
    meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
    meta_source = _clean_text(
        meta.get("source") if isinstance(meta, dict) else ""
    ).lower()
    if row_source in {"tvscreener", "tvscreener_upbit"} or meta_source in {
        "tvscreener",
        "tvscreener_upbit",
    }:
        sources.append(_source_context("tvscreener_upbit"))
    if row.get("market_warning") is not None or row.get("warning") is not None:
        sources.append(_source_context("upbit_official"))
    if row.get("market_cap") is not None or row.get("market_cap_rank") is not None:
        sources.append(_source_context("coingecko_reference", state="reference_only"))
    return _dedupe_sources(sources)


def _screen_response_sources(
    *, requested_market: str, snapshot_was_checked: bool, raw: dict[str, Any]
) -> list[ScreenerSourceContext]:
    if requested_market != "crypto":
        return []
    sample_row: dict[str, Any] = {}
    rows = raw.get("results") or raw.get("stocks") or []
    if rows:
        sample_row = rows[0]
    sources = _crypto_row_source_context(
        sample_row, snapshot_was_checked=snapshot_was_checked, raw=raw
    )
    meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
    if isinstance(meta, dict) and meta.get("coingecko_cached"):
        sources.append(_source_context("coingecko_reference", state="reference_only"))
    return _dedupe_sources(sources)


def _crypto_risk_context(row: dict[str, Any]) -> list[ScreenerRiskContext]:
    risks: list[ScreenerRiskContext] = []
    if row.get("market_warning") or row.get("warning"):
        risks.append(
            ScreenerRiskContext(
                kind="market_warning",
                label="Upbit 유의 종목",
                severity="warning",
                source="upbit_official",
            )
        )
    if (
        _coerce_float(row.get("close") or row.get("price") or row.get("current_price"))
        is None
    ):
        risks.append(
            ScreenerRiskContext(
                kind="data_unavailable",
                label="가격 데이터 준비중",
                severity="warning",
            )
        )
    rsi = _coerce_float(row.get("rsi"))
    if rsi is not None and rsi <= 35:
        risks.append(
            ScreenerRiskContext(
                kind="low_rsi",
                label=f"RSI {rsi:.1f} 저점권",
                severity="info",
                source="tvscreener_upbit",
            )
        )
    adx = _coerce_float(row.get("adx"))
    if adx is not None and adx >= 25:
        risks.append(
            ScreenerRiskContext(
                kind="trend_strength",
                label=f"ADX {adx:.1f} 추세 강도",
                severity="info",
                source="tvscreener_upbit",
            )
        )
    return risks


def _crypto_candidate_source(row: dict[str, Any]) -> str:
    row_source = _clean_text(row.get("source")).lower()
    if row_source in {"tvscreener", "tvscreener_upbit"}:
        return "tvscreener_upbit"
    if row_source in {"upbit", "upbit_official"}:
        return "upbit_official"
    if row_source:
        return "external_reference"
    return "mcp_screen_stocks"


def _crypto_candidate_context(
    row: dict[str, Any], preset_id: str
) -> ScreenerCandidateContext | None:
    from app.services.screener_evidence import build_candidate_evidence

    evidence = build_candidate_evidence(market="crypto", preset=preset_id, rows=[row])
    if not evidence:
        return None
    ev = evidence[0]
    if ev.score_label == "-":
        return None
    if not ev.reasons:
        return None
    return ScreenerCandidateContext(
        scoreLabel=ev.score_label,
        reasons=ev.reasons,
        source=ev.source,  # type: ignore[arg-type]
    )


def _format_relative_korean(delta_seconds: int) -> str:
    if delta_seconds <= 60:
        return "방금 갱신"
    minutes = delta_seconds // 60
    if minutes < 60:
        return f"{minutes}분 전 갱신"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}시간 전 갱신"
    days = hours // 24
    return f"{days}일 전 갱신"


def _is_kr_market_open(at_kst: datetime) -> bool:
    if at_kst.weekday() >= 5:
        return False
    return _KR_OPEN <= at_kst.time() <= _KR_CLOSE


def _build_freshness(
    *,
    raw_timestamp: str | None,
    cache_hit: bool,
    market: str,
    now: Callable[[], datetime],
    dataState: str = "missing",
    # NEW (all keyword-only, defaulted) — ROB-277 Task 4
    primary_kind: Literal["screener_snapshot", "live", "fallback"] = "live",
    primary_snapshot_date: dt.date | None = None,
    primary_computed_at: datetime | None = None,
    primary_source: str | None = None,
    dependency_specs: list[dict[str, Any]] | None = None,
) -> ScreenerFreshness:
    """ROB-277: split served time vs data 기준.

    raw_timestamp historically served as both, which is the bug this fixes.  When
    primary_kind == "screener_snapshot", primary fields derive from the partition
    date/computed_at and the user-visible asOfLabel reflects that — NOT now().

    dependency_specs items shape:
      {"kind": "investor_flow", "snapshot_date": date|None, "collected_at": dt|None,
       "data_state": "fresh|partial|stale|missing|fallback", "source": str|None}
    """
    from app.services.invest_screener_snapshots.freshness import (
        compute_overall_state,
        format_kst_as_of_label,
        session_label_for_partition,
    )

    now_utc = now()
    # served = response refresh time, always now() of the request
    served_at_utc = now_utc
    served_relative = "방금"

    # data_basis_at is the INTERNAL data-as-of timestamp used only for delta /
    # relativeLabel / previous_session detection and for the live-path asOfLabel
    # fallback. It is NOT exported as freshness.fetchedAt — per ROB-277 §D1 the
    # exported fetchedAt is a servedAt alias. For snapshot kind we still compute
    # data_basis_at from partition computed_at (or EOD 15:30 KST) so the staleness
    # logic / previous_session detection have something meaningful to compare;
    # the user-visible asOfLabel and primary.computedAt carry the real data 기준.
    if primary_kind == "screener_snapshot" and primary_snapshot_date is not None:
        if primary_computed_at is not None:
            data_basis_at = primary_computed_at
            if data_basis_at.tzinfo is None:
                data_basis_at = data_basis_at.replace(tzinfo=UTC)
        else:
            # Treat as end-of-trading-day in KST.
            basis_kst_eod = datetime.combine(
                primary_snapshot_date, _time(15, 30), tzinfo=_KST
            )
            data_basis_at = basis_kst_eod.astimezone(UTC)
    elif raw_timestamp:
        try:
            data_basis_at = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
        except ValueError:
            data_basis_at = now_utc
        if data_basis_at.tzinfo is None:
            data_basis_at = data_basis_at.replace(tzinfo=UTC)
    else:
        data_basis_at = now_utc

    data_basis_kst = data_basis_at.astimezone(_KST)
    delta = max(0, int((now_utc - data_basis_at).total_seconds()))
    now_kst = now_utc.astimezone(_KST)
    market_open = market == "kr" and _is_kr_market_open(now_kst)

    if cache_hit and primary_kind == "screener_snapshot":
        # Snapshot-first responses are persisted-cache reads.  Keep the legacy
        # top-level enum stable as "cached" even when the snapshot partition is
        # stale; staleness is carried by primary/dependency dataState instead.
        source: Literal["live", "cached", "previous_session"] = "cached"
        relative = _format_relative_korean(delta)
    elif not market_open and delta > _CACHE_HIT_FRESH_SECONDS * 4:
        source = "previous_session"
        relative = "전 거래일 기준"
    elif cache_hit:
        source = "cached"
        relative = _format_relative_korean(delta)
    else:
        source = "live"
        relative = _format_relative_korean(delta)

    # Build primary object
    primary: ScreenerFreshnessPrimary | None = None
    if primary_kind == "screener_snapshot" and primary_snapshot_date is not None:
        # ROB-281: append session token (KRX preliminary / NXT final / US
        # post-close) to the data-基準 label so the UI can disambiguate which
        # KR/US scheduled slot produced this partition. Token is omitted when
        # session_label_for_partition returns None (unknown market or rare
        # 07:00–07:39 KST gap) — preserves ROB-277 served vs data-as-of split.
        base_as_of_label = format_kst_as_of_label(
            snapshot_date=primary_snapshot_date,
            computed_at=primary_computed_at,
        )
        session_token = session_label_for_partition(market, primary_computed_at)
        as_of_label = (
            f"{base_as_of_label} ({session_token})"
            if session_token
            else base_as_of_label
        )
        primary = ScreenerFreshnessPrimary(
            kind="screener_snapshot",
            snapshotDate=primary_snapshot_date.isoformat(),
            computedAt=primary_computed_at.astimezone(UTC).isoformat()
            if primary_computed_at is not None
            else None,
            asOfLabel=as_of_label,
            dataState=dataState,  # type: ignore[arg-type]
            source=primary_source,
        )
    elif primary_kind in {"live", "fallback"}:
        primary = ScreenerFreshnessPrimary(
            kind=primary_kind,
            snapshotDate=None,
            computedAt=None,
            asOfLabel=data_basis_kst.strftime("%Y.%m.%d %H:%M 기준"),
            dataState=dataState,  # type: ignore[arg-type]
            source=primary_source,
        )

    # Build dependencies list
    dependencies: list[ScreenerFreshnessDependency] = []
    for spec in dependency_specs or []:
        dep_snap = spec.get("snapshot_date")
        dep_collected = spec.get("collected_at")
        lag_label: str | None = None
        if dep_snap is not None and primary_snapshot_date is not None:
            # NOTE: calendar-day diff between two snapshot dates. Both
            # partitions are emitted on trading days, so on consecutive trading
            # days this matches the trading-day count; weekend/holiday gaps will
            # inflate it (e.g. Fri vs Mon = 3 instead of 1). We use "일" (not
            # "거래일") on the label until a trading-day-aware diff is added.
            lag_days = (primary_snapshot_date - dep_snap).days
            if lag_days >= 1:
                lag_label = f"{lag_days}일 지연"
        dependencies.append(
            ScreenerFreshnessDependency(
                kind=spec.get("kind", "investor_flow"),
                snapshotDate=dep_snap.isoformat() if dep_snap is not None else None,
                collectedAt=dep_collected.astimezone(UTC).isoformat()
                if dep_collected is not None
                else None,
                lagLabel=lag_label,
                dataState=spec.get("data_state", "missing"),  # type: ignore[arg-type]
                source=spec.get("source"),
            )
        )

    overall = compute_overall_state(
        primary_state=primary.dataState if primary is not None else dataState,  # type: ignore[arg-type]
        dependency_states=[d.dataState for d in dependencies],
    )

    return ScreenerFreshness(
        fetchedAt=served_at_utc.isoformat(),  # ROB-277 D1: fetchedAt is a servedAt alias
        asOfLabel=primary.asOfLabel
        if primary is not None
        else data_basis_kst.strftime("%Y.%m.%d %H:%M 기준"),
        relativeLabel=relative,
        cacheHit=bool(cache_hit),
        source=source,
        dataState=overall,  # alias of overallState (D1.c)
        servedAt=served_at_utc.isoformat(),
        servedRelativeLabel=served_relative,
        primary=primary,
        dependencies=dependencies,
        overallState=overall,
    )


async def build_screener_results(
    preset_id: str,
    screening_service: _ScreeningServiceProto,
    resolver: _ResolverProto,
    market: str = "kr",
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    session: AsyncSession | None = None,
) -> ScreenerResultsResponse:
    requested_market = _normalize_market(market)
    preset = get_preset(preset_id, requested_market)
    if preset is None:
        freshness = _build_freshness(
            raw_timestamp=None,
            cache_hit=False,
            market=requested_market,
            now=now,
        )
        return ScreenerResultsResponse(
            presetId=preset_id,
            title=preset_id,
            description="",
            filterChips=[],
            metricLabel="-",
            results=[],
            warnings=[f"알 수 없는 프리셋: {preset_id}"],
            freshness=freshness,
        )

    filters = screening_filters_for(preset_id, requested_market)
    # ROB-277 follow-up: loaders for consecutive_gainers and investor_flow_momentum
    # now return _SnapshotLoadResult so partition metadata threads through even when
    # 0 rows qualify. double_buy and crypto still return list|None directly.
    _snapshot_load_result: _SnapshotLoadResult | None = None
    _snapshot_check_result: list[dict[str, Any]] | None = None
    _snapshot_state_override: str | None = None
    _fundamentals_screen_result = None
    _snapshot_empty_warning = (
        "스크리너 스냅샷 업데이트가 필요해 최신 연속 상승세 결과를 표시하지 못했습니다."
    )
    if session is not None and _should_use_snapshot_first(screening_service):
        if preset_id == "consecutive_gainers":
            _snapshot_load_result = await _load_consecutive_gainers_from_snapshots(
                session,
                market=requested_market,
                limit=int(filters.get("limit") or _SNAPSHOT_FIRST_LIMIT),
                now=now,
            )
            if _snapshot_load_result is not None:
                _snapshot_check_result = _snapshot_load_result.rows
        elif preset_id == "investor_flow_momentum":
            _snapshot_load_result = await _load_investor_flow_discovery_from_snapshots(
                session,
                market=requested_market,
                limit=int(filters.get("limit") or _SNAPSHOT_FIRST_LIMIT),
                now=now,
            )
            if _snapshot_load_result is not None:
                _snapshot_check_result = _snapshot_load_result.rows
            _snapshot_empty_warning = (
                "최신 수급 스냅샷에서 조건에 맞는 결과가 없습니다."
            )
        elif preset_id == "double_buy":
            from app.services.invest_view_model.double_buy_screener import (
                load_double_buy_from_snapshots,
            )

            _snapshot_check_result = await load_double_buy_from_snapshots(
                session,
                market=requested_market,
                limit=int(filters.get("limit") or _SNAPSHOT_FIRST_LIMIT),
            )
            _snapshot_empty_warning = (
                "최신 수급/시세 스냅샷에서 쌍끌이 매수 조건에 맞는 종목이 없습니다."
            )
        elif preset_id == "high_yield_value":
            from app.services.invest_view_model.high_yield_value_screener import (
                load_high_yield_value_from_snapshots,
            )

            _snapshot_check_result = await load_high_yield_value_from_snapshots(
                session,
                market=requested_market,
                limit=int(filters.get("limit") or _SNAPSHOT_FIRST_LIMIT),
            )
            _snapshot_empty_warning = (
                "최신 밸류에이션 스냅샷에서 고수익 저평가 조건(ROE 15%↑·PER 0~10)에 "
                "맞는 종목이 없습니다."
            )
        elif preset_id == "undervalued_breakout":
            from app.services.invest_view_model.undervalued_breakout_screener import (
                load_undervalued_breakout_from_snapshots,
            )

            _snapshot_check_result = await load_undervalued_breakout_from_snapshots(
                session,
                market=requested_market,
                limit=int(filters.get("limit") or _SNAPSHOT_FIRST_LIMIT),
            )
            _snapshot_empty_warning = (
                "최신 밸류에이션/시세 스냅샷에서 저평가 탈출 조건"
                "(PER 0~10·PBR 0~1·신고가 근접)에 맞는 종목이 없습니다."
            )
        elif preset_id in FUNDAMENTALS_PRESET_SPECS:
            from app.services.invest_view_model.fundamentals_screener import (
                load_fundamentals_preset_from_snapshots,
            )

            _fundamentals_screen_result = await load_fundamentals_preset_from_snapshots(
                session,
                market=requested_market,
                spec=FUNDAMENTALS_PRESET_SPECS[preset_id],
                limit=int(filters.get("limit") or _SNAPSHOT_FIRST_LIMIT),
                now=now,
            )
            if _fundamentals_screen_result is not None:
                _snapshot_check_result = _fundamentals_screen_result.rows
                _snapshot_load_result = _SnapshotLoadResult(
                    rows=_fundamentals_screen_result.rows,
                    partition_date=_fundamentals_screen_result.valuation_partition_date,
                    partition_computed_at=None,
                )
            _snapshot_empty_warning = "최신 밸류에이션/재무 스냅샷에서 해당 프리셋 조건에 맞는 종목이 없습니다."
        elif requested_market == "crypto":
            _crypto_snapshot_result = await _load_crypto_rows_from_snapshots(
                session,
                preset_id=preset_id,
                limit=int(filters.get("limit") or _SNAPSHOT_FIRST_LIMIT),
                now=now,
            )
            if _crypto_snapshot_result is not None:
                _snapshot_check_result, _snapshot_state_override = (
                    _crypto_snapshot_result
                )
                _snapshot_empty_warning = (
                    "최신 암호화폐 스크리너 스냅샷에서 조건에 맞는 결과가 없습니다."
                )

    if preset_id == "investor_flow_momentum" and _snapshot_check_result is None:
        # This preset is deliberately persisted-snapshot-only. Do not fall
        # through to the generic screener provider, which neither supports the
        # investor-flow filters nor guarantees no request-time external lookup.
        _snapshot_check_result = []
        _snapshot_state_override = "missing"
        _snapshot_empty_warning = (
            "수급 스냅샷이 아직 적재되지 않아 수급 모멘텀 후보를 표시할 수 없습니다."
        )

    if preset_id == "double_buy" and _snapshot_check_result is None:
        # snapshot-only preset; generic provider has no Toss-parity filters
        _snapshot_check_result = []
        _snapshot_state_override = "missing"
        _snapshot_empty_warning = "수급 또는 시세 스냅샷이 아직 적재되지 않아 쌍끌이 매수 후보를 표시할 수 없습니다."

    if preset_id == "high_yield_value" and _snapshot_check_result is None:
        # snapshot-only preset; the generic provider has no ROE filter and could
        # half-apply (PER only) the rule — never fall through to it.
        _snapshot_check_result = []
        _snapshot_state_override = "missing"
        _snapshot_empty_warning = "밸류에이션 스냅샷이 아직 적재되지 않아 고수익 저평가 후보를 표시할 수 없습니다."

    if preset_id == "undervalued_breakout" and _snapshot_check_result is None:
        # snapshot-only; the generic provider has no 52-week-high proximity filter.
        _snapshot_check_result = []
        _snapshot_state_override = "missing"
        _snapshot_empty_warning = "밸류에이션/시세 스냅샷이 아직 적재되지 않아 저평가 탈출 후보를 표시할 수 없습니다."

    if preset_id in FUNDAMENTALS_PRESET_SPECS and _snapshot_check_result is None:
        # snapshot-only; the generic provider has neither a gross-margin nor a
        # fundamentals filter and could half-apply the rule — never fall through.
        _snapshot_check_result = []
        _snapshot_state_override = "missing"
        _snapshot_empty_warning = "밸류에이션/재무 스냅샷이 아직 적재되지 않아 해당 프리셋 후보를 표시할 수 없습니다."

    _snapshot_was_checked = _snapshot_check_result is not None
    if _snapshot_was_checked:
        # Snapshot check succeeded (latest partition found); use that result.
        # Even an empty list must NOT fall through to external screening —
        # historical qualifying rows from older partitions must stay hidden.
        snapshot_rows = _snapshot_check_result  # type: ignore[assignment]
        _snapshot_empty_warnings: list[str] = []
        if not snapshot_rows:
            _snapshot_empty_warnings = [_snapshot_empty_warning]
        raw = {
            "results": snapshot_rows,
            "warnings": _snapshot_empty_warnings,
            "timestamp": now().isoformat(),
            "cache_hit": True,
        }
    else:
        try:
            raw = await screening_service.list_screening(**filters)
        except Exception as exc:  # noqa: BLE001
            raw = {
                "results": [],
                "warnings": [_external_failure_warning(exc)],
                "timestamp": datetime.now(UTC).isoformat(),
                "cache_hit": False,
            }
    rows: list[dict[str, Any]] = list(raw.get("results") or raw.get("stocks") or [])
    upstream_warnings: list[str] = _safe_warnings(list(raw.get("warnings") or []))

    # ROB-170 follow-up: snapshot-first hydration runs at the view-model layer so
    # the session reaches _enrich_consecutive_up_days. Without this call the
    # screening service path never sees the session and _screener_snapshot_state
    # is never populated, leaving dataState pinned at "missing".
    if (
        session is not None
        and requested_market in {"kr", "us"}
        and preset_id == "consecutive_gainers"
        and rows
    ):
        from app.mcp_server.tooling.screening.enrichment import (
            _enrich_consecutive_up_days as _async_enrich,
        )

        await _async_enrich(rows, market=requested_market, session=session, now=now)

    # Aggregate snapshot dataState from enriched rows (set by _enrich_consecutive_up_days when session provided)
    from app.services.invest_screener_snapshots.freshness import aggregate_states

    if _snapshot_was_checked and not rows:
        # Latest snapshot partition was found but had no qualifying rows —
        # the data exists, but this preset has no current qualifiers.  Stock
        # snapshot semantics keep the historical stale warning; crypto snapshots
        # can still be fresh/partial 24/7 even when a preset returns zero rows.
        _aggregated_data_state = _snapshot_state_override or "stale"
    else:
        _row_states: list[str] = [
            str(r.get("_screener_snapshot_state") or "missing") for r in rows
        ]
        _aggregated_data_state = aggregate_states(_row_states)  # type: ignore[arg-type]

    if preset_id == "double_buy":
        from app.services.invest_screener_snapshots.freshness import today_trading_date

        _market_date = today_trading_date(requested_market, now=now())
        _double_buy_warnings, _double_buy_state_override = (
            _double_buy_dependency_warnings(
                snapshot_rows=rows,
                now_market_date=_market_date,
            )
        )
        for w in _double_buy_warnings:
            if w not in upstream_warnings:
                upstream_warnings.append(w)
        if _double_buy_state_override is not None and _aggregated_data_state == "fresh":
            _aggregated_data_state = _double_buy_state_override

    if requested_market == "us" and _aggregated_data_state in {"missing", "stale"}:
        if _US_SCREENER_DATA_NOT_READY_WARNING not in upstream_warnings:
            upstream_warnings.append(_US_SCREENER_DATA_NOT_READY_WARNING)

    # ROB-277: determine primary kind/source for freshness (computed before _build_freshness).
    primary_kind: Literal["screener_snapshot", "live", "fallback"]
    primary_snapshot_date: dt.date | None = None
    primary_computed_at: datetime | None = None
    primary_source: str | None = None
    if _snapshot_was_checked:
        primary_kind = "screener_snapshot"
        if preset_id == "investor_flow_momentum":
            primary_source = "investor_flow_snapshots"
        elif preset_id == "high_yield_value":
            primary_source = "market_valuation_snapshots"
        elif preset_id == "undervalued_breakout":
            primary_source = "market_valuation_snapshots"
        elif preset_id in FUNDAMENTALS_PRESET_SPECS:
            primary_source = "market_valuation_snapshots"
        elif requested_market == "crypto":
            primary_source = "invest_crypto_screener_snapshots"
        else:
            primary_source = "invest_screener_snapshots"
        # ROB-277 follow-up: prefer partition metadata from the loader dataclass
        # so freshness.primary has the correct date even when 0 rows qualify.
        if _snapshot_load_result is not None:
            primary_snapshot_date = _snapshot_load_result.partition_date
            primary_computed_at = _snapshot_load_result.partition_computed_at
        elif rows:
            # Fallback for double_buy / crypto paths that don't use _SnapshotLoadResult
            primary_snapshot_date = rows[0].get("snapshot_date")
            primary_computed_at = rows[0].get("computed_at")
    else:
        primary_kind = "live"
        primary_source = "screening_service"

    # Bulk-lookup Korean names for KR rows from kr_symbol_universe
    _kr_names: dict[str, str] = {}
    if session is not None and requested_market == "kr" and rows:
        import sqlalchemy as sa

        from app.models.kr_symbol_universe import KRSymbolUniverse

        kr_symbols = [
            _normalize_symbol(r, "kr")[0]
            for r in rows
            if _normalize_market(r.get("market") or requested_market) == "kr"
        ]
        if kr_symbols:
            _kr_result = await session.execute(
                sa.select(KRSymbolUniverse.symbol, KRSymbolUniverse.name).where(
                    KRSymbolUniverse.symbol.in_(kr_symbols)
                )
            )
            _kr_names = {row_t.symbol: row_t.name for row_t in _kr_result.all()}

    investor_flow_chips = await _hydrate_investor_flow_chips(
        db=session, market=requested_market, rows=rows
    )

    # ROB-277: build dependency specs from hydrated investor-flow chips (KR only).
    # Must run after _hydrate_investor_flow_chips so _investor_flow_snapshot_date,
    # _investor_flow_collected_at, and _investor_flow_data_state have been stashed
    # back on rows.
    dependency_specs: list[dict[str, Any]] = []
    if requested_market == "kr" and investor_flow_chips:
        from app.services.invest_screener_snapshots.freshness import (
            classify_investor_flow_partition,
            today_trading_date,
        )

        # ROB-277 follow-up: dependency = investor_flow_snapshots, distinct from primary.
        # ONLY use the dependency-specific stash keys, never the primary's snapshot_date.
        inv_meta: list[tuple[dt.date, datetime | None]] = []
        for r in rows:
            sd = r.get("_investor_flow_snapshot_date")
            if sd is not None:
                inv_meta.append((sd, r.get("_investor_flow_collected_at")))

        if inv_meta:
            worst_sd, worst_collected = min(inv_meta, key=lambda t: t[0])
            now_utc = now()
            today_kr = today_trading_date("kr", now=now_utc)
            dep_state = classify_investor_flow_partition(
                snapshot_date=worst_sd,
                collected_at=worst_collected,
                today_trading_date_value=today_kr,
                now=now_utc,
            )
            dependency_specs.append(
                {
                    "kind": "investor_flow",
                    "snapshot_date": worst_sd,
                    "collected_at": worst_collected,
                    "data_state": dep_state,
                    "source": "investor_flow_snapshots",
                }
            )

    if requested_market == "kr" and _fundamentals_screen_result is not None:
        dependency_specs.append(
            {
                "kind": "fundamentals",
                "snapshot_date": _fundamentals_screen_result.fundamentals_partition_date,
                "collected_at": _fundamentals_screen_result.fundamentals_collected_at,
                "data_state": _fundamentals_screen_result.fundamentals_state,
                "source": "financial_fundamentals_snapshots",
            }
        )

    freshness = _build_freshness(
        raw_timestamp=raw.get("timestamp"),
        cache_hit=bool(raw.get("cache_hit")),
        market=requested_market,
        now=now,
        dataState=_aggregated_data_state,
        primary_kind=primary_kind,
        primary_snapshot_date=primary_snapshot_date,
        primary_computed_at=primary_computed_at,
        primary_source=primary_source,
        dependency_specs=dependency_specs,
    )
    response_sources = _screen_response_sources(
        requested_market=requested_market,
        snapshot_was_checked=_snapshot_was_checked,
        raw=raw,
    )
    results: list[ScreenerResultRow] = []
    excluded_crypto_rows = 0
    for row in rows:
        market = _normalize_market(row.get("market") or requested_market)
        if market == "crypto":
            symbol, symbol_warnings = _normalize_crypto_symbol(row)
            if not symbol:
                excluded_crypto_rows += 1
                continue
        else:
            symbol, symbol_warnings = _normalize_symbol(row, market)
        market_cap_label, market_cap_warnings = _format_market_cap(row, market)
        change_pct_label, direction = _format_change_pct(row.get("change_rate"))
        _enrich_consecutive_up_days(preset_id, row)
        metric_label, metric_warnings = _metric_value_label(preset_id, row)
        relation = resolver.relation(market, symbol)
        is_watched = relation in ("watchlist", "both")
        row_warnings = symbol_warnings + market_cap_warnings + metric_warnings
        source_context = (
            _crypto_row_source_context(
                row, snapshot_was_checked=_snapshot_was_checked, raw=raw
            )
            if market == "crypto"
            else []
        )
        response_sources = _dedupe_sources([*response_sources, *source_context])
        results.append(
            ScreenerResultRow(
                rank=len(results) + 1,
                symbol=symbol,
                market=market,  # type: ignore[arg-type]
                name=_kr_names.get(symbol) or _clean_text(row.get("name")) or symbol,
                logoUrl=row.get("logo_url"),
                isWatched=is_watched,
                priceLabel=_format_crypto_price(
                    _coerce_float(
                        row.get("close") or row.get("price") or row.get("current_price")
                    ),
                    symbol,
                )
                if market == "crypto"
                else _format_price(
                    _coerce_float(
                        row.get("close") or row.get("price") or row.get("current_price")
                    ),
                    market,
                ),
                changePctLabel=change_pct_label,
                changeAmountLabel=_format_crypto_change_amount(
                    _coerce_float(row.get("change_amount")), symbol
                )
                if market == "crypto"
                else _format_change_amount(
                    _coerce_float(row.get("change_amount")), market
                ),
                changeDirection=direction,
                category=str(row.get("sector") or row.get("category") or "-"),
                marketCapLabel=market_cap_label,
                volumeLabel=_format_volume_label(row, market),
                analystLabel=str(row.get("analyst_label") or "-"),
                metricValueLabel=metric_label,
                investorFlowChip=investor_flow_chips.get(symbol),
                warnings=row_warnings,
                sourceContext=source_context,
                riskContext=_crypto_risk_context(row) if market == "crypto" else [],
                candidateContext=_crypto_candidate_context(row, preset_id)
                if market == "crypto"
                else None,
            )
        )

    if (
        excluded_crypto_rows
        and "비KRW 가상자산 행은 제외했습니다." not in upstream_warnings
    ):
        upstream_warnings.append("비KRW 가상자산 행은 제외했습니다.")

    return ScreenerResultsResponse(
        presetId=preset.id,
        title=preset.name,
        description=preset.description,
        filterChips=preset.filterChips,
        metricLabel=preset.metricLabel,
        results=results,
        warnings=upstream_warnings,
        freshness=freshness,
        sources=response_sources,
    )
