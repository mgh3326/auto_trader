"""ROB-123 — read-only InvestHomeService.

이 모듈은 KIS / Upbit / manual(toss) holdings 를 read-only 로 합성한다.
mutation 경로(submit/cancel/modify/place_order/watch/order-intent/scheduler/worker)
모듈 import / 호출 금지. DB write/backfill/update/delete 금지.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field

from app.schemas.invest_home import (
    Account,
    AccountKindLiteral,
    GroupedHolding,
    GroupedSourceBreakdown,
    Holding,
    HomeSummary,
    InvestHomeHiddenCounts,
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

        known_native_values = [
            h.valueNative for h in items if h.valueNative is not None and h.quantity > 0
        ]
        known_native_quantities = [
            h.quantity for h in items if h.valueNative is not None and h.quantity > 0
        ]
        inferred_native_unit: float | None = None
        if known_native_values and sum(known_native_quantities) > 0:
            inferred_native_unit = sum(known_native_values) / sum(
                known_native_quantities
            )

        native_parts: list[float] = []
        for h in items:
            if h.valueNative is not None:
                native_parts.append(h.valueNative)
            elif inferred_native_unit is not None:
                native_parts.append(h.quantity * inferred_native_unit)
        value_native: float | None = (
            sum(native_parts) if len(native_parts) == len(items) else None
        )

        fx_rate: float | None = None
        fx_candidates = [
            h.valueKrw / h.valueNative
            for h in items
            if h.currency == "USD"
            and h.valueKrw is not None
            and h.valueNative is not None
            and h.valueNative > 0
        ]
        if fx_candidates:
            fx_rate = sum(fx_candidates) / len(fx_candidates)

        krw_parts: list[float] = []
        for h in items:
            if h.valueKrw is not None:
                krw_parts.append(h.valueKrw)
            elif h.currency == "KRW" and inferred_native_unit is not None:
                krw_parts.append(h.quantity * inferred_native_unit)
            elif h.currency == "USD" and inferred_native_unit is not None and fx_rate:
                krw_parts.append(h.quantity * inferred_native_unit * fx_rate)
        value_krw: float | None = (
            sum(krw_parts) if len(krw_parts) == len(items) else None
        )

        pnl_vals = [h.pnlKrw for h in items]
        pnl_krw: float | None = (
            sum(v for v in pnl_vals if v is not None)
            if all(v is not None for v in pnl_vals)
            else None
        )
        if pnl_krw is None and cost_basis is not None and value_krw is not None:
            if first.currency == "KRW":
                pnl_krw = value_krw - cost_basis
            elif first.currency == "USD" and fx_rate:
                pnl_krw = value_krw - cost_basis * fx_rate

        pnl_rate: float | None = None
        if cost_basis is not None and cost_basis > 0 and value_native is not None:
            pnl_rate = (value_native - cost_basis) / cost_basis

        price_states = {h.priceState for h in items}
        if "live" in price_states:
            price_state = "live"
        elif "stale" in price_states:
            price_state = "stale"
        else:
            price_state = "missing"

        out.append(
            GroupedHolding(
                groupId=gid,
                symbol=_normalize_symbol(first.symbol),
                market=first.market,
                assetType=first.assetType,
                assetCategory=first.assetCategory,
                displayName=first.displayName,
                currency=first.currency,
                totalQuantity=total_qty,
                averageCost=avg_cost,
                costBasis=cost_basis,
                valueNative=value_native,
                valueKrw=value_krw,
                pnlKrw=pnl_krw,
                pnlRate=pnl_rate,
                priceState=price_state,
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


def _holding_cost_basis_krw(h: Holding) -> float | None:
    """Return cost basis converted to KRW when reliable conversion is available."""

    if h.costBasis is None:
        return None
    if h.currency == "KRW":
        return h.costBasis
    if h.currency == "USD":
        if h.valueKrw is not None and h.valueNative is not None and h.valueNative > 0:
            return h.costBasis * (h.valueKrw / h.valueNative)
        if h.valueKrw is not None and h.pnlKrw is not None:
            return h.valueKrw - h.pnlKrw
    return None


def build_manual_account_from_holdings(holdings: Iterable[Holding]) -> Account | None:
    """Build the synthetic Toss/manual account without poisoning home PnL.

    Only holdings with a reliable current KRW value are included in value/cost/PnL
    math. Unpriced manual holdings stay visible in the holdings list with warnings,
    but they must not fabricate losses by contributing cost basis without value.
    """

    toss_holdings = [h for h in holdings if h.source == "toss_manual"]
    if not toss_holdings:
        return None

    valued_holdings = [h for h in toss_holdings if h.valueKrw is not None]
    toss_value_krw = sum(h.valueKrw for h in valued_holdings if h.valueKrw is not None)

    converted_costs = [_holding_cost_basis_krw(h) for h in valued_holdings]
    toss_cost_basis_krw: float | None = None
    toss_pnl_krw: float | None = None
    toss_pnl_rate: float | None = None
    if valued_holdings and all(v is not None for v in converted_costs):
        toss_cost_basis_krw = sum(v for v in converted_costs if v is not None)
        toss_pnl_krw = toss_value_krw - toss_cost_basis_krw
        if toss_cost_basis_krw > 0:
            toss_pnl_rate = toss_pnl_krw / toss_cost_basis_krw

    return Account(
        accountId="toss_manual_account",
        displayName="Toss 수동",
        source="toss_manual",
        accountKind="manual",
        includedInHome=True,
        valueKrw=toss_value_krw,
        costBasisKrw=toss_cost_basis_krw,
        pnlKrw=toss_pnl_krw,
        pnlRate=toss_pnl_rate,
        cashBalances=Account.model_fields["cashBalances"].default_factory(),
        buyingPower=Account.model_fields["buyingPower"].default_factory(),
    )


@dataclass(frozen=True)
class _SourceFetchResult:
    accounts: list[Account]
    holdings: list[Holding]
    warning: InvestHomeWarning | None = None
    hidden_holdings: list[Holding] = field(default_factory=list)
    hidden_counts: InvestHomeHiddenCounts = field(
        default_factory=InvestHomeHiddenCounts
    )


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
        hidden_holdings: list[Holding] = []
        hidden_counts = InvestHomeHiddenCounts()

        for fetcher, src in (
            (self._kis.fetch, "kis"),
            (self._upbit.fetch, "upbit"),
            (self._manual.fetch, "toss_manual"),
        ):
            try:
                if src == "kis" or src == "upbit":
                    result: _SourceFetchResult = await fetcher(user_id=user_id)
                else:
                    result: _SourceFetchResult = await self._manual.fetch(
                        user_id=user_id
                    )

                accounts.extend(result.accounts)
                holdings.extend(result.holdings)
                hidden_holdings.extend(result.hidden_holdings)
                hidden_counts.upbitInactive += result.hidden_counts.upbitInactive
                hidden_counts.upbitDust += result.hidden_counts.upbitDust

                if result.warning is not None:
                    warnings.append(result.warning)

                # Synthetic Toss Manual Account
                if src == "toss_manual":
                    toss_account = build_manual_account_from_holdings(result.holdings)
                    if toss_account is not None:
                        accounts.append(toss_account)

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
            meta=InvestHomeResponseMeta(
                warnings=warnings,
                hiddenCounts=hidden_counts,
                hiddenHoldings=hidden_holdings,
            ),
        )
