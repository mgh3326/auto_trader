"""Aggregate the /invest 매수 계획 board (§144차).

Read-only by construction: this service composes four existing read models
(``InvestHomeService`` holdings + cash, ``WatchPanelService`` alerts,
``CurrentOrdersService`` resting orders, ``config/trading_policy.yaml``) plus
two public market-data metrics. It never imports an order, proposal, watch, or
broker mutation path.

The board answers one operator question — *"트리거가 걸리면 돈이 얼마나
필요하고, 지금 계좌에 있는가"* — so cash can be moved in advance. It does not
reproduce a session's verdict; see ``computation.APPROXIMATION_NOTICE``.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from app.schemas.invest_buy_plan import (
    ActiveBuyWatchRow,
    ApprovalCondition,
    ApprovalContext,
    AveragingSampleRow,
    AveragingTriggerRow,
    BuyPlanCurrency,
    BuyPlanFunding,
    BuyPlanMarket,
    BuyPlanResponse,
    CashAccountRow,
    DiscoveryGateCondition,
    DiscoveryGateRow,
    FundingBroker,
    PolicyStamp,
    ScopeReconciliation,
    SourceState,
    SupportNetPlacement,
    SupportNetRow,
    SupportNetTier,
    UnattributedRequirement,
    ValueSource,
)
from app.schemas.invest_home import Account, GroupedHolding, InvestHomeResponse
from app.schemas.invest_watches import WatchAlertRow, WatchesResponse
from app.schemas.open_orders import OpenOrdersResponse
from app.services.brokers.kis.overseas_cash import (
    KIS_OVERSEAS_USD_BALANCE_ORDERABLE_MISMATCH,
)
from app.services.invest_view_model.buy_plan.computation import (
    APPROXIMATION_NOTICE,
    approval_lane_for,
    averaging_turn_point,
)
from app.services.invest_view_model.buy_plan.gate_inputs import (
    GATE_CACHE_TTL_SECONDS,
    GateMetricReading,
    read_alt_breadth_24h,
    read_btc_long_short_ratio,
)
from app.services.trading_policy_service import (
    load_trading_policy,
    policy_version_stamp,
)

logger = logging.getLogger(__name__)

MarketFilter = str

CACHE_TTL_SECONDS: Final = GATE_CACHE_TTL_SECONDS

SUPPORT_RESERVE_NET_KEY: Final = "buy.support_reserve_net"
HELD_MAJORS_SUPPORT_NET_KEY: Final = "buy.held_majors_support_net"
HELD_MAJORS_TIER_ID: Final = "held_majors_support_net"
CRYPTO_RECOVERY_GATE_KEY: Final = "market_rules.crypto.recovery_gate"

# Only live broker cash can actually be moved and spent by an order. Manual
# and paper accounts are reference rows on this board's sibling surfaces; a
# reserve verdict built on them would tell the operator to move money they do
# not have in the account that would place the order.
_RESERVE_ACCOUNT_KINDS: Final = frozenset({"live"})

_MARKET_BY_GROUP: Final[dict[str, BuyPlanMarket]] = {
    "KR": "kr",
    "US": "us",
    "CRYPTO": "crypto",
}

# Cash held at one broker cannot fill an order placed at another, so every
# requirement is resolved to a broker before it is compared with cash
# (verify-r1 B1). Both maps are deliberately closed: an account source or an
# account_mode that is not listed resolves to ``None`` and its requirement
# becomes ``unattributed`` rather than being charged to an arbitrary scope.
_BROKER_BY_ACCOUNT_SOURCE: Final[dict[str, FundingBroker]] = {
    "kis": "kis",
    "upbit": "upbit",
    "toss_api": "toss",
}
_BROKER_BY_ACCOUNT_MODE: Final[dict[str, FundingBroker]] = {
    "kis_live": "kis",
    "upbit": "upbit",
    "toss_live": "toss",
}
UNATTRIBUTED: Final[FundingBroker] = "unattributed"

# Every gate ``evaluate_auto_approve_eligibility`` can reject on, split by
# whether this read surface actually evaluates it.
#
# The codes are that function's literal ``reject(...)`` reasons;
# ``tests/.../test_service.py::test_s1_...`` extracts them from its source and
# fails if this split stops covering the real set. verify-r2 SHOULD-1 found
# the old seven-item list short by six gates — an incomplete list still reads
# as "these are the only things we skipped", which is the same lie in a
# smaller font.
AUTO_APPROVE_EVALUATED_CONDITIONS: Final[tuple[tuple[str, str], ...]] = (
    ("per_order_cap_exceeded", "건당 자동승인 상한"),
)
AUTO_APPROVE_UNEVALUATED_CONDITIONS: Final[tuple[tuple[str, str], ...]] = (
    ("unknown_auto_approve_mode", "자동승인 모드 인식 가능 여부"),
    ("action_not_supported", "지원되는 action (place/replace/cancel)"),
    ("target_evidence_missing", "replace·cancel 대상 주문 증거"),
    ("order_type_not_limit", "limit 주문 여부"),
    ("loss_cut_intent", "손절 의도 아님"),
    ("exit_intent_present", "청산 의도 아님"),
    ("account_not_veto_capable", "veto 가능한 계좌·시장"),
    ("approval_required_tag", "승인 필요 태그 스캔"),
    ("thesis_required_for_veto_card", "veto 카드용 thesis 존재"),
    ("preview_guard_failed", "fresh preview 성공"),
    ("price_or_quantity_missing", "가격·수량 확보"),
    ("daily_cap_exceeded", "일일 누적 cap 잔여"),
    ("side_not_supported", "지원되는 매매구분"),
    ("distance_below_minimum", "최소 이격 거리"),
    ("marketable_not_resting", "비-marketable(대기형) 주문"),
    ("breakeven_band", "매도 breakeven 밴드 밖"),
    ("expected_pnl_not_positive", "매도 기대손익 > 0"),
    ("sell_classification_unavailable", "매도 분류 가능"),
    # §163차 — parking-allowlist gates. Unevaluated here for the same reason as
    # the rest: this read surface has no broker balance, so it cannot know the
    # account's cumulative SGOV/BIL exposure.
    ("parking_exposure_unavailable", "파킹 보유액 조회 가능"),
    ("parking_cap_exceeded", "파킹 누적 보유 상한 잔여"),
)


@dataclass(frozen=True, slots=True)
class _SourceHealth:
    """What one upstream read model said about its own completeness.

    ``ok`` is only claimed when the upstream actively said so. An exception, a
    ``data_state`` other than ``ok``, or any warning it emitted all downgrade
    it — a degraded upstream must never be rendered as a confident zero
    (verify-r1 B2/B3/B4).
    """

    state: SourceState = "ok"
    reasons: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return self.state == "ok" and not self.reasons


@dataclass(frozen=True, slots=True)
class _Requirement:
    """One projected cash need, with the account that would have to hold it."""

    kind: str
    label: str
    currency: BuyPlanCurrency
    amount: Decimal | None
    broker: FundingBroker | None
    unattributed_reason: str | None = None


@dataclass(frozen=True, slots=True)
class _TradeableLot:
    """The tradeable slice of one grouped holding.

    Manual/reference sources are excluded: their quantity is not sellable or
    addable through a broker, so folding them into the average would move the
    turn point to a price no order can act on.
    """

    market: BuyPlanMarket
    symbol: str
    symbol_name: str | None
    currency: BuyPlanCurrency
    quantity: Decimal
    cost_basis: Decimal
    average_price: Decimal
    current_price: Decimal | None
    unrealized_pnl_pct: Decimal | None
    account_sources: list[str]
    price_state: str
    excluded_reference_quantity: Decimal


def _dec(value: object) -> Decimal | None:
    """Convert a read-model float to Decimal without inventing precision."""

    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def crypto_base_symbol(symbol: str) -> str:
    """``KRW-XRP`` / ``xrp`` → ``XRP``.

    Holdings key crypto by base currency while open orders and watches key it
    by the Upbit market pair, so both sides are folded to the base token
    before matching.
    """

    token = (symbol or "").strip().upper()
    if "-" in token:
        return token.split("-", 1)[1]
    return token


def _match_key(market: BuyPlanMarket, symbol: str) -> str:
    if market == "crypto":
        return f"crypto:{crypto_base_symbol(symbol)}"
    return f"{market}:{(symbol or '').strip().upper()}"


class BuyPlanService:
    """Compose the 매수 계획 board from existing read models."""

    def __init__(
        self,
        *,
        home_service: Any,
        watch_service: Any,
        open_orders_service: Any,
    ) -> None:
        self._home = home_service
        self._watches = watch_service
        self._open_orders = open_orders_service

    async def build(
        self,
        *,
        user_id: int,
        market: MarketFilter = "all",
        now: dt.datetime | None = None,
    ) -> BuyPlanResponse:
        as_of = now or dt.datetime.now(dt.UTC)
        warnings: list[str] = []

        policy = load_trading_policy()
        stamp = policy_version_stamp()

        home, home_health = await self._safe_home(user_id=user_id, warnings=warnings)
        watches, watch_health = await self._safe_watches(warnings=warnings)
        open_orders, order_health = await self._safe_open_orders(warnings=warnings)

        lots = _tradeable_lots(home, warnings=warnings)
        wanted = _wanted_markets(market)
        scoped_lots = [lot for lot in lots if lot.market in wanted]

        averaging = _build_averaging_rows(scoped_lots, policy=policy)
        support_net = _build_support_net(
            [lot for lot in scoped_lots if lot.market == "crypto"],
            policy=policy,
            open_orders=open_orders,
            watches=watches,
            crypto_in_scope="crypto" in wanted,
            order_health=order_health,
            watch_health=watch_health,
        )
        buy_watches = _build_active_buy_watches(watches, policy=policy, wanted=wanted)
        gates = (
            await _build_discovery_gates(policy=policy) if "crypto" in wanted else []
        )
        funding = _build_funding(
            home,
            averaging=averaging,
            support_net=support_net,
            buy_watches=buy_watches,
            home_health=home_health,
            watch_health=watch_health,
            order_health=order_health,
            warnings=warnings,
        )

        return BuyPlanResponse(
            as_of=as_of,
            policy=PolicyStamp(
                version=stamp["version"], content_hash=stamp["content_hash"]
            ),
            cache_ttl_seconds=CACHE_TTL_SECONDS,
            approximation_notice=APPROXIMATION_NOTICE,
            approval_context=_approval_context(),
            market=market if market in {"all", "kr", "us", "crypto"} else "all",
            averaging_triggers=averaging,
            support_net=support_net,
            active_buy_watches=buy_watches,
            discovery_gates=gates,
            funding=funding,
            value_sources=_value_sources(),
            warnings=warnings,
        )

    async def _safe_home(
        self, *, user_id: int, warnings: list[str]
    ) -> tuple[InvestHomeResponse | None, _SourceHealth]:
        try:
            home = await self._home.get_home(user_id=user_id)
        except Exception as exc:  # noqa: BLE001 — one dead reader must not 500 the board
            logger.warning("buy_plan: holdings/cash unavailable: %s", exc)
            reason = f"보유·현금 조회 실패 — 자금 대조 불가: {exc}"
            warnings.append(reason)
            return None, _SourceHealth(state="unavailable", reasons=(reason,))

        # A partial failure does NOT raise: InvestHomeService returns a normal
        # response with the dead source's accounts simply absent and a warning
        # in meta. Reading only the exception path let a missing KIS account
        # silently shrink the cash total (verify-r1 B2).
        reasons = tuple(
            f"보유·현금 소스 누락({warning.source}): {warning.message}"
            for warning in home.meta.warnings
        )
        for reason in reasons:
            warnings.append(reason)
        return home, _SourceHealth(
            state="degraded" if reasons else "ok", reasons=reasons
        )

    async def _safe_watches(
        self, *, warnings: list[str]
    ) -> tuple[WatchesResponse | None, _SourceHealth]:
        try:
            watches = await self._watches.list_watches(market="all", status="active")
        except Exception as exc:  # noqa: BLE001
            logger.warning("buy_plan: watches unavailable: %s", exc)
            reason = f"워치 조회 실패 — 활성 매수 워치 누락: {exc}"
            warnings.append(reason)
            return None, _SourceHealth(state="unavailable", reasons=(reason,))
        return watches, _watches_health(watches, warnings=warnings)

    async def _safe_open_orders(
        self, *, warnings: list[str]
    ) -> tuple[OpenOrdersResponse | None, _SourceHealth]:
        try:
            orders = await self._open_orders.list_open_orders(market="all")
        except Exception as exc:  # noqa: BLE001
            logger.warning("buy_plan: open orders unavailable: %s", exc)
            reason = f"미체결 주문 조회 실패 — 그물에 이미 걸린 주문이 누락됐을 수 있습니다: {exc}"
            warnings.append(reason)
            return None, _SourceHealth(state="unavailable", reasons=(reason,))
        return orders, _open_orders_health(orders, warnings=warnings)


def _watches_health(watches: WatchesResponse, *, warnings: list[str]) -> _SourceHealth:
    """Trust the watch panel's own completeness signal.

    ``WatchPanelService`` reports partial reads two ways: a non-``ok``
    ``data_state``, and per-alert build failures surfaced as warnings with the
    offending items dropped from ``items``. Either way the requirement side of
    the board is short by an unknown amount (verify-r1 B3).
    """

    reasons: list[str] = []
    if watches.data_state != "ok":
        reasons.append(
            f"워치 조회 상태 {watches.data_state} — 누락된 워치가 있을 수 있습니다."
        )
    for warning in watches.warnings:
        reasons.append(f"워치 경고: {warning}")
    for reason in reasons:
        warnings.append(reason)
    state: SourceState = "ok"
    if watches.data_state == "unavailable":
        state = "unavailable"
    elif reasons:
        state = "degraded"
    return _SourceHealth(state=state, reasons=tuple(reasons))


def _open_orders_health(
    orders: OpenOrdersResponse, *, warnings: list[str]
) -> _SourceHealth:
    """Trust the open-order reader's own completeness signal.

    An empty ``items`` from a degraded collector means "we could not see the
    resting orders", not "there are none" — and the support net publishes both
    committed notional and remaining headroom off that list (verify-r1 B4).
    """

    reasons: list[str] = []
    if orders.data_state != "ok":
        reasons.append(
            f"미체결 주문 조회 상태 {orders.data_state} — 이미 걸린 주문이 누락됐을 수 있습니다."
        )
    for source in orders.sources:
        if source.status != "ok":
            reasons.append(
                f"미체결 주문 소스 {source.broker}/{source.market} 상태 {source.status}"
                + (f": {source.message}" if source.message else "")
            )
    for warning in orders.warnings:
        reasons.append(f"미체결 주문 경고: {warning}")
    for reason in reasons:
        warnings.append(reason)
    state: SourceState = "ok"
    if orders.data_state == "unavailable":
        state = "unavailable"
    elif reasons:
        state = "degraded"
    return _SourceHealth(state=state, reasons=tuple(reasons))


def _broker_from_account_sources(
    sources: list[str],
) -> tuple[FundingBroker | None, str | None]:
    """Resolve a holding's tradeable sources to exactly one broker.

    A lot split across two brokers has no single account that must fund the
    add, so it is left unattributed instead of being charged to whichever
    source happened to sort first.
    """

    if not sources:
        return None, "보유의 계좌 소스를 확인하지 못했습니다."
    resolved = {_BROKER_BY_ACCOUNT_SOURCE.get(source) for source in sources}
    if None in resolved:
        unknown = sorted(
            source for source in sources if source not in _BROKER_BY_ACCOUNT_SOURCE
        )
        return None, f"매핑되지 않은 계좌 소스: {', '.join(unknown)}"
    if len(resolved) != 1:
        return None, "여러 브로커에 걸친 보유 — 어느 계좌가 낼지 확정 불가"
    return next(iter(resolved)), None


def _broker_from_account_mode(mode: object) -> tuple[FundingBroker | None, str | None]:
    if not isinstance(mode, str) or not mode.strip():
        return None, "워치 max_action에 account_mode가 없습니다."
    resolved = _BROKER_BY_ACCOUNT_MODE.get(mode.strip())
    if resolved is None:
        return None, f"매핑되지 않은 account_mode: {mode.strip()}"
    return resolved, None


def _approval_context() -> ApprovalContext:
    """Publish how far the board's approval-lane classification actually goes.

    ``approval_lane`` compares notionals with two caps. Real dispatch consults
    ``settings.ORDER_PROPOSALS_AUTO_APPROVE`` first (default False) and then a
    further eligibility evaluation this surface never runs. Both facts ship in
    the response so the UI cannot present a cap check as an approval decision
    (verify-r1 B6).
    """

    setting = "ORDER_PROPOSALS_AUTO_APPROVE"
    try:
        from app.core.config import settings

        enabled: bool | None = bool(getattr(settings, setting))
        source = f"settings.{setting}"
    except Exception as exc:  # noqa: BLE001 — unknown gate must read as unknown
        logger.warning("buy_plan: auto-approve master gate unreadable: %s", exc)
        enabled = None
        source = f"settings.{setting} (읽기 실패)"

    if enabled is False:
        notice = (
            "자동승인 마스터 게이트가 꺼져 있습니다 — 조건과 무관하게 전건이 "
            "수동 승인 카드로 갑니다. 아래 레인 표시는 cap 기준 분류일 뿐입니다."
        )
    elif enabled is True:
        notice = (
            "자동승인 마스터 게이트는 켜져 있지만, 이 보드는 cap 두 개만 확인합니다 — "
            "실제 자동제출에는 아래 조건이 더 필요하며 여기서 평가하지 않았습니다."
        )
    else:
        notice = (
            "자동승인 마스터 게이트 상태를 읽지 못했습니다 — 레인 표시를 "
            "승인 예정으로 읽지 마십시오."
        )
    return ApprovalContext(
        master_gate_enabled=enabled,
        master_gate_setting=setting,
        master_gate_source=source,
        unevaluated_conditions=[
            ApprovalCondition(code=code, label=label)
            for code, label in AUTO_APPROVE_UNEVALUATED_CONDITIONS
        ],
        evaluated_conditions=[
            ApprovalCondition(code=code, label=label)
            for code, label in AUTO_APPROVE_EVALUATED_CONDITIONS
        ],
        notice=notice,
    )


def _wanted_markets(market: MarketFilter) -> frozenset[BuyPlanMarket]:
    if market in {"kr", "us", "crypto"}:
        return frozenset({market})  # type: ignore[arg-type]
    return frozenset({"kr", "us", "crypto"})


def _tradeable_lots(
    home: InvestHomeResponse | None, *, warnings: list[str]
) -> list[_TradeableLot]:
    if home is None:
        return []
    lots: list[_TradeableLot] = []
    for group in home.groupedHoldings:
        lot = _lot_from_group(group)
        if lot is not None:
            lots.append(lot)
    if not lots and home.groupedHoldings:
        warnings.append("거래 가능한 보유가 없어 트리거 행이 비었습니다.")
    return lots


def _lot_from_group(group: GroupedHolding) -> _TradeableLot | None:
    market = _MARKET_BY_GROUP.get(group.market)
    if market is None:
        return None

    quantity = Decimal(0)
    cost_basis = Decimal(0)
    sources: list[str] = []
    reference_quantity = Decimal(0)
    for part in group.sourceBreakdown:
        part_qty = _dec(part.quantity) or Decimal(0)
        if not part.isTradeable:
            reference_quantity += part_qty
            continue
        if part_qty <= 0:
            continue
        part_cost = _dec(part.costBasis)
        if part_cost is None:
            avg = _dec(part.averageCost)
            part_cost = avg * part_qty if avg is not None else None
        if part_cost is None or part_cost <= 0:
            # No usable cost basis for this slice — A(k) is undefined without
            # it, so the slice is dropped rather than guessed at.
            continue
        quantity += part_qty
        cost_basis += part_cost
        sources.append(str(part.source))

    if quantity <= 0 or cost_basis <= 0:
        return None

    total_quantity = _dec(group.totalQuantity) or Decimal(0)
    value_native = _dec(group.valueNative)
    current_price = (
        value_native / total_quantity
        if value_native is not None and total_quantity > 0
        else None
    )
    pnl_rate = _dec(group.pnlRate)
    return _TradeableLot(
        market=market,
        symbol=group.symbol,
        symbol_name=group.displayName,
        currency="KRW" if group.currency == "KRW" else "USD",
        quantity=quantity,
        cost_basis=cost_basis,
        average_price=cost_basis / quantity,
        current_price=current_price,
        # pnlRate is the group-level rate the rest of /invest already shows;
        # reusing it keeps this board consistent with the holdings table
        # instead of publishing a second, subtly different P&L number.
        unrealized_pnl_pct=pnl_rate * Decimal(100) if pnl_rate is not None else None,
        account_sources=sorted(set(sources)),
        price_state=group.priceState,
        excluded_reference_quantity=reference_quantity,
    )


def _support_reserve_net_rule(policy: Any) -> Any | None:
    return policy.decision_rules.get(SUPPORT_RESERVE_NET_KEY)


def _per_order_auto_approve_cap(policy: Any, market: BuyPlanMarket) -> Decimal | None:
    caps = policy.order_proposals.auto_approve.per_order_cap
    value = caps.get(market)
    return _dec(value)


def _tier_auto_submit_notional(rule: Any, currency: BuyPlanCurrency) -> Decimal | None:
    if rule is None:
        return None
    notional = getattr(rule, "auto_submit_notional", None)
    if notional is None:
        return None
    return _dec(notional.krw if currency == "KRW" else notional.usd)


def _build_averaging_rows(
    lots: list[_TradeableLot], *, policy: Any
) -> list[AveragingTriggerRow]:
    """One row per underwater tradeable lot, ranked by nearness to its turn point."""

    rule = _support_reserve_net_rule(policy)
    add_candidate = getattr(rule, "add_candidate", None)
    k = _dec(getattr(add_candidate, "k_used", None))
    max_add_per_market = getattr(add_candidate, "max_add_symbols_per_market", None)
    if k is None or k <= 0:
        return []

    rows: list[AveragingTriggerRow] = []
    for lot in lots:
        if lot.current_price is None or lot.current_price <= 0:
            continue
        projection = averaging_turn_point(
            cost_basis=lot.cost_basis,
            average_price=lot.average_price,
            current_price=lot.current_price,
            k=k,
        )
        cap = _per_order_auto_approve_cap(policy, lot.market)
        tier_ceiling = _tier_auto_submit_notional(rule, lot.currency)
        samples: list[AveragingSampleRow] = []
        for sample in projection.samples:
            lane, reason = approval_lane_for(
                notional=sample.additional_notional,
                tier_auto_submit_notional=tier_ceiling,
                per_order_auto_approve_cap=cap,
            )
            samples.append(
                AveragingSampleRow(
                    offset_from_turn_point_pct=sample.offset_from_turn_point_pct,
                    price=sample.price,
                    additional_notional=sample.additional_notional,
                    target_average_price=sample.target_average_price,
                    approval_lane=lane,
                    approval_lane_reason=reason,
                )
            )
        if not samples:
            continue

        lot_broker, lot_broker_reason = _broker_from_account_sources(
            lot.account_sources
        )
        notes: list[str] = []
        if lot_broker is None and lot_broker_reason:
            notes.append(
                f"이 소요액을 어느 계좌가 낼지 확정하지 못했습니다 — {lot_broker_reason}"
            )
        if lot.price_state != "live":
            notes.append(
                f"현재가 상태 {lot.price_state} — 전환점 거리는 그만큼 오래된 값입니다."
            )
        if lot.excluded_reference_quantity > 0:
            notes.append(
                "수동/참고 보유 수량은 평단·전환점 계산에서 제외했습니다"
                f" (제외 수량 {format(lot.excluded_reference_quantity, 'f')})."
            )

        rows.append(
            AveragingTriggerRow(
                market=lot.market,
                symbol=lot.symbol,
                symbol_name=lot.symbol_name,
                currency=lot.currency,
                account_sources=lot.account_sources,
                quantity=lot.quantity,
                average_price=lot.average_price,
                cost_basis=lot.cost_basis,
                current_price=lot.current_price,
                unrealized_pnl_pct=lot.unrealized_pnl_pct,
                k=projection.k,
                turn_point_price=projection.turn_point_price,
                distance_to_turn_point_pct=projection.distance_to_turn_point_pct,
                turn_point_reached=projection.reached,
                samples=samples,
                # The deeper sample is the conservative reserve: if the price
                # keeps falling past the turn point, this is the larger of the
                # two published cash needs.
                reserve_plan_notional=max(
                    sample.additional_notional for sample in projection.samples
                ),
                market_rank=0,
                within_policy_add_cap=False,
                funding_broker=lot_broker or UNATTRIBUTED,
                funding_broker_reason=lot_broker_reason,
                notes=notes,
            )
        )

    # Nearest to (or deepest past) the turn point first — that is the order in
    # which these triggers actually become live. The policy's own ranking
    # (support strength, R-931 state, sector delta) needs inputs this board
    # does not read, so the basis is stated in value_sources rather than
    # dressed up as the policy sort.
    rows.sort(key=lambda row: (row.market, row.distance_to_turn_point_pct))
    ranked: list[AveragingTriggerRow] = []
    per_market: dict[str, int] = {}
    for row in rows:
        rank = per_market.get(row.market, 0) + 1
        per_market[row.market] = rank
        within_cap = (
            rank <= int(max_add_per_market) if max_add_per_market is not None else False
        )
        ranked.append(
            row.model_copy(
                update={"market_rank": rank, "within_policy_add_cap": within_cap}
            )
        )
    ranked.sort(key=lambda row: (row.distance_to_turn_point_pct, row.symbol))
    return ranked


def _held_majors_tier(policy: Any) -> tuple[Any, dict[str, Any]] | tuple[None, dict]:
    rule = policy.decision_rules.get(HELD_MAJORS_SUPPORT_NET_KEY)
    if rule is None:
        return None, {}
    for tier in getattr(rule, "tiers", []) or []:
        if getattr(tier, "id", None) == HELD_MAJORS_TIER_ID:
            conditions = getattr(tier, "conditions", {}) or {}
            if not isinstance(conditions, dict):
                conditions = dict(conditions)
            return rule, conditions
    return rule, {}


def _build_support_net(
    crypto_lots: list[_TradeableLot],
    *,
    policy: Any,
    open_orders: OpenOrdersResponse | None,
    watches: WatchesResponse | None,
    crypto_in_scope: bool,
    order_health: _SourceHealth,
    watch_health: _SourceHealth,
) -> SupportNetTier:
    rule, conditions = _held_majors_tier(policy)
    per_symbol_cap = _dec(conditions.get("max_notional_krw_per_coin"))
    tier_cap = _dec(conditions.get("max_notional_krw_per_tier"))
    band_raw = conditions.get("support_distance_from_current_pct_range") or []
    band = [d for d in (_dec(v) for v in band_raw) if d is not None]
    review_date = conditions.get("review_date")
    notes: list[str] = []
    if rule is None:
        notes.append(
            f"정책에 {HELD_MAJORS_SUPPORT_NET_KEY} 티어가 없습니다 — 은퇴했거나 미등록."
        )

    resting = _resting_buy_by_symbol(open_orders)
    watch_rungs = _buy_watch_by_symbol(watches)

    # Committed notional is the sum of what the placement sources could see.
    # If either source said it was incomplete, that sum is a floor, not a
    # total — and the headroom derived from it would be an over-estimate of
    # what is still free to spend, so both are published as unknown
    # (verify-r1 B4).
    placements_reasons = [*order_health.reasons, *watch_health.reasons]
    placements_confident = order_health.complete and watch_health.complete
    placements_state: SourceState = "ok"
    if "unavailable" in {order_health.state, watch_health.state}:
        placements_state = "unavailable"
    elif not placements_confident:
        placements_state = "degraded"

    rows: list[SupportNetRow] = []
    placed_total = Decimal(0)
    if crypto_in_scope and rule is not None:
        for lot in sorted(crypto_lots, key=lambda item: item.symbol):
            key = _match_key("crypto", lot.symbol)
            placements = [
                *resting.get(key, ()),
                *watch_rungs.get(key, ()),
            ]
            placements = [
                _annotate_placement(p, current_price=lot.current_price, band=band)
                for p in placements
            ]
            placed = sum(
                (p.notional for p in placements if p.notional is not None),
                Decimal(0),
            )
            placed_total += placed
            eligible, reason = _support_net_eligibility(lot)
            headroom = (
                max(per_symbol_cap - placed, Decimal(0))
                if per_symbol_cap is not None and eligible
                else Decimal(0)
            )
            rows.append(
                SupportNetRow(
                    market="crypto",
                    symbol=lot.symbol,
                    symbol_name=lot.symbol_name,
                    currency=lot.currency,
                    quantity=lot.quantity,
                    average_price=lot.average_price,
                    current_price=lot.current_price,
                    unrealized_pnl_pct=lot.unrealized_pnl_pct,
                    eligible=eligible,
                    ineligible_reason=reason,
                    placements=placements,
                    placed_notional=placed if placements_confident else None,
                    per_symbol_cap_notional=per_symbol_cap or Decimal(0),
                    remaining_headroom_notional=(
                        headroom if placements_confident else None
                    ),
                    placements_state=placements_state,
                    placements_incomplete_reasons=list(placements_reasons),
                )
            )

    remaining = (
        max(tier_cap - placed_total, Decimal(0)) if tier_cap is not None else None
    )
    if not placements_confident:
        remaining = None
    if rule is not None:
        notes.append(
            "그물 대상 판정(‘메이저’)은 세션 판단이며 기계 allowlist가 없습니다 — "
            "여기서는 보유·이익권 두 조건만 기계로 확인합니다."
        )
        notes.append(
            "이미 걸린 지정가(주문 상시형)는 브로커가 현금을 이미 묶고 있으므로 "
            "추가 입금 대상이 아닙니다. 워치형만 신규 소요액으로 집계합니다."
        )
    if not placements_confident and crypto_in_scope and rule is not None:
        notes.append(
            "미체결/워치 소스가 불완전해 걸린 금액과 남은 여력을 확정하지 못했습니다 — "
            "표시된 여력을 쓸 수 있는 돈으로 읽지 마십시오."
        )
    return SupportNetTier(
        policy_key=HELD_MAJORS_SUPPORT_NET_KEY,
        enabled=rule is not None and crypto_in_scope,
        currency="KRW",
        tier_cap_notional=tier_cap,
        per_symbol_cap_notional=per_symbol_cap,
        placed_notional=placed_total if placements_confident else None,
        remaining_notional=remaining,
        distance_band_pct=band,
        review_date=str(review_date) if review_date is not None else None,
        rows=rows,
        notes=notes,
        placements_state=placements_state,
        placements_incomplete_reasons=list(placements_reasons),
    )


def _support_net_eligibility(lot: _TradeableLot) -> tuple[bool, str | None]:
    """The two conditions the policy actually expresses in machine terms."""

    if lot.quantity <= 0:
        return False, "보유 수량 없음"
    if lot.unrealized_pnl_pct is None:
        return False, "평가손익 확인 불가 — 이익권 판정 불가"
    if lot.unrealized_pnl_pct <= 0:
        return False, "이익권 아님 (평가손익 ≤ 0)"
    return True, None


def _annotate_placement(
    placement: SupportNetPlacement,
    *,
    current_price: Decimal | None,
    band: list[Decimal],
) -> SupportNetPlacement:
    if placement.anchor_price is None or current_price is None or current_price <= 0:
        return placement
    distance = (placement.anchor_price - current_price) / current_price * Decimal(100)
    within = None
    if len(band) == 2:
        low, high = min(band), max(band)
        within = low <= distance <= high
    return placement.model_copy(
        update={
            "distance_from_current_pct": distance,
            "within_policy_distance_band": within,
        }
    )


def _resting_buy_by_symbol(
    open_orders: OpenOrdersResponse | None,
) -> dict[str, list[SupportNetPlacement]]:
    out: dict[str, list[SupportNetPlacement]] = {}
    if open_orders is None:
        return out
    for order in open_orders.items:
        if order.side != "buy":
            continue
        market = order.market
        remaining = order.remaining_qty
        if remaining is None:
            remaining = order.quantity
        notional = (
            order.price * remaining
            if order.price is not None and remaining is not None
            else None
        )
        key = _match_key(market, order.symbol)
        out.setdefault(key, []).append(
            SupportNetPlacement(
                form="resting_order",
                reference=f"{order.broker}:{order.order_no}",
                anchor_price=order.price,
                quantity=remaining,
                notional=notional,
            )
        )
    return out


def _buy_watch_by_symbol(
    watches: WatchesResponse | None,
) -> dict[str, list[SupportNetPlacement]]:
    out: dict[str, list[SupportNetPlacement]] = {}
    if watches is None:
        return out
    for alert in watches.items:
        if not _is_buy_watch(alert):
            continue
        notional, _ = _planned_notional(alert)
        key = _match_key(alert.market, alert.symbol)
        out.setdefault(key, []).append(
            SupportNetPlacement(
                form="watch",
                reference=str(alert.alert_uuid),
                anchor_price=alert.threshold,
                quantity=_dec((alert.max_action or {}).get("quantity")),
                notional=notional,
                valid_until=alert.valid_until,
            )
        )
    return out


def _is_buy_watch(alert: WatchAlertRow) -> bool:
    """Buy-side only.

    ``max_action.side`` is the typed execution-plan field (CLAUDE.md item
    contract) and is authoritative when present. ``intent`` is the fallback for
    older rows that predate ``max_action``.
    """

    side = str((alert.max_action or {}).get("side") or "").strip().lower()
    if side in {"buy", "sell"}:
        return side == "buy"
    return alert.intent == "buy_review"


def _planned_notional(alert: WatchAlertRow) -> tuple[Decimal | None, str | None]:
    """Cash this watch would need if it fired, from its own execution plan."""

    action = alert.max_action or {}
    for key in ("notional", "amount_krw"):
        value = _dec(action.get(key))
        if value is not None and value > 0:
            return value, f"max_action.{key}"
    quantity = _dec(action.get("quantity"))
    if quantity is not None and quantity > 0:
        price = _dec(action.get("limit_price")) or _dec(action.get("limit_price_hint"))
        if price is None:
            # The threshold is the level the watch fires at, so it is the
            # closest thing to a fill price this read model has. Labelled so
            # the UI can show it as an estimate, not a plan value.
            price = alert.threshold
        if price is not None and price > 0:
            return quantity * price, "max_action.quantity × 트리거 레벨"
    return None, None


def _build_active_buy_watches(
    watches: WatchesResponse | None,
    *,
    policy: Any,
    wanted: frozenset[BuyPlanMarket],
) -> list[ActiveBuyWatchRow]:
    if watches is None:
        return []
    rule = _support_reserve_net_rule(policy)
    rows: list[ActiveBuyWatchRow] = []
    for alert in watches.items:
        if alert.market not in wanted or not _is_buy_watch(alert):
            continue
        currency: BuyPlanCurrency = "USD" if alert.market == "us" else "KRW"
        notional, source = _planned_notional(alert)
        raw_mode = (alert.max_action or {}).get("account_mode")
        broker, broker_reason = _broker_from_account_mode(raw_mode)
        lane, reason = approval_lane_for(
            notional=notional,
            tier_auto_submit_notional=_tier_auto_submit_notional(rule, currency),
            per_order_auto_approve_cap=_per_order_auto_approve_cap(
                policy, alert.market
            ),
        )
        distance = None
        if alert.current_price is not None and alert.current_price > 0:
            distance = (
                (alert.threshold - alert.current_price)
                / alert.current_price
                * Decimal(100)
            )
        rows.append(
            ActiveBuyWatchRow(
                market=alert.market,
                symbol=alert.symbol,
                symbol_name=alert.symbol_name,
                currency=currency,
                alert_uuid=str(alert.alert_uuid),
                metric=alert.metric,
                operator=alert.operator,
                threshold=alert.threshold,
                threshold_high=alert.threshold_high,
                current_price=alert.current_price,
                distance_to_threshold_pct=distance,
                valid_until=alert.valid_until,
                near_expiry=alert.near_expiry,
                planned_notional=notional,
                planned_notional_source=source,
                funding_broker=broker or UNATTRIBUTED,
                funding_broker_reason=broker_reason,
                account_mode=raw_mode if isinstance(raw_mode, str) else None,
                approval_lane=lane,
                approval_lane_reason=reason,
            )
        )
    rows.sort(key=lambda row: (row.valid_until, row.symbol))
    return rows


async def _build_discovery_gates(*, policy: Any) -> list[DiscoveryGateRow]:
    crypto_rules = policy.market_rules.get("crypto")
    gate = getattr(crypto_rules, "recovery_gate", None)
    if gate is None:
        return []

    readings: dict[str, GateMetricReading] = {}
    breadth = await read_alt_breadth_24h()
    readings[breadth.metric] = breadth
    lsr = await read_btc_long_short_ratio()
    readings[lsr.metric] = lsr

    conditions: list[DiscoveryGateCondition] = []
    met = 0
    unavailable = 0
    for condition in gate.conditions:
        reading = readings.get(condition.metric)
        threshold = _dec(condition.threshold)
        value = reading.value if reading is not None else None
        if value is None or threshold is None or not condition.operator:
            state = "unavailable"
            unavailable += 1
        else:
            passed = _compare(condition.operator, value, threshold)
            state = "met" if passed else "not_met"
            if passed:
                met += 1
        conditions.append(
            DiscoveryGateCondition(
                condition_id=condition.id,
                metric=condition.metric,
                comparison=condition.operator,
                threshold=threshold,
                unit=condition.unit,
                current_value=value,
                state=state,  # type: ignore[arg-type]
                # Provenance comes from the policy's own ``sources`` list, not
                # from a constant duplicated in the reader — the policy file is
                # what declares which upstreams this metric is allowed to come
                # from, so it cannot drift out of sync with the gate.
                source="+".join(condition.sources) if condition.sources else None,
                note=reading.note
                if reading is not None
                else "이 지표를 읽는 소스가 배선돼 있지 않습니다.",
            )
        )

    if unavailable:
        # policy: missing_or_null_threshold = do_not_infer_or_count_as_met.
        # An unreadable input can never be counted toward the gate, so the
        # honest states are "already open on what we could read" or
        # "indeterminate" — never "open" by assumption.
        state = "open" if met >= gate.min_conditions_met else "indeterminate"
    else:
        state = "open" if met >= gate.min_conditions_met else "closed"

    notes = [
        f"미확인 조건은 충족으로 세지 않습니다 ({gate.missing_or_null_threshold}).",
    ]
    if getattr(gate, "advisory", False):
        notes.append("이 게이트는 advisory입니다 — 코드가 주문을 막지 않습니다.")

    return [
        DiscoveryGateRow(
            market="crypto",
            gate_key=CRYPTO_RECOVERY_GATE_KEY,
            state=state,  # type: ignore[arg-type]
            min_conditions_met=gate.min_conditions_met,
            of=gate.of,
            met_count=met,
            unavailable_count=unavailable,
            semantics=gate.semantics,
            conditions=conditions,
            notes=notes,
        )
    ]


def _compare(operator: str, value: Decimal, threshold: Decimal) -> bool:
    if operator == "gt":
        return value > threshold
    if operator == "gte":
        return value >= threshold
    if operator == "lt":
        return value < threshold
    if operator == "lte":
        return value <= threshold
    if operator == "eq":
        return value == threshold
    # An operator this board does not implement must not silently pass.
    return False


def _requirements(
    *,
    averaging: list[AveragingTriggerRow],
    support_net: SupportNetTier,
    buy_watches: list[ActiveBuyWatchRow],
) -> list[_Requirement]:
    """Flatten the board's three requirement families into attributed amounts.

    A watch that is also a support-net rung appears here exactly once, keyed by
    its alert id. The previous version subtracted one total from the other,
    which happened to work but could not say *which* account either belonged
    to; identity-based de-duplication does both jobs at once.
    """

    out: list[_Requirement] = []

    for row in averaging:
        if not row.within_policy_add_cap:
            # Outside the policy's per-market add cap — shown on the board but
            # not money to reserve for.
            continue
        out.append(
            _Requirement(
                kind="averaging_add",
                label=f"{row.symbol} 물타기 A(k)",
                currency=row.currency,
                amount=row.reserve_plan_notional,
                broker=None
                if row.funding_broker == UNATTRIBUTED
                else row.funding_broker,
                # The row carries the concrete reason (an unmapped source name,
                # a split lot); collapsing it to one generic sentence hid real
                # misconfiguration (verify-r2 SHOULD-3).
                unattributed_reason=(
                    row.funding_broker_reason
                    or "보유의 계좌 소스를 확정하지 못했습니다."
                    if row.funding_broker == UNATTRIBUTED
                    else None
                ),
            )
        )

    rung_alert_ids = {
        placement.reference
        for row in support_net.rows
        if row.eligible
        for placement in row.placements
        if placement.form == "watch"
    }

    for row in buy_watches:
        kind = "support_net" if row.alert_uuid in rung_alert_ids else "active_watch"
        out.append(
            _Requirement(
                kind=kind,
                label=f"{row.symbol} 워치 {format(row.threshold, 'f')}",
                currency=row.currency,
                amount=row.planned_notional,
                broker=None
                if row.funding_broker == UNATTRIBUTED
                else row.funding_broker,
                unattributed_reason=(
                    row.funding_broker_reason
                    or "워치 max_action이 실행 계좌(account_mode)를 지정하지 않았습니다."
                    if row.funding_broker == UNATTRIBUTED
                    else None
                ),
            )
        )
    return out


def _build_funding(
    home: InvestHomeResponse | None,
    *,
    averaging: list[AveragingTriggerRow],
    support_net: SupportNetTier,
    buy_watches: list[ActiveBuyWatchRow],
    home_health: _SourceHealth,
    watch_health: _SourceHealth,
    order_health: _SourceHealth,
    warnings: list[str],
) -> BuyPlanFunding:
    accounts = _cash_accounts(home)
    requirements = _requirements(
        averaging=averaging, support_net=support_net, buy_watches=buy_watches
    )

    # Requirements whose owning account could not be resolved are published,
    # never dropped. They then hold every same-currency scope at ``unknown``
    # unless that scope's cash covers its own needs plus all of them.
    unattributed = [
        UnattributedRequirement(
            kind=req.kind,  # type: ignore[arg-type]
            label=req.label,
            currency=req.currency,
            amount=req.amount,
            reason=req.unattributed_reason or "귀속 계좌 불명",
        )
        for req in requirements
        if req.broker is None
    ]
    unattributed_by_currency: dict[str, Decimal] = {
        "KRW": Decimal(0),
        "USD": Decimal(0),
    }
    unattributed_unknown_amount: dict[str, bool] = {"KRW": False, "USD": False}
    for req in requirements:
        if req.broker is not None:
            continue
        if req.amount is None:
            unattributed_unknown_amount[req.currency] = True
            continue
        unattributed_by_currency[req.currency] += req.amount

    # A requirement whose amount could not be resolved is also an unknown, and
    # it must not read as zero.
    amount_unknown_by_scope: dict[tuple[str, str], list[str]] = {}
    for req in requirements:
        if req.amount is None and req.broker is not None:
            amount_unknown_by_scope.setdefault((req.broker, req.currency), []).append(
                f"{req.label}: 소요 금액을 확정하지 못했습니다."
            )

    scope_keys: set[tuple[FundingBroker, BuyPlanCurrency]] = set()
    for row in accounts:
        if row.included_in_reserve:
            scope_keys.add((row.broker, row.currency))
    for req in requirements:
        # An unresolved requirement gets its own destination row rather than
        # being folded into whichever broker happens to hold that currency.
        # Without this, a currency with no cash account (unattributed USD on a
        # KRW-only book) reached no verdict at all, and a broker with no row
        # left the operator no account to fund (verify-r2 B1 (b)/(d)).
        scope_keys.add((req.broker or UNATTRIBUTED, req.currency))
    if not scope_keys:
        scope_keys.add((UNATTRIBUTED, "KRW"))

    # Requirement-side incompleteness is symmetric with the cash-side hold that
    # already existed: an unknown requirement must never fold to zero
    # (verify-r1 B3/B4).
    shared_incomplete: list[str] = [
        *home_health.reasons,
        *watch_health.reasons,
        *order_health.reasons,
    ]

    scopes: list[ScopeReconciliation] = []
    for broker, currency in sorted(scope_keys):
        rows = [
            row
            for row in accounts
            if row.included_in_reserve
            and row.broker == broker
            and row.currency == currency
        ]
        available = _scope_available_cash(rows, warnings=warnings)

        by_kind: dict[str, Decimal] = {
            "averaging_add": Decimal(0),
            "support_net": Decimal(0),
            "active_watch": Decimal(0),
        }
        for req in requirements:
            if (req.broker or UNATTRIBUTED) != broker or req.currency != currency:
                continue
            if req.amount is None:
                continue
            by_kind[req.kind] += req.amount
        attributed_total = sum(by_kind.values(), Decimal(0))

        incomplete = [
            *shared_incomplete,
            *amount_unknown_by_scope.get((broker, currency), []),
        ]
        if unattributed_unknown_amount[currency]:
            incomplete.append(
                f"{currency} 소요액 중 금액을 확정하지 못한 항목이 있습니다."
            )
        if broker == UNATTRIBUTED:
            incomplete.append(
                "이 행은 귀속 계좌를 확정하지 못한 소요액 모음입니다 — 어느 계좌에서 "
                "나갈지 모르므로 대조할 현금이 없습니다."
            )

        # A real broker scope is measured against the unattributed pool; the
        # unattributed destination row already *contains* that pool, so adding
        # it again would double-count it.
        unattributed_amount = (
            Decimal(0) if broker == UNATTRIBUTED else unattributed_by_currency[currency]
        )
        unattributed_present = broker != UNATTRIBUTED and (
            unattributed_amount > 0 or unattributed_unknown_amount[currency]
        )
        upper_bound = attributed_total + unattributed_amount
        verdict, shortfall = _scope_verdict(
            available=available,
            attributed=attributed_total,
            unattributed_present=unattributed_present,
            incomplete=bool(incomplete),
        )

        notes = [
            "다른 브로커 계좌의 같은 통화 현금은 이 판정에 포함하지 않습니다 — "
            "그 돈으로 이 계좌의 주문을 낼 수 없습니다.",
            "물타기 합계는 정책의 시장별 add 상한 안에 드는 행만 더합니다.",
            "이미 걸린 지정가는 브로커가 현금을 묶고 있어 합계에서 제외했습니다.",
        ]
        if unattributed_present:
            notes.append(
                "귀속 계좌를 확정하지 못한 소요액이 있어 이 계좌 판정을 보류합니다 — "
                "그 돈은 다른 브로커로 나갈 수 있으므로, 여기 현금이 그 금액을 덮는다는 "
                "사실은 실제 실행 계좌에 대해 아무것도 증명하지 않습니다."
            )
        scopes.append(
            ScopeReconciliation(
                scope_key=f"{broker}:{currency}",
                broker=broker,
                currency=currency,
                account_ids=[row.account_id for row in rows],
                available_cash=available,
                required_averaging_adds=by_kind["averaging_add"],
                required_support_net=by_kind["support_net"],
                required_active_watches=by_kind["active_watch"],
                required_total=attributed_total,
                unattributed_same_currency=unattributed_amount,
                upper_bound_if_all_unattributed_lands_here=upper_bound,
                requirements_complete=not incomplete,
                incomplete_reasons=sorted(set(incomplete)),
                verdict=verdict,  # type: ignore[arg-type]
                shortfall=shortfall,
                notes=notes,
            )
        )

    return BuyPlanFunding(
        accounts=accounts,
        scopes=scopes,
        unattributed=unattributed,
        source_warnings=sorted(set(shared_incomplete)),
    )


def _scope_available_cash(
    rows: list[CashAccountRow], *, warnings: list[str]
) -> Decimal | None:
    """Total the scope's cash, or ``None`` if any account in it is unreadable.

    A partially-known total understates what is available and would send the
    operator to deposit money they already hold, so it is withheld instead.
    """

    if not rows:
        return None
    known = [row.available_cash for row in rows if row.available_cash is not None]
    if len(known) != len(rows):
        warnings.append(
            f"{rows[0].broker}/{rows[0].currency} 가용 현금이 일부 계좌에서 "
            "확인되지 않아 대조를 보류합니다."
        )
        return None
    return sum(known, Decimal(0))


def _scope_verdict(
    *,
    available: Decimal | None,
    attributed: Decimal,
    unattributed_present: bool,
    incomplete: bool,
) -> tuple[str, Decimal | None]:
    """Decide one scope, refusing to round any unknown down to zero.

    A shortfall is still reported when the *known* requirement already exceeds
    the cash: that deficit is proven regardless of what else is missing, and
    suppressing it would be its own kind of dishonesty. It is a lower bound,
    which the UI says out loud when the requirement side is incomplete.

    ``sufficient`` requires that nothing about this scope is unresolved. In
    particular an earlier version treated "this account could cover the whole
    unattributed pool" as proof of sufficiency; that is false, because the
    unattributed money may be destined for a *different* broker whose account
    is empty — which is the very mistake the broker scoping was introduced to
    prevent (verify-r2 B1 (a)).
    """

    if available is None:
        return "unknown", None
    if attributed > available:
        return "shortfall", attributed - available
    if incomplete:
        return "unknown", None
    if unattributed_present:
        return "unknown", None
    return "sufficient", Decimal(0)


def _cash_accounts(home: InvestHomeResponse | None) -> list[CashAccountRow]:
    if home is None:
        return []

    # A KIS account normally omits USD when it only reports KRW, but the
    # overseas-cash reader emits this stable warning when USD was queried and
    # found internally inconsistent.  Preserve that distinction so the buy
    # plan does not silently treat an unavailable USD balance as absent.
    kis_usd_unavailable = any(
        warning.source == "kis"
        and KIS_OVERSEAS_USD_BALANCE_ORDERABLE_MISMATCH in warning.message
        for warning in home.meta.warnings
    )
    rows: list[CashAccountRow] = []
    for account in home.accounts:
        included = account.accountKind in _RESERVE_ACCOUNT_KINDS
        resolved = {
            currency: _account_cash(account, currency) for currency in ("KRW", "USD")
        }
        reported_any = any(cash is not None for cash, _ in resolved.values())
        for currency, (cash, source) in resolved.items():
            explicitly_unavailable = (
                currency == "USD" and account.source == "kis" and kis_usd_unavailable
            )
            if explicitly_unavailable:
                cash, source = None, None
            # A KRW-only account legitimately has no USD line, so a missing
            # currency on an account that reported *something* is just absence.
            # An explicit KIS USD mismatch is not absence and must keep an
            # unknown funding scope visible.
            # A live account that reported nothing at all is a failed read —
            # it is published as unknown so the reserve verdict goes unknown
            # rather than quietly totalling the accounts that did answer.
            if (
                cash is None
                and (not included or reported_any)
                and not explicitly_unavailable
            ):
                continue
            rows.append(
                CashAccountRow(
                    account_id=account.accountId,
                    display_name=account.displayName,
                    source=account.source,
                    broker=_BROKER_BY_ACCOUNT_SOURCE.get(
                        str(account.source), UNATTRIBUTED
                    ),
                    currency=currency,  # type: ignore[arg-type]
                    available_cash=cash,
                    available_cash_source=source or "unavailable",
                    included_in_reserve=included,
                )
            )
    return rows


def _account_cash(account: Account, currency: str) -> tuple[Decimal | None, str | None]:
    """Prefer buying power, fall back to the cash balance.

    ``buyingPower`` is what the broker says is actually orderable (settlement
    and pending orders already netted); ``cashBalances`` is the raw balance.
    Reporting which one answered keeps the difference visible.
    """

    buying = _dec(getattr(account.buyingPower, currency.lower(), None))
    if buying is not None:
        return buying, "buyingPower"
    balance = _dec(getattr(account.cashBalances, currency.lower(), None))
    if balance is not None:
        return balance, "cashBalances"
    return None, None


def _value_sources() -> list[ValueSource]:
    return [
        ValueSource(
            field="averaging_triggers.*",
            source="InvestHomeService 보유 + config/trading_policy.yaml "
            "decision_rules['buy.support_reserve_net'].add_candidate.k_used",
            note="A(p) = C·(1 − (p/P)·(1+k))/k, 전환점 P* = P/(1+k).",
        ),
        ValueSource(
            field="averaging_triggers[].market_rank",
            source="이 보드의 표시용 정렬 — 전환점까지의 거리",
            note="정책의 same_intent_class_sort_order(지지강도·R-931·섹터 증분)는 "
            "여기서 읽지 않는 입력이 필요해 재현하지 않았습니다.",
        ),
        ValueSource(
            field="support_net.*",
            source="config/trading_policy.yaml "
            "decision_rules['buy.held_majors_support_net'] + CurrentOrdersService "
            "미체결 + WatchPanelService 활성 워치",
        ),
        ValueSource(
            field="active_buy_watches[].planned_notional",
            source="InvestmentWatchAlert.max_action (typed execution plan)",
        ),
        ValueSource(
            field="discovery_gates[].conditions[].current_value",
            source="market_rules.crypto.recovery_gate.conditions[].sources "
            "(정책이 선언한 업스트림)",
            note=f"캐시 {GATE_CACHE_TTL_SECONDS}초. 확인 불가 조건은 충족으로 세지 않습니다.",
        ),
        ValueSource(
            field="funding.accounts[].available_cash",
            source="InvestHomeService Account.buyingPower ?? Account.cashBalances",
            note="live 계좌만 리저브에 포함합니다.",
        ),
        ValueSource(
            field="funding.scopes[]",
            source="(브로커, 통화) 쌍 — 보유 account source / 워치 max_action.account_mode",
            note="다른 브로커의 같은 통화 현금은 합산하지 않습니다. 귀속을 확정하지 "
            "못한 소요액은 funding.unattributed 로 올라가며 같은 통화 판정을 보류시킵니다.",
        ),
        ValueSource(
            field="approval_context.master_gate_enabled",
            source="settings.ORDER_PROPOSALS_AUTO_APPROVE",
            note="행의 approval_lane 은 cap 두 개만 본 분류입니다 — 실제 자동제출 "
            "조건은 approval_context.unevaluated_conditions 에 나열돼 있고 "
            "이 보드는 그것들을 평가하지 않습니다.",
        ),
        ValueSource(
            field="*.approval_lane",
            source="config/trading_policy.yaml order_proposals.auto_approve.per_order_cap "
            "+ decision_rules['buy.support_reserve_net'].auto_submit_notional",
        ),
    ]
