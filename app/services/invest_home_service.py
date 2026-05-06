"""ROB-123 — read-only InvestHomeService.

이 모듈은 KIS / Upbit / manual(toss) holdings 를 read-only 로 합성한다.
mutation 경로(submit/cancel/modify/place_order/watch/order-intent/scheduler/worker)
모듈 import / 호출 금지. DB write/backfill/update/delete 금지.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass

from app.schemas.invest_home import (
    Account,
    AccountKindLiteral,
    GroupedHolding,
    GroupedSourceBreakdown,
    Holding,
    HomeSummary,
    InvestHomeResponse,
    InvestHomeResponseMeta,
    InvestHomeWarning,
)

logger = logging.getLogger(__name__)

HOME_INCLUDED_SOURCES: frozenset[str] = frozenset({"kis", "upbit", "toss_manual"})

_PAPER: frozenset[str] = frozenset(
    {"kis_mock", "kiwoom_mock", "alpaca_paper", "db_simulated"}
)
_MANUAL: frozenset[str] = frozenset({"toss_manual", "pension_manual", "isa_manual"})


def classify_account_kind(source: str) -> AccountKindLiteral:
    if source in _PAPER:
        return "paper"
    if source in _MANUAL:
        return "manual"
    return "live"  # kis, upbit


def _normalize_symbol(s: str) -> str:
    return s.strip().upper()


def _group_id(h: Holding) -> str:
    return f"{h.market}:{h.assetType}:{h.currency}:{_normalize_symbol(h.symbol)}"


def build_grouped_holdings(holdings: Iterable[Holding]) -> list[GroupedHolding]:
    buckets: dict[str, list[Holding]] = {}
    for h in holdings:
        buckets.setdefault(_group_id(h), []).append(h)

    out: list[GroupedHolding] = []
    for gid, items in buckets.items():
        first = items[0]
        total_qty = sum(h.quantity for h in items)
        cost_vals = [h.costBasis for h in items]
        avg_cost: float | None = None
        cost_basis: float | None = None
        if all(v is not None for v in cost_vals) and total_qty > 0:
            cost_basis = sum(v for v in cost_vals if v is not None)
            avg_cost = cost_basis / total_qty

        native_vals = [h.valueNative for h in items]
        value_native: float | None = (
            sum(v for v in native_vals if v is not None)
            if all(v is not None for v in native_vals)
            else None
        )
        krw_vals = [h.valueKrw for h in items]
        value_krw: float | None = (
            sum(v for v in krw_vals if v is not None)
            if all(v is not None for v in krw_vals)
            else None
        )
        pnl_krw: float | None = None
        pnl_rate: float | None = None
        if value_krw is not None and cost_basis is not None and cost_basis > 0:
            pnl_krw = value_krw - cost_basis
            pnl_rate = pnl_krw / cost_basis

        out.append(
            GroupedHolding(
                groupId=gid,
                symbol=_normalize_symbol(first.symbol),
                market=first.market,
                assetType=first.assetType,
                displayName=first.displayName,
                currency=first.currency,
                totalQuantity=total_qty,
                averageCost=avg_cost,
                costBasis=cost_basis,
                valueNative=value_native,
                valueKrw=value_krw,
                pnlKrw=pnl_krw,
                pnlRate=pnl_rate,
                includedSources=sorted({h.source for h in items}),
                sourceBreakdown=[
                    GroupedSourceBreakdown(
                        holdingId=h.holdingId,
                        accountId=h.accountId,
                        source=h.source,
                        quantity=h.quantity,
                        averageCost=h.averageCost,
                        costBasis=h.costBasis,
                        valueNative=h.valueNative,
                        valueKrw=h.valueKrw,
                        pnlKrw=h.pnlKrw,
                        pnlRate=h.pnlRate,
                    )
                    for h in items
                ],
            )
        )
    return out


def build_home_summary(accounts: Iterable[Account]) -> HomeSummary:
    included = [a for a in accounts if a.includedInHome]
    excluded = [a for a in accounts if not a.includedInHome]
    total = sum(a.valueKrw for a in included)
    cost_vals = [a.costBasisKrw for a in included]
    cost_basis: float | None = (
        sum(v for v in cost_vals if v is not None)
        if cost_vals and all(v is not None for v in cost_vals)
        else None
    )
    pnl_krw: float | None = None
    pnl_rate: float | None = None
    if cost_basis is not None and cost_basis > 0:
        pnl_krw = total - cost_basis
        pnl_rate = pnl_krw / cost_basis
    return HomeSummary(
        includedSources=sorted({a.source for a in included}),
        excludedSources=sorted({a.source for a in excluded}),
        totalValueKrw=total,
        costBasisKrw=cost_basis,
        pnlKrw=pnl_krw,
        pnlRate=pnl_rate,
    )


@dataclass(frozen=True)
class _SourceFetchResult:
    accounts: list[Account]
    holdings: list[Holding]
    warning: InvestHomeWarning | None = None


class InvestHomeService:
    """Read-only 합성 서비스. mutation 경로 호출 금지."""

    def __init__(self, *, kis_reader, upbit_reader, manual_reader) -> None:
        self._kis = kis_reader
        self._upbit = upbit_reader
        self._manual = manual_reader

    async def get_home(self, *, user_id: int) -> InvestHomeResponse:
        warnings: list[InvestHomeWarning] = []
        accounts: list[Account] = []
        holdings: list[Holding] = []
        for fetcher, src in (
            (self._kis.fetch, "kis"),
            (self._upbit.fetch, "upbit"),
            (self._manual.fetch, "toss_manual"),
        ):
            try:
                result: _SourceFetchResult = await fetcher(user_id=user_id)
                accounts.extend(result.accounts)
                holdings.extend(result.holdings)
                if result.warning is not None:
                    warnings.append(result.warning)
            except Exception as exc:  # 부분 실패 — 전체 API 는 살림
                logger.warning(
                    "[invest_home] %s fetch failed: %s", src, exc, exc_info=True
                )
                warnings.append(
                    InvestHomeWarning(
                        source=src, message=str(exc) or type(exc).__name__
                    )
                )
        return InvestHomeResponse(
            homeSummary=build_home_summary(accounts),
            accounts=accounts,
            holdings=holdings,
            groupedHoldings=build_grouped_holdings(holdings),
            meta=InvestHomeResponseMeta(warnings=warnings),
        )
