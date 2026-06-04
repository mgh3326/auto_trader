from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from app.services.invest_kr_fundamentals_snapshots.repository import (
    InvestKrFundamentalsSnapshotsRepository,
    KrFundamentalsSnapshotUpsert,
)
from app.services.invest_screener_snapshots.freshness import today_trading_date
from app.services.snapshot_commit_guard import (
    PartialCommitBlocked,
    assert_min_coverage,
)

#: ROB-429 A2 — production commit coverage floor for KR fundamentals snapshots.
#: A commit below ceil(active_universe * floor) is blocked unless --allow-partial.
_KR_FUNDAMENTALS_MIN_COMMIT_COVERAGE_RATIO = 0.80

_RAW_PAYLOAD_KEYS = {
    "symbol",
    "name",
    "description",
    "active_symbol",
    "sector",
    "industry",
    "source",
}


@dataclass(frozen=True)
class KrFundamentalsProviderRow:
    symbol: str
    name: str | None = None
    price: Decimal | None = None
    change_rate: Decimal | None = None
    volume: Decimal | None = None
    market_cap: Decimal | None = None
    per: Decimal | None = None
    pbr: Decimal | None = None
    dividend_yield: Decimal | None = None
    roe_ttm: Decimal | None = None
    payout_ratio_ttm: Decimal | None = None
    gross_margin_ttm: Decimal | None = None
    revenue_yoy: Decimal | None = None
    eps_yoy: Decimal | None = None
    eps_qoq: Decimal | None = None
    net_income_yoy: Decimal | None = None
    net_income_cagr_5y: Decimal | None = None
    continuous_dividend_payout: Decimal | None = None
    continuous_dividend_growth: Decimal | None = None
    week_high_52: Decimal | None = None
    rsi14: Decimal | None = None
    sector: str | None = None
    industry: str | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)


class KrFundamentalsSnapshotProvider(Protocol):
    async def fetch_rows(
        self, *, limit: int | None = None
    ) -> list[KrFundamentalsProviderRow]: ...


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if result.is_nan() or result.is_infinite():
        return None
    return result


def _extract_kr_symbol(raw: Any) -> str:
    """Strip a ``KRX:`` exchange prefix and uppercase the bare code."""
    text = str(raw or "").strip().upper()
    if not text:
        return ""
    if ":" in text:
        text = text.split(":", maxsplit=1)[-1].strip()
    return text


def _str_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def build_kr_fundamentals_snapshot_payloads(
    rows: list[KrFundamentalsProviderRow], *, snapshot_date: dt.date
) -> list[KrFundamentalsSnapshotUpsert]:
    payloads: list[KrFundamentalsSnapshotUpsert] = []
    for row in rows:
        symbol = _extract_kr_symbol(row.symbol)
        if not symbol or row.price is None:
            continue
        payloads.append(
            KrFundamentalsSnapshotUpsert(
                symbol=symbol,
                snapshot_date=snapshot_date,
                name=row.name,
                price=row.price,
                change_rate=row.change_rate,
                volume=row.volume,
                market_cap=row.market_cap,
                per=row.per,
                pbr=row.pbr,
                dividend_yield=row.dividend_yield,
                roe_ttm=row.roe_ttm,
                payout_ratio_ttm=row.payout_ratio_ttm,
                gross_margin_ttm=row.gross_margin_ttm,
                revenue_yoy=row.revenue_yoy,
                eps_yoy=row.eps_yoy,
                eps_qoq=row.eps_qoq,
                net_income_yoy=row.net_income_yoy,
                net_income_cagr_5y=row.net_income_cagr_5y,
                continuous_dividend_payout=row.continuous_dividend_payout,
                continuous_dividend_growth=row.continuous_dividend_growth,
                week_high_52=row.week_high_52,
                rsi14=row.rsi14,
                sector=row.sector,
                industry=row.industry,
                raw_payload=row.raw_payload,
                source="tvscreener_kr",
            )
        )
    return payloads


async def build_kr_fundamentals_snapshots(
    *,
    provider: KrFundamentalsSnapshotProvider,
    repository: InvestKrFundamentalsSnapshotsRepository,
    snapshot_date: dt.date | None = None,
    commit: bool = False,
    limit: int | None = None,
    universe_count: int = 0,
    allow_partial: bool = False,
) -> dict[str, Any]:
    date_value = snapshot_date or today_trading_date("kr")
    provider_rows = await provider.fetch_rows(limit=limit)
    payloads = build_kr_fundamentals_snapshot_payloads(
        provider_rows, snapshot_date=date_value
    )
    would_upsert = len(payloads)
    coverage_ratio = (
        round(would_upsert / universe_count, 4) if universe_count > 0 else 0.0
    )

    # ROB-429 A2: coverage guard (ROB-426 snapshot_commit_guard). A build below
    # ceil(active_universe * 0.80) is NOT commit-allowed unless --allow-partial.
    # We evaluate the gate for both dry-run and commit so the result always
    # carries an honest commit_allowed/block_reason; the gate only blocks the
    # actual upsert when committing. --allow-partial bypasses the gate entirely.
    # universe_count <= 0 fail-opens (no denominator → cannot judge coverage).
    commit_allowed = True
    block_reason: str | None = None
    if not allow_partial:
        try:
            assert_min_coverage(
                count=would_upsert,
                universe_count=universe_count,
                market="kr",
                metric="kr_fundamentals",
                min_ratio=_KR_FUNDAMENTALS_MIN_COMMIT_COVERAGE_RATIO,
            )
        except PartialCommitBlocked as exc:
            commit_allowed = False
            block_reason = str(exc)

    should_commit = commit and commit_allowed
    upserted = 0
    if should_commit:
        for payload in payloads:
            await repository.upsert(payload)
            upserted += 1
    return {
        "snapshot_date": date_value.isoformat(),
        "fetched": len(provider_rows),
        "would_upsert": would_upsert,
        "upserted": upserted,
        "committed": should_commit,
        "active_universe_count": universe_count,
        "coverage_ratio": coverage_ratio,
        "commit_allowed": commit_allowed,
        "block_reason": block_reason,
        "samples": [payload.model_dump(mode="json") for payload in payloads[:5]],
    }


def provider_row_from_mapping(row: dict[str, Any]) -> KrFundamentalsProviderRow | None:
    """Map a normalised snake_case tvscreener row into a provider row.

    Returns ``None`` when the symbol cannot be resolved to a KR code or no
    price is present (defensive: ETF-ish / non-``KRX:`` rows are dropped).
    """
    symbol = _extract_kr_symbol(row.get("symbol") or row.get("active_symbol"))
    # tvscreener emits ``KRX:NNNNNN``-shaped symbols for KOREA equities; the
    # ``name`` column is frequently the ticker itself, so prefer ``description``
    # when present for a human-readable display name.
    raw_symbol = str(row.get("symbol") or row.get("active_symbol") or "").strip()
    if raw_symbol and ":" in raw_symbol and not raw_symbol.upper().startswith("KRX:"):
        return None
    price = _decimal_or_none(row.get("price") or row.get("close"))
    if not symbol or price is None:
        return None
    name = _str_or_none(row.get("description")) or _str_or_none(row.get("name"))
    return KrFundamentalsProviderRow(
        symbol=symbol,
        name=name,
        price=price,
        change_rate=_decimal_or_none(row.get("change_percent") or row.get("change")),
        volume=_decimal_or_none(row.get("volume")),
        market_cap=_decimal_or_none(
            row.get("market_capitalization") or row.get("market_cap_basic")
        ),
        per=_decimal_or_none(
            row.get("price_to_earnings_ratio_ttm") or row.get("price_to_earnings_ttm")
        ),
        pbr=_decimal_or_none(
            row.get("price_to_book_fq")
            or row.get("price_to_book_mrq")
            or row.get("price_book_current")
        ),
        dividend_yield=_decimal_or_none(
            row.get("dividend_yield_forward")
            or row.get("dividends_yield_current")
            or row.get("dividend_yield_current")
        ),
        roe_ttm=_decimal_or_none(
            row.get("return_on_equity_ttm") or row.get("return_on_equity_fy")
        ),
        payout_ratio_ttm=_decimal_or_none(
            row.get("dividend_payout_ratio_ttm")
            or row.get("dividend_payout_ratio_percent_ttm")
            or row.get("dividend_payout_ratio_fy")
        ),
        gross_margin_ttm=_decimal_or_none(
            row.get("gross_margin_ttm") or row.get("gross_margin_percent_ttm")
        ),
        revenue_yoy=_decimal_or_none(row.get("revenue_annual_yoy_growth")),
        eps_yoy=_decimal_or_none(row.get("eps_diluted_annual_yoy_growth")),
        eps_qoq=_decimal_or_none(row.get("eps_diluted_quarterly_qoq_growth")),
        net_income_yoy=_decimal_or_none(row.get("net_income_annual_yoy_growth")),
        net_income_cagr_5y=_decimal_or_none(row.get("net_income_cagr_5y")),
        continuous_dividend_payout=_decimal_or_none(
            row.get("continuous_dividend_payout")
        ),
        continuous_dividend_growth=_decimal_or_none(
            row.get("continuous_dividend_growth")
        ),
        week_high_52=_decimal_or_none(row.get("52_week_high")),
        rsi14=_decimal_or_none(row.get("relative_strength_index_14")),
        sector=_str_or_none(row.get("sector")),
        industry=_str_or_none(row.get("industry")),
        raw_payload={k: v for k, v in row.items() if k in _RAW_PAYLOAD_KEYS},
    )
