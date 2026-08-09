"""B0-X order derivation — pure, deterministic, market-agnostic.

Contract §2-1: *주문은 정책표 행에서만 파생된다 … 같은 표 + 같은 계좌상태 →
같은 주문 (byte-deterministic). LLM/세션 판단 개입 0.*

Every function here is pure. There is no clock read, no randomness, no network,
no DB, and no import of anything that could reach a broker. The only inputs are
a validated :class:`~scripts.b0x.table_source.PolicyTable`, a
:class:`~scripts.b0x.state.LaneAccountState`, and a locked
:class:`~scripts.b0x.envelope.Envelope`.

B0 rules realized here, each traceable to a table field:

  매수 L1/L2   ``A_buy_side.buy_l1.price`` / ``A_buy_side.buy_l2.price``
  물타기       ``config.averaging_k_levels`` fed through the table's own
               ``averaging_math`` (reused, not reimplemented) against **B0-X's**
               position — the table's precomputed ``averaging_math`` block is
               scoped to the operator's real account and does not describe
               B0-X's inventory.
  매도 R1/R2   ``B_sell_side.sell_r1`` / ``sell_r2``, 50/50 of held quantity.

Prices are carried two ways. ``table_price`` is the table's own quote-currency
number (what the Upbit shadow lane trades on). ``price_ratio`` is the
dimensionless level/previous_close, which is what survives a venue whose quote
currency differs (the Binance sidecar) — see
``scripts.b0x.labels.CROSS_QUOTE_RATIO_TRANSFER``.

§4 cap inputs come from :class:`~scripts.b0x.broker_truth.BrokerTruth` — this
cycle's broker read — not from any persisted counter. See that module for what
went wrong when they did not.

Scarcity tie-break: rows are processed in lexicographic symbol order, so when
the daily-new-entry or concurrent-position cap can only admit some of the
candidates, *which* ones is a fixed function of the input, not of iteration
luck. The machine has no discretion here by construction.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Any, Final

from research.kr_corpus.d3_engine.constants import DECIMAL_PRECISION, DECIMAL_ROUNDING
from scripts.b0x import broker_truth as broker_truth_module
from scripts.b0x.envelope import Envelope, assert_envelope_locked
from scripts.b0x.kill_switch import KillSwitchDecision
from scripts.b0x.state import LaneAccountState
from scripts.b0x.table_source import PolicyTable
from scripts.policy_table.core.averaging import averaging_math

#: 매도 R1/R2 50/50 — the documented B0 sell ladder. Carried as a constant so
#: the SELL_SIDE_MODEL_MISMATCH observation has an explicit number to compare
#: the operator's revealed behaviour (전량매도) against.
SELL_LADDER_FRACTIONS: tuple[tuple[str, Decimal], ...] = (
    ("sell_r1", Decimal("0.5")),
    ("sell_r2", Decimal("0.5")),
)

SELL_SIDE_LABEL = "SELL_SIDE_MODEL_MISMATCH"


class Leg:
    BUY_L1 = "buy_l1"
    BUY_L2 = "buy_l2"
    AVERAGING = "averaging"
    SELL_R1 = "sell_r1"
    SELL_R2 = "sell_r2"


class SkipReason:
    INSUFFICIENT_HISTORY = "insufficient_history"
    NOT_IN_LANE_UNIVERSE = "not_in_lane_universe"
    MISSING_LEVEL = "missing_level"
    NON_POSITIVE_LEVEL = "non_positive_level"
    KILL_SWITCH_ACTIVE = "kill_switch_active"
    #: Contract v1.5 ① 동일 심볼 재제출 금지 — reasons owned by
    #: :mod:`scripts.b0x.broker_truth`, re-exported here so a skipped-leg row
    #: and the submission-boundary refusal carry the same string.
    OWN_PENDING_ORDER_EXISTS = broker_truth_module.OWN_PENDING_ORDER_EXISTS
    OWN_PENDING_UNREADABLE = broker_truth_module.OWN_PENDING_UNREADABLE
    DAILY_NEW_ENTRY_CAP = "daily_new_entry_cap_reached"
    CONCURRENT_POSITION_CAP = "concurrent_position_cap_reached"
    SYMBOL_TOTAL_CAP = "symbol_total_notional_cap_reached"
    INSUFFICIENT_CASH = "insufficient_cash"
    AVERAGING_ALREADY_SATISFIED = "averaging_already_satisfied"
    NO_POSITION_TO_SELL = "no_position_to_sell"
    BELOW_LOSS_GUARD_FLOOR = "below_loss_guard_floor"
    SELL_LEVEL_NOT_ABOVE_CLOSE = "sell_level_not_above_close"


@dataclass(frozen=True, slots=True)
class DerivedOrder:
    """One order the rules produced. Venue-agnostic: no venue symbol, no qty."""

    sequence: int
    symbol: str
    side: str  # "buy" | "sell"
    leg: str
    #: Level / previous_close — the venue-portable form of the level.
    price_ratio: Decimal
    #: The table's own quote-currency level (shadow lane trades this directly).
    table_price: Decimal
    table_previous_close: Decimal
    #: Buys are sized by notional; sells by a fraction of the held quantity.
    notional: Decimal | None
    quantity_fraction: Decimal | None
    basis: str
    labels: tuple[str, ...]
    detail: dict[str, Any]
    order_key: str

    def canonical(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "symbol": self.symbol,
            "side": self.side,
            "leg": self.leg,
            "price_ratio": format(self.price_ratio, "f"),
            "table_price": format(self.table_price, "f"),
            "table_previous_close": format(self.table_previous_close, "f"),
            "notional": None if self.notional is None else format(self.notional, "f"),
            "quantity_fraction": (
                None
                if self.quantity_fraction is None
                else format(self.quantity_fraction, "f")
            ),
            "basis": self.basis,
            "labels": list(self.labels),
            "detail": self.detail,
            "order_key": self.order_key,
        }


@dataclass(frozen=True, slots=True)
class SkippedLeg:
    symbol: str
    leg: str
    reason: str
    detail: str

    def canonical(self) -> dict[str, str]:
        return {
            "symbol": self.symbol,
            "leg": self.leg,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class DerivationResult:
    cycle_id: str
    market: str
    lane: str
    policy_table_hash: str
    account_state_hash: str
    envelope_hash: str
    orders: tuple[DerivedOrder, ...]
    skipped: tuple[SkippedLeg, ...]
    kill_switch: KillSwitchDecision

    def canonical(self) -> dict[str, Any]:
        return {
            "cycle_id": self.cycle_id,
            "market": self.market,
            "lane": self.lane,
            "policy_table_hash": self.policy_table_hash,
            "account_state_hash": self.account_state_hash,
            "envelope_hash": self.envelope_hash,
            "orders": [order.canonical() for order in self.orders],
            "skipped": [skip.canonical() for skip in self.skipped],
            "kill_switch": self.kill_switch.canonical(),
        }

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.canonical(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")

    def derivation_hash(self) -> str:
        return f"sha256:{hashlib.sha256(self.canonical_bytes()).hexdigest()}"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 — a malformed cell is "no level", not a crash
        return None


#: Ratio serialization quantum. The raw division carries D3's 50-digit context,
#: which is deterministic but unreadable in every artifact row. 1e-12 is many
#: orders of magnitude finer than any venue tick (on a $100k BTC price it is
#: 1e-7 USD, against a 0.01 USD tick), so quantizing here cannot move a
#: tick-aligned order while keeping the record legible.
_RATIO_QUANTUM: Final[Decimal] = Decimal("0.000000000001")


def _ratio(level: Decimal, previous_close: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        context.rounding = DECIMAL_ROUNDING
        raw = level / previous_close
        return raw.quantize(_RATIO_QUANTUM, rounding=DECIMAL_ROUNDING)


def _stable_key(*, cycle_seed: str, payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha256(f"{cycle_seed}|{blob}".encode()).hexdigest()
    return digest[:16]


def compute_cycle_id(
    *, policy_table_hash: str, account_state_hash: str, envelope_hash: str, lane: str
) -> str:
    """Deterministic cycle identity — no clock, no counter, no uuid.

    Two runs over the same table and the same account state produce the same
    ``cycle_id``, which is what makes the derived ``order_key`` values (and
    hence the venue ``clientOrderId``) stable across a replay.
    """

    seed = "|".join([lane, policy_table_hash, account_state_hash, envelope_hash])
    return f"b0x-{hashlib.sha256(seed.encode()).hexdigest()[:20]}"


def envelope_hash(envelope: Envelope) -> str:
    blob = json.dumps(
        envelope.canonical(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return f"sha256:{hashlib.sha256(blob.encode()).hexdigest()}"


# ---------------------------------------------------------------------------
# derivation
# ---------------------------------------------------------------------------


def derive_orders(
    *,
    table: PolicyTable,
    state: LaneAccountState,
    envelope: Envelope,
    kill_switch: KillSwitchDecision,
    lane_universe: frozenset[str] | None = None,
    apply_envelope: bool = True,
) -> DerivationResult:
    """Derive the full order set for one cycle.

    ``lane_universe`` restricts the rows a lane may consume (the Binance
    sidecar is limited to three symbols by the account map); ``None`` means
    every row in the table. ``apply_envelope=False`` is the contract §4
    footnote for the synthetic Upbit lane — the envelope is still recorded on
    the result, it just does not bind sizing.
    """

    assert_envelope_locked(envelope)

    env_hash = envelope_hash(envelope)
    cycle_id = compute_cycle_id(
        policy_table_hash=table.policy_table_hash,
        account_state_hash=state.state_hash(),
        envelope_hash=env_hash,
        lane=state.lane,
    )

    orders: list[DerivedOrder] = []
    skipped: list[SkippedLeg] = []

    config = table.config
    k_levels = [
        k
        for k in (_decimal(raw) for raw in config.get("averaging_k_levels") or ())
        if k is not None and k > 0
    ]
    k_levels.sort()
    loss_guard_multiplier = _decimal(config.get("loss_guard_multiplier")) or Decimal(
        "1"
    )
    new_entry_notional_table = _decimal(
        (table.sizing or {}).get("new_entry_notional_krw")
    )

    # Running budgets, consumed in lexicographic symbol order.
    #
    # Contract v1.5 ①: both counters start from *this cycle's broker read*, not
    # from a carried-over file. That is the whole fix — the previous inputs came
    # from ``attributed_book.json``, which nothing ever wrote, so both started
    # at zero every cycle and a per-UTC-day cap only ever bound within one
    # cycle (crypto: 6 cycles/day × 2 = 12 effective daily entries).
    truth = state.broker_truth
    daily_new_symbols = truth.daily_new_entry_seed()
    new_entries_used = len(daily_new_symbols)
    open_position_count = truth.concurrent_position_count
    cash_remaining = state.cash

    rows = sorted(table.rows, key=lambda row: str(row.get("symbol", "")))
    sequence = 0

    def _emit(
        *,
        symbol: str,
        side: str,
        leg: str,
        level: Decimal,
        previous_close: Decimal,
        notional: Decimal | None,
        quantity_fraction: Decimal | None,
        basis: str,
        labels: tuple[str, ...],
        detail: dict[str, Any],
    ) -> DerivedOrder:
        nonlocal sequence
        ratio = _ratio(level, previous_close)
        key_payload = {
            "symbol": symbol,
            "side": side,
            "leg": leg,
            "price_ratio": format(ratio, "f"),
            "notional": None if notional is None else format(notional, "f"),
            "quantity_fraction": (
                None if quantity_fraction is None else format(quantity_fraction, "f")
            ),
        }
        order = DerivedOrder(
            sequence=sequence,
            symbol=symbol,
            side=side,
            leg=leg,
            price_ratio=ratio,
            table_price=level,
            table_previous_close=previous_close,
            notional=notional,
            quantity_fraction=quantity_fraction,
            basis=basis,
            labels=labels,
            detail=detail,
            order_key=_stable_key(cycle_seed=cycle_id, payload=key_payload),
        )
        sequence += 1
        return order

    for row in rows:
        symbol = str(row.get("symbol", ""))
        if not symbol:
            continue
        if lane_universe is not None and symbol not in lane_universe:
            # Not recorded as a skip: a lane that trades three symbols should
            # not emit thirty "not mine" rows every cycle.
            continue

        if row.get("insufficient_history"):
            skipped.append(
                SkippedLeg(
                    symbol=symbol,
                    leg="*",
                    reason=SkipReason.INSUFFICIENT_HISTORY,
                    detail=str(row.get("note") or "row marked insufficient_history"),
                )
            )
            continue

        previous_close = _decimal(row.get("previous_close"))
        if previous_close is None or previous_close <= 0:
            skipped.append(
                SkippedLeg(
                    symbol=symbol,
                    leg="*",
                    reason=SkipReason.NON_POSITIVE_LEVEL,
                    detail=f"previous_close={row.get('previous_close')!r}",
                )
            )
            continue

        if not kill_switch.allow_new_orders:
            skipped.append(
                SkippedLeg(
                    symbol=symbol,
                    leg="*",
                    reason=SkipReason.KILL_SWITCH_ACTIVE,
                    detail=", ".join(kill_switch.kill_reasons),
                )
            )
            continue

        # Contract v1.5 ① 동일 심볼 재제출 금지 — before any leg of this row is
        # considered, and side-agnostic: the literal names the symbol, not the
        # side, because the failure it closes is *stacking* (same symbol, same
        # level, one order per cycle, forever). Gated on ``apply_envelope`` like
        # every other §4 cap: the Upbit shadow lane is exempt by the §4 footnote
        # and cannot stack anyway — ``shadow.place_derived_orders`` replaces its
        # whole virtual book each cycle rather than appending to it.
        if apply_envelope:
            resubmit_block = truth.resubmit_block(symbol)
            if resubmit_block is not None:
                skipped.append(
                    SkippedLeg(
                        symbol=symbol,
                        leg="*",
                        reason=resubmit_block[0],
                        detail=resubmit_block[1],
                    )
                )
                continue

        position = state.position(symbol)
        buy_side = dict(row.get("A_buy_side") or {})
        sell_side = dict(row.get("B_sell_side") or {})

        invested = position.invested_notional if position else Decimal("0")
        symbol_headroom = (
            envelope.per_symbol_total_notional - invested if apply_envelope else None
        )

        # ---------------- BUY side ----------------
        if position is None:
            # New entry: the L1/L2 ladder for one symbol counts as ONE entry
            # against the daily cap (§4 "일일 신규 진입"), because it is one
            # decision to take a position, executed in two legs.
            entry_blocked: str | None = None
            if apply_envelope:
                # The concurrent-position cap applies unconditionally. Only the
                # *daily new entry* cap is exempt for a symbol already counted
                # today: re-entering something entered and exited earlier today
                # is not a new decision, but it is still another open position.
                if open_position_count >= envelope.max_concurrent_positions:
                    entry_blocked = SkipReason.CONCURRENT_POSITION_CAP
                elif (
                    symbol not in daily_new_symbols
                    and new_entries_used >= envelope.max_new_entries_per_utc_day
                ):
                    entry_blocked = SkipReason.DAILY_NEW_ENTRY_CAP

            if entry_blocked is not None:
                skipped.append(
                    SkippedLeg(
                        symbol=symbol,
                        leg=Leg.BUY_L1,
                        reason=entry_blocked,
                        detail=(
                            f"new_entries_used={new_entries_used}/"
                            f"{envelope.max_new_entries_per_utc_day} "
                            f"open_positions={open_position_count}/"
                            f"{envelope.max_concurrent_positions}"
                        ),
                    )
                )
            else:
                admitted_any = False
                for leg_name, cell in (
                    (Leg.BUY_L1, buy_side.get("buy_l1")),
                    (Leg.BUY_L2, buy_side.get("buy_l2")),
                ):
                    level = _decimal((cell or {}).get("price")) if cell else None
                    if level is None:
                        if leg_name == Leg.BUY_L1:
                            skipped.append(
                                SkippedLeg(
                                    symbol=symbol,
                                    leg=leg_name,
                                    reason=SkipReason.MISSING_LEVEL,
                                    detail="A_buy_side.buy_l1.price absent",
                                )
                            )
                        else:
                            skipped.append(
                                SkippedLeg(
                                    symbol=symbol,
                                    leg=leg_name,
                                    reason=SkipReason.MISSING_LEVEL,
                                    detail=(
                                        "A_buy_side.buy_l2 is null — the table found "
                                        "no qualifying second support level"
                                    ),
                                )
                            )
                        continue
                    if level <= 0:
                        skipped.append(
                            SkippedLeg(
                                symbol=symbol,
                                leg=leg_name,
                                reason=SkipReason.NON_POSITIVE_LEVEL,
                                detail=f"level={level}",
                            )
                        )
                        continue

                    notional, block = _size_buy(
                        envelope=envelope,
                        apply_envelope=apply_envelope,
                        symbol_headroom=symbol_headroom,
                        cash_remaining=cash_remaining,
                        table_new_entry_notional=new_entry_notional_table,
                        requested=None,
                    )
                    if block is not None:
                        skipped.append(
                            SkippedLeg(
                                symbol=symbol,
                                leg=leg_name,
                                reason=block[0],
                                detail=block[1],
                            )
                        )
                        continue

                    orders.append(
                        _emit(
                            symbol=symbol,
                            side="buy",
                            leg=leg_name,
                            level=level,
                            previous_close=previous_close,
                            notional=notional,
                            quantity_fraction=None,
                            basis=f"A_buy_side.{leg_name}.price",
                            labels=(),
                            detail={
                                "rule": "b0_new_entry_ladder",
                                "level_basis": str((cell or {}).get("basis") or ""),
                            },
                        )
                    )
                    admitted_any = True
                    cash_remaining -= notional or Decimal("0")
                    if symbol_headroom is not None:
                        symbol_headroom -= notional or Decimal("0")

                if admitted_any:
                    # A position is opening either way; the daily-entry counter
                    # only moves for a symbol not already in the distinct set.
                    # This is the ``∪ 당 사이클 신규 제출`` term of the v1.5 ①
                    # daily-new definition — the other two terms came from the
                    # broker read that seeded ``daily_new_symbols``.
                    open_position_count += 1
                    if symbol not in daily_new_symbols:
                        daily_new_symbols.add(symbol)
                        new_entries_used += 1
        else:
            # ---------------- 물타기 (averaging down) ----------------
            leg_name = Leg.AVERAGING
            level_cell = buy_side.get("buy_l1") or {}
            level = _decimal(level_cell.get("price"))
            chosen: dict[str, Any] | None = None
            for k in k_levels:
                math = averaging_math(
                    cost_basis=position.cost_basis,
                    average_price=position.average_price,
                    current_price=previous_close,
                    k=k,
                )
                if not math["already_satisfied"]:
                    chosen = math
                    break

            if chosen is None:
                skipped.append(
                    SkippedLeg(
                        symbol=symbol,
                        leg=leg_name,
                        reason=SkipReason.AVERAGING_ALREADY_SATISFIED,
                        detail=(
                            f"every k in {[format(k, 'f') for k in k_levels]} already "
                            f"satisfied at average_price={format(position.average_price, 'f')}"
                        ),
                    )
                )
            elif level is None or level <= 0:
                skipped.append(
                    SkippedLeg(
                        symbol=symbol,
                        leg=leg_name,
                        reason=SkipReason.MISSING_LEVEL,
                        detail="A_buy_side.buy_l1.price absent — no add level",
                    )
                )
            else:
                requested = chosen["additional_notional"]
                assert isinstance(requested, Decimal)
                notional, block = _size_buy(
                    envelope=envelope,
                    apply_envelope=apply_envelope,
                    symbol_headroom=symbol_headroom,
                    cash_remaining=cash_remaining,
                    table_new_entry_notional=new_entry_notional_table,
                    requested=requested,
                )
                if block is not None:
                    skipped.append(
                        SkippedLeg(
                            symbol=symbol,
                            leg=leg_name,
                            reason=block[0],
                            detail=block[1],
                        )
                    )
                else:
                    orders.append(
                        _emit(
                            symbol=symbol,
                            side="buy",
                            leg=leg_name,
                            level=level,
                            previous_close=previous_close,
                            notional=notional,
                            quantity_fraction=None,
                            basis="A_buy_side.buy_l1.price + config.averaging_k_levels",
                            labels=(),
                            detail={
                                "rule": "b0_averaging_down",
                                "k": format(chosen["k"], "f"),
                                "full_additional_notional": format(requested, "f"),
                                "target_average_price": format(
                                    chosen["target_average_price"], "f"
                                ),
                                "capped": bool(
                                    notional is not None and notional < requested
                                ),
                            },
                        )
                    )
                    cash_remaining -= notional or Decimal("0")

        # ---------------- SELL side (R1/R2 50/50) ----------------
        if position is None or position.quantity <= 0:
            if position is None:
                skipped.append(
                    SkippedLeg(
                        symbol=symbol,
                        leg="sell_ladder",
                        reason=SkipReason.NO_POSITION_TO_SELL,
                        detail="B0-X holds no position in this symbol",
                    )
                )
        else:
            loss_guard_floor = position.average_price * loss_guard_multiplier
            for leg_name, fraction in SELL_LADDER_FRACTIONS:
                level = _decimal(sell_side.get(leg_name))
                if level is None or level <= 0:
                    skipped.append(
                        SkippedLeg(
                            symbol=symbol,
                            leg=leg_name,
                            reason=SkipReason.MISSING_LEVEL,
                            detail=(
                                f"B_sell_side.{leg_name} absent — the table found no "
                                "qualifying resistance level at this rung"
                            ),
                        )
                    )
                    continue
                if level <= previous_close:
                    skipped.append(
                        SkippedLeg(
                            symbol=symbol,
                            leg=leg_name,
                            reason=SkipReason.SELL_LEVEL_NOT_ABOVE_CLOSE,
                            detail=f"level={level} <= previous_close={previous_close}",
                        )
                    )
                    continue
                if level < loss_guard_floor:
                    # Repo-wide fail-closed loss-sell guard (config.loss_guard_
                    # multiplier). Not a B0-X invention and not policy-table
                    # jurisdiction — it stops a rung that would book a loss.
                    skipped.append(
                        SkippedLeg(
                            symbol=symbol,
                            leg=leg_name,
                            reason=SkipReason.BELOW_LOSS_GUARD_FLOOR,
                            detail=(
                                f"level={level} < loss_guard_floor={loss_guard_floor} "
                                f"(average_price x {loss_guard_multiplier})"
                            ),
                        )
                    )
                    continue

                orders.append(
                    _emit(
                        symbol=symbol,
                        side="sell",
                        leg=leg_name,
                        level=level,
                        previous_close=previous_close,
                        notional=None,
                        quantity_fraction=fraction,
                        basis=f"B_sell_side.{leg_name}",
                        labels=(SELL_SIDE_LABEL,),
                        detail={
                            "rule": "b0_sell_ladder_50_50",
                            "position_quantity": format(position.quantity, "f"),
                            "observation": (
                                "documented B0 ladders 50/50; C2P calibration says the "
                                "operator actually sells the whole position — the gap "
                                "is the thing being observed, not a bug to fix"
                            ),
                        },
                    )
                )

    return DerivationResult(
        cycle_id=cycle_id,
        market=table.market,
        lane=state.lane,
        policy_table_hash=table.policy_table_hash,
        account_state_hash=state.state_hash(),
        envelope_hash=env_hash,
        orders=tuple(orders),
        skipped=tuple(skipped),
        kill_switch=kill_switch,
    )


def _size_buy(
    *,
    envelope: Envelope,
    apply_envelope: bool,
    symbol_headroom: Decimal | None,
    cash_remaining: Decimal,
    table_new_entry_notional: Decimal | None,
    requested: Decimal | None,
) -> tuple[Decimal | None, tuple[str, str] | None]:
    """Return ``(notional, None)`` or ``(None, (reason, detail))``.

    Envelope-bound lanes never round *up*: the size is the minimum of what the
    rule asked for, the per-order cap, and the remaining per-symbol headroom.
    """

    if not apply_envelope:
        # Synthetic lane: B0's own sizing, straight from the table.
        size = requested if requested is not None else table_new_entry_notional
        if size is None or size <= 0:
            return None, (
                SkipReason.MISSING_LEVEL,
                "no B0 sizing available (sizing.new_entry_notional_krw absent)",
            )
        if size > cash_remaining:
            return None, (
                SkipReason.INSUFFICIENT_CASH,
                f"size={format(size, 'f')} > cash_remaining={format(cash_remaining, 'f')}",
            )
        return size, None

    size = envelope.per_order_notional
    if requested is not None and requested < size:
        size = requested
    if symbol_headroom is not None and symbol_headroom < size:
        size = symbol_headroom
    if size <= 0:
        return None, (
            SkipReason.SYMBOL_TOTAL_CAP,
            (
                f"per-symbol headroom exhausted "
                f"(cap={format(envelope.per_symbol_total_notional, 'f')} "
                f"{envelope.quote_currency})"
            ),
        )
    if size > cash_remaining:
        return None, (
            SkipReason.INSUFFICIENT_CASH,
            f"size={format(size, 'f')} > cash_remaining={format(cash_remaining, 'f')}",
        )
    return size, None


__all__ = [
    "SELL_LADDER_FRACTIONS",
    "Leg",
    "SkipReason",
    "DerivedOrder",
    "SkippedLeg",
    "DerivationResult",
    "compute_cycle_id",
    "envelope_hash",
    "derive_orders",
]
