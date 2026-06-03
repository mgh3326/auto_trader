from __future__ import annotations

import asyncio
import datetime as dt
import logging
import threading
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import pandas as pd

from app.core.json_safe import sanitize_non_finite
from app.services.financial_fundamentals_snapshots.freshness import row_data_state
from app.services.financial_fundamentals_snapshots.repository import (
    FinancialFundamentalsUpsert,
)
from app.services.market_quote_snapshots.builder import redact_sensitive_payload

logger = logging.getLogger(__name__)

# XBRL account_id codes (preferred) with Korean account_nm contains-fallbacks.
_REVENUE_IDS = ("ifrs-full_Revenue", "ifrs-full_RevenueFromContractsWithCustomers")
_GROSS_PROFIT_IDS = ("ifrs-full_GrossProfit",)
_COST_OF_SALES_IDS = ("ifrs-full_CostOfSales",)
_NET_INCOME_IDS = ("ifrs-full_ProfitLoss",)

_REVENUE_NAMES = ("매출액", "수익(매출액)", "영업수익")
_GROSS_PROFIT_NAMES = ("매출총이익",)
_COST_OF_SALES_NAMES = ("매출원가",)
_NET_INCOME_NAMES = ("당기순이익", "당기순이익(손실)")


def _dart_amount_to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    text = str(value).strip().replace(",", "")
    if text in {"", "-", "nan"}:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _normalize_label(value: Any) -> str:
    if value is None:
        return ""
    return "".join(str(value).split()).replace("(", "").replace(")", "")


def _pick_amount(
    df: pd.DataFrame, *, account_ids: tuple[str, ...], account_names: tuple[str, ...]
) -> Decimal | None:
    if df.empty:
        return None
    for _, row in df.iterrows():
        if str(row.get("account_id", "")).strip() in account_ids:
            return _dart_amount_to_decimal(row.get("thstrm_amount"))
    # Fallback: normalized-contains match on the Korean account name.
    targets = {_normalize_label(name) for name in account_names}
    for _, row in df.iterrows():
        label = _normalize_label(row.get("account_nm"))
        if any(target in label for target in targets):
            return _dart_amount_to_decimal(row.get("thstrm_amount"))
    return None


def parse_income_statement_frame(df: pd.DataFrame) -> dict[str, Decimal | None]:
    """Extract revenue / net_income / gross_profit / cost_of_sales from a finstate_all frame."""
    return {
        "revenue": _pick_amount(
            df, account_ids=_REVENUE_IDS, account_names=_REVENUE_NAMES
        ),
        "gross_profit": _pick_amount(
            df, account_ids=_GROSS_PROFIT_IDS, account_names=_GROSS_PROFIT_NAMES
        ),
        "cost_of_sales": _pick_amount(
            df, account_ids=_COST_OF_SALES_IDS, account_names=_COST_OF_SALES_NAMES
        ),
        "net_income": _pick_amount(
            df, account_ids=_NET_INCOME_IDS, account_names=_NET_INCOME_NAMES
        ),
    }


def _pick_dividend_row(df: pd.DataFrame, *, contains: str) -> Decimal | None:
    target = _normalize_label(contains)
    for _, row in df.iterrows():
        if target in _normalize_label(row.get("se")):
            return _dart_amount_to_decimal(row.get("thstrm"))
    return None


def parse_dividend_frame(df: pd.DataFrame) -> dict[str, Decimal | None]:
    """Extract payout_ratio (현금배당성향%) and dividend_per_share (주당 현금배당금) from alotMatter."""
    if df.empty:
        return {"payout_ratio": None, "dividend_per_share": None}
    return {
        "payout_ratio": _pick_dividend_row(df, contains="현금배당성향"),
        "dividend_per_share": _pick_dividend_row(df, contains="주당현금배당금"),
    }


def _parse_dart_date(value: Any) -> dt.date | None:
    text = str(value).strip().replace("-", "")
    if len(text) < 8 or not text[:8].isdigit():
        return None
    return dt.date(int(text[:4]), int(text[4:6]), int(text[6:8]))


def parse_filing_dates_frame(df: pd.DataFrame) -> dict[str, dt.date]:
    """Map rcept_no -> filing date (rcept_dt) from a disclosure-list frame."""
    mapping: dict[str, dt.date] = {}
    if df.empty:
        return mapping
    for _, row in df.iterrows():
        rcept_no = str(row.get("rcept_no", "")).strip()
        filing_date = _parse_dart_date(row.get("rcept_dt"))
        if rcept_no and filing_date is not None:
            mapping[rcept_no] = filing_date
    return mapping


def single_quarter_discrete(
    *, cumulative: Decimal | None, prior_cumulative: Decimal | None
) -> Decimal | None:
    """Standalone single-quarter value from KR YTD-cumulative interim amounts.

    Q1 cumulative == standalone (prior_cumulative is None). Later quarters subtract the
    prior cumulative. A missing current cumulative cannot be differenced.
    """
    if cumulative is None:
        return None
    if prior_cumulative is None:
        return cumulative
    return cumulative - prior_cumulative


class DartDailyRequestBudgetExceeded(RuntimeError):
    def __init__(
        self,
        message: str,
        payloads: tuple[FinancialFundamentalsUpsert, ...] = (),
        warnings: tuple[str, ...] = (),
    ):
        super().__init__(message)
        self.payloads = payloads
        self.warnings = warnings


_request_count = 0
# Guards the module-global counter: fetch_sync runs under asyncio.to_thread and
# symbols fan out via gather + Semaphore, so concurrent threads mutate the count.
_request_lock = threading.Lock()


def reset_request_count() -> None:
    global _request_count
    with _request_lock:
        _request_count = 0


def increment_and_check_budget(delta: int = 1) -> None:
    global _request_count
    from app.core.config import settings

    budget = settings.opendart_daily_request_budget
    with _request_lock:
        if budget > 0 and _request_count + delta > budget:
            raise DartDailyRequestBudgetExceeded(
                f"DART daily request budget of {budget} exceeded (attempted to make {_request_count + delta} requests)"
            )
        _request_count += delta


@dataclass(frozen=True)
class RawAnnualFiling:
    bsns_year: int
    rcept_no: str
    income_statement: pd.DataFrame
    dividend: pd.DataFrame | None = None


@dataclass(frozen=True)
class RawQuarterlyFiling:
    bsns_year: int
    quarter: int  # 1..4
    rcept_no: str
    reprt_code: str
    income_statement: pd.DataFrame  # cumulative YTD amounts
    prior_income_statement: pd.DataFrame | None = (
        None  # prior cumulative (for differencing)
    )


@dataclass(frozen=True)
class RawFundamentalsBundle:
    symbol: str
    currency: str | None = None
    annual: tuple[RawAnnualFiling, ...] = ()
    quarterly: tuple[RawQuarterlyFiling, ...] = ()
    filing_dates: dict[str, dt.date] | None = None


@dataclass(frozen=True)
class FinancialFundamentalsBuildResult:
    payloads: tuple[FinancialFundamentalsUpsert, ...]
    warnings: tuple[str, ...] = ()


FundamentalsFetcher = Callable[..., Awaitable[RawFundamentalsBundle]]

_REPRT_CODE_BY_QUARTER = {1: "11013", 2: "11012", 3: "11014", 4: "11011"}


def _payload_from_annual(
    *,
    market: str,
    symbol: str,
    filing: RawAnnualFiling,
    currency: str | None,
    filing_date: dt.date | None,
    collected_at: dt.datetime,
) -> FinancialFundamentalsUpsert:
    income = parse_income_statement_frame(filing.income_statement)
    dividend = (
        parse_dividend_frame(filing.dividend)
        if filing.dividend is not None
        else {"payout_ratio": None, "dividend_per_share": None}
    )
    raw = {
        "income_statement": filing.income_statement.to_dict(orient="records"),
        "dividend": (
            filing.dividend.to_dict(orient="records")
            if filing.dividend is not None
            else None
        ),
        "rcept_no": filing.rcept_no,
        "bsns_year": filing.bsns_year,
    }
    return FinancialFundamentalsUpsert(
        market=market,
        symbol=symbol,
        fiscal_period=f"{filing.bsns_year}A",
        period_type="annual",
        period_end_date=dt.date(filing.bsns_year, 12, 31),
        filing_date=filing_date,
        effective_at=filing_date,
        source="dart",
        source_collected_at=collected_at,
        currency=currency,
        revenue=income["revenue"],
        net_income=income["net_income"],
        gross_profit=income["gross_profit"],
        cost_of_sales=income["cost_of_sales"],
        payout_ratio=dividend["payout_ratio"],
        dividend_per_share=dividend["dividend_per_share"],
        discrete_revenue=income["revenue"],  # annual: discrete == reported
        discrete_net_income=income["net_income"],
        data_state=row_data_state(filing_date=filing_date),
        raw_payload=redact_sensitive_payload(sanitize_non_finite(raw)),
    )


def _payload_from_quarterly(
    *,
    market: str,
    symbol: str,
    filing: RawQuarterlyFiling,
    currency: str | None,
    filing_date: dt.date | None,
    collected_at: dt.datetime,
) -> FinancialFundamentalsUpsert:
    income = parse_income_statement_frame(filing.income_statement)
    prior = (
        parse_income_statement_frame(filing.prior_income_statement)
        if filing.prior_income_statement is not None
        else {"revenue": None, "net_income": None}
    )
    discrete_revenue = single_quarter_discrete(
        cumulative=income["revenue"], prior_cumulative=prior["revenue"]
    )
    discrete_net_income = single_quarter_discrete(
        cumulative=income["net_income"], prior_cumulative=prior["net_income"]
    )
    raw = {
        "income_statement": filing.income_statement.to_dict(orient="records"),
        "rcept_no": filing.rcept_no,
        "bsns_year": filing.bsns_year,
        "quarter": filing.quarter,
        "reprt_code": filing.reprt_code,
    }
    return FinancialFundamentalsUpsert(
        market=market,
        symbol=symbol,
        fiscal_period=f"{filing.bsns_year}Q{filing.quarter}",
        period_type="quarterly",
        period_end_date=_quarter_end_date(filing.bsns_year, filing.quarter),
        filing_date=filing_date,
        effective_at=filing_date,
        source="dart",
        source_collected_at=collected_at,
        currency=currency,
        revenue=income["revenue"],
        net_income=income["net_income"],
        gross_profit=income["gross_profit"],
        cost_of_sales=income["cost_of_sales"],
        discrete_revenue=discrete_revenue,
        discrete_net_income=discrete_net_income,
        data_state=row_data_state(filing_date=filing_date),
        raw_payload=redact_sensitive_payload(sanitize_non_finite(raw)),
    )


def _quarter_end_date(year: int, quarter: int) -> dt.date:
    return {
        1: dt.date(year, 3, 31),
        2: dt.date(year, 6, 30),
        3: dt.date(year, 9, 30),
        4: dt.date(year, 12, 31),
    }[quarter]


async def build_financial_fundamentals_for_symbols(
    *,
    market: str,
    symbols: Iterable[str],
    collected_at: dt.datetime,
    fetcher: FundamentalsFetcher,
    include_quarterly: bool = False,
    concurrency: int = 4,
) -> FinancialFundamentalsBuildResult:
    market_norm = market.strip().lower()
    if market_norm != "kr":
        raise ValueError(f"PR1 supports market='kr' only, got: {market}")
    sem = asyncio.Semaphore(max(1, concurrency))
    symbols_list = [s.strip().upper() for s in symbols if s.strip()]
    collected: list[FinancialFundamentalsUpsert] = []
    warnings: list[str] = []

    async def _one(symbol: str) -> None:
        async with sem:
            try:
                bundle = await fetcher(symbol, include_quarterly=include_quarterly)
            except DartDailyRequestBudgetExceeded:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("fundamentals fetch failed symbol=%s: %s", symbol, exc)
                warnings.append(f"{symbol}: fetch failed ({exc})")
                return
            filing_dates = bundle.filing_dates or {}
            for filing in bundle.annual:
                collected.append(
                    _payload_from_annual(
                        market=market_norm,
                        symbol=symbol,
                        filing=filing,
                        currency=bundle.currency,
                        filing_date=filing_dates.get(filing.rcept_no),
                        collected_at=collected_at,
                    )
                )
            for q_filing in bundle.quarterly:
                collected.append(
                    _payload_from_quarterly(
                        market=market_norm,
                        symbol=symbol,
                        filing=q_filing,
                        currency=bundle.currency,
                        filing_date=filing_dates.get(q_filing.rcept_no),
                        collected_at=collected_at,
                    )
                )

    try:
        await asyncio.gather(*(_one(symbol) for symbol in symbols_list))
    except DartDailyRequestBudgetExceeded as exc:
        raise DartDailyRequestBudgetExceeded(
            str(exc),
            payloads=tuple(collected),
            warnings=tuple(warnings) + (str(exc),),
        ) from exc
    return FinancialFundamentalsBuildResult(
        payloads=tuple(collected), warnings=tuple(warnings)
    )


async def default_dart_fetcher(
    symbol: str, *, include_quarterly: bool, years_back: int = 5
) -> RawFundamentalsBundle:
    """Live DART fetcher: activates the dormant finstate_all + report('배당') methods.

    fs_div='CFS' (consolidated) first, falling back to 'OFS' (separate) when CFS is empty.
    filing dates resolved by joining each rcept_no to the disclosure-list endpoint.
    """
    from app.core.config import settings
    from app.services.disclosures.dart import _get_client

    if not settings.opendart_api_key:
        raise RuntimeError("OPENDART_API_KEY not set")
    client = await _get_client()
    if client is None:
        raise RuntimeError("DART functionality not available")

    today = dt.date.today()
    years = list(range(today.year - 1, today.year - 1 - years_back, -1))

    def fetch_sync() -> RawFundamentalsBundle:
        class BudgetedClient:
            def __init__(self, original_client):
                self._client = original_client

            def finstate_all(self, *args, **kwargs):
                increment_and_check_budget()
                return self._client.finstate_all(*args, **kwargs)

            def report(self, *args, **kwargs):
                increment_and_check_budget()
                return self._client.report(*args, **kwargs)

            def list(self, *args, **kwargs):
                increment_and_check_budget()
                return self._client.list(*args, **kwargs)

        b_client = BudgetedClient(client)
        client_to_use = b_client

        annual: list[RawAnnualFiling] = []
        for year in years:
            stmt = client_to_use.finstate_all(symbol, year, "11011", fs_div="CFS")
            if stmt is None or stmt.empty:
                stmt = client_to_use.finstate_all(symbol, year, "11011", fs_div="OFS")
            if stmt is None or stmt.empty:
                continue
            try:
                dividend = client_to_use.report(symbol, "배당", year, "11011")
            except Exception:  # noqa: BLE001
                dividend = None
            rcept_no = ""
            if "rcept_no" in stmt.columns and not stmt.empty:
                rcept_no = str(stmt.iloc[0].get("rcept_no", "")).strip()
            annual.append(
                RawAnnualFiling(
                    bsns_year=year,
                    rcept_no=rcept_no,
                    income_statement=stmt,
                    dividend=dividend,
                )
            )

        quarterly: list[RawQuarterlyFiling] = []
        if include_quarterly:
            annual_by_year = {a.bsns_year: a for a in annual}
            for year in years:
                stmts_by_q: dict[int, tuple[str, pd.DataFrame]] = {}
                for q in (1, 2, 3, 4):
                    reprt_code = _REPRT_CODE_BY_QUARTER[q]
                    if q == 4:
                        if year in annual_by_year:
                            ann_filing = annual_by_year[year]
                            stmts_by_q[4] = (
                                ann_filing.rcept_no,
                                ann_filing.income_statement,
                            )
                    else:
                        q_stmt = client_to_use.finstate_all(
                            symbol, year, reprt_code, fs_div="CFS"
                        )
                        if q_stmt is None or q_stmt.empty:
                            q_stmt = client_to_use.finstate_all(
                                symbol, year, reprt_code, fs_div="OFS"
                            )
                        if q_stmt is not None and not q_stmt.empty:
                            r_no = ""
                            if "rcept_no" in q_stmt.columns:
                                r_no = str(q_stmt.iloc[0].get("rcept_no", "")).strip()
                            stmts_by_q[q] = (r_no, q_stmt)

                for q in (1, 2, 3, 4):
                    if q not in stmts_by_q:
                        continue
                    rcept_no, stmt = stmts_by_q[q]
                    prior_stmt = None
                    if q > 1:
                        # A KR interim/annual statement is YTD-cumulative, so the
                        # standalone quarter needs the immediately-prior cumulative
                        # to difference against. If that prior is missing we cannot
                        # produce a correct discrete — skip this quarter rather than
                        # emit the YTD cumulative mislabeled as the standalone value
                        # (single_quarter_discrete returns the cumulative verbatim
                        # when prior is None, which is only correct for Q1).
                        if (q - 1) not in stmts_by_q:
                            continue
                        _, prior_stmt = stmts_by_q[q - 1]

                    quarterly.append(
                        RawQuarterlyFiling(
                            bsns_year=year,
                            quarter=q,
                            rcept_no=rcept_no,
                            reprt_code=_REPRT_CODE_BY_QUARTER[q],
                            income_statement=stmt,
                            prior_income_statement=prior_stmt,
                        )
                    )

        # Resolve filing dates via the disclosure-list endpoint (carries rcept_dt).
        listing = client_to_use.list(
            corp=symbol,
            start=(today - dt.timedelta(days=365 * (years_back + 1))).isoformat(),
            end=today.isoformat(),
            kind="A",  # 정기보고서
            final=True,
        )
        filing_dates = parse_filing_dates_frame(
            listing if listing is not None else pd.DataFrame()
        )
        currency = None
        if annual and "currency" in annual[0].income_statement.columns:
            vals = annual[0].income_statement["currency"].dropna().unique().tolist()
            currency = str(vals[0]) if vals else None
        return RawFundamentalsBundle(
            symbol=symbol,
            currency=currency,
            annual=tuple(annual),
            quarterly=tuple(quarterly),
            filing_dates=filing_dates,
        )

    return await asyncio.to_thread(fetch_sync)
