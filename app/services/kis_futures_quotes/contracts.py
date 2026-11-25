"""Closed futures product set, front/next contract resolution and roll state (hk 6148).

Contract identity is ``(product, contract month)``; the DB symbol is
``<PRODUCT>.<YYMM>`` (``app.core.symbol.to_futures_contract_symbol``). KIS short
codes are never guessed: a contract exists only if a futures-master row matches the
closed product (product type, underlying, ``<name> YYYYMM`` pattern) and its short
code agrees with the month in the name (same check as the mock order path).

Read-only reuse: product constants and the last-trade-date rule come from
``kis_futures_mock`` (constants and session_calendar modules only — no place,
ledger or attribution module).

``[unverified]`` the regular KOSPI200 futures master row layout (product type
``1``, name ``F YYYYMM``, short code ``A01<y><mm>``) comes from the repo's existing
mock-rules fixture of the master, not from a live download.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from app.core.symbol import to_futures_contract_symbol
from app.services.kis_futures_mock import session_calendar
from app.services.kis_futures_mock.constants import MINI_KOSPI200

ROLL_FRONT = "front"
ROLL_NEXT = "next"
ROLL_EXPIRING = "expiring"
ROLL_EXPIRED = "expired"
ROLL_STATES = (ROLL_FRONT, ROLL_NEXT, ROLL_EXPIRING, ROLL_EXPIRED)


@dataclass(frozen=True, slots=True)
class QuoteProduct:
    code: str  # DB product code (symbol ``F200.2612``)
    master_product_type: str
    master_name_pattern: re.Pattern[str]  # groups: year, month
    master_underlying_name: str
    short_code_pattern: re.Pattern[str]  # ``A`` + product + year digit + month


KOSPI200_FUTURES = QuoteProduct(
    code="F200",
    master_product_type="1",
    master_name_pattern=re.compile(r"^F (\d{4})(\d{2})$"),
    master_underlying_name="KOSPI200",
    short_code_pattern=re.compile(r"^A01\d{3}$"),
)
MINI_KOSPI200_FUTURES = QuoteProduct(
    code=MINI_KOSPI200.code,
    master_product_type=MINI_KOSPI200.master_product_type,
    master_name_pattern=MINI_KOSPI200.master_name_pattern,
    master_underlying_name=MINI_KOSPI200.master_underlying_name,
    short_code_pattern=MINI_KOSPI200.short_code_pattern,
)
#: Closed set: KOSPI200 futures and mini KOSPI200 futures. No options, no stock futures.
CLOSED_QUOTE_PRODUCTS: tuple[QuoteProduct, ...] = (
    KOSPI200_FUTURES,
    MINI_KOSPI200_FUTURES,
)


@dataclass(frozen=True, slots=True)
class ResolvedContract:
    product: QuoteProduct
    short_code: str  # KIS short code, from the master row
    contract_month: str  # ``YYYY-MM``
    symbol: str  # DB symbol ``<PRODUCT>.<YYMM>``
    yymm: str
    last_trade_date: date
    rank: int  # 0 = front, 1 = next (among contracts not expired on the as-of date)
    listed_months: tuple[
        str, ...
    ]  # every master month of the product (roll_state input)


def last_trade_date_for(contract_month: str) -> date:
    """Last trade date of ``YYYY-MM`` — second Thursday, holiday-adjusted when known.

    Inside the mock calendar's known range (holidays tabulated) the holiday-adjusted
    date is used. Outside it there is no holiday table, so the plain second-Thursday
    rule applies **without** holiday adjustment: the date can be a day or more late
    in the rare month where the second Thursday is a holiday.
    """
    adjusted = session_calendar.last_trade_date(contract_month)
    if adjusted is not None:
        return adjusted
    year_text, _, month_text = contract_month.partition("-")
    return session_calendar.second_thursday(int(year_text), int(month_text))


def roll_state(contract_month: str, on_date: date, listed_months: list[str]) -> str:
    """``front`` | ``next`` | ``expiring`` | ``expired`` of a contract on ``on_date``.

    * ``expired``  — ``on_date`` is after the last trade date.
    * ``expiring`` — ``on_date`` is in the ISO week (Mon-Sun) that contains the last
      trade date (and not after it).
    * otherwise ``front``/``next`` by position among the listed contracts that have
      not expired on ``on_date``. A third or later contract raises ``ValueError``
      (outside the collected set).
    """
    last = last_trade_date_for(contract_month)
    if on_date > last:
        return ROLL_EXPIRED
    if on_date.isocalendar()[:2] == last.isocalendar()[:2]:
        return ROLL_EXPIRING
    earlier_live = sum(
        1
        for month in listed_months
        if month < contract_month and last_trade_date_for(month) >= on_date
    )
    if earlier_live == 0:
        return ROLL_FRONT
    if earlier_live == 1:
        return ROLL_NEXT
    raise ValueError(f"{contract_month} is beyond the next contract on {on_date}")


def _candidates(
    master_rows: list[dict[str, str]], product: QuoteProduct
) -> dict[str, str]:
    """``YYYY-MM`` -> short code for rows matching ``product``; ambiguous months drop."""
    found: dict[str, list[str]] = {}
    for row in master_rows:
        if (
            row.get("product_type") != product.master_product_type
            or row.get("underlying_name") != product.master_underlying_name
        ):
            continue
        match = product.master_name_pattern.fullmatch(row.get("name", ""))
        short = row.get("short_code", "")
        if match is None or product.short_code_pattern.fullmatch(short) is None:
            continue
        year, month = match.group(1), match.group(2)
        if short[3] != year[3] or short[4:6] != month:
            continue
        if not 1 <= int(month) <= 12:
            continue
        found.setdefault(f"{year}-{month}", []).append(short)
    return {month: codes[0] for month, codes in found.items() if len(set(codes)) == 1}


def listed_months_by_product(
    master_rows: list[dict[str, str]],
) -> dict[str, tuple[str, ...]]:
    """Product code -> every listed ``YYYY-MM`` of that closed product, ascending.

    The same listing ``resolve_front_next`` ranks, exposed for ``roll_state``: the
    ingest server recomputes a bar's roll state from the master because it has no
    contract list of its own. A product with no matching master row is absent.
    """
    listing: dict[str, tuple[str, ...]] = {}
    for product in CLOSED_QUOTE_PRODUCTS:
        months = tuple(sorted(_candidates(master_rows, product)))
        if months:
            listing[product.code] = months
    return listing


def resolve_front_next(
    master_rows: list[dict[str, str]],
    as_of: date,
    products: tuple[QuoteProduct, ...] = CLOSED_QUOTE_PRODUCTS,
) -> tuple[list[ResolvedContract], list[str]]:
    """Front and next contract of every product on ``as_of``.

    Returns ``(contracts, unresolved_product_codes)``. A product with no live
    contract in the master is unresolved, never guessed.
    """
    contracts: list[ResolvedContract] = []
    unresolved: list[str] = []
    for product in products:
        by_month = _candidates(master_rows, product)
        live = sorted(
            month for month in by_month if last_trade_date_for(month) >= as_of
        )
        if not live:
            unresolved.append(product.code)
            continue
        for rank, month in enumerate(live[:2]):
            symbol = to_futures_contract_symbol(product.code, month)
            contracts.append(
                ResolvedContract(
                    product=product,
                    short_code=by_month[month],
                    contract_month=month,
                    symbol=symbol,
                    yymm=symbol.rpartition(".")[2],
                    last_trade_date=last_trade_date_for(month),
                    rank=rank,
                    listed_months=tuple(sorted(by_month)),
                )
            )
    return contracts, unresolved
