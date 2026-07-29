"""ROB-993 — plugin strategy interface for the Binance Demo strategy loop.

``StrategyPlugin.evaluate(bars_4h_multi_symbol) -> Signal | None`` is the
entire contract. The loop calls it once per newly-closed 4h bar (H1
semantics — a symbol with a gap simply contributes no bar for that
bucket) and reacts only to a non-``None`` result.

The S3 signal-engine adapter (``research/nautilus_scalping/rob974_h3_s3.py``,
ROB-980) is a separate, later commit — deliberately not implemented here.
``NullStrategy`` is the safe default (always ``None``); it lets the loop's
infra (bar aggregation, kill switch, execution wiring) be exercised and
smoke-tested before any real strategy is plugged in.

ROB-1145 adds ``LastBarDirectionStrategy`` — the first plugin that can
actually emit a signal. It is an **infrastructure proof, not an alpha**
(see its docstring); ``NullStrategy`` remains the CLI default and is
selected by ``--strategy null``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol

from app.services.brokers.binance.futures_demo.sizing import (
    FUTURES_DEMO_EXCLUDED_SYMBOLS,
    FUTURES_DEMO_FALLBACK_SYMBOLS,
)
from research.nautilus_scalping.rob974_features import Bar4h

Side = Literal["BUY", "SELL"]


@dataclass(frozen=True)
class Signal:
    """A single accepted entry decision, emitted at a 4h bar close.

    ``decision_ts`` is the triggering bar's ``close_ts`` (epoch ms, UTC,
    4h-aligned) — the same timestamp semantics H1/H2/S3 use, so a plugin's
    signal is directly comparable across the offline and live paths.
    """

    symbol: str
    side: Side
    decision_ts: int
    strategy_id: str
    reason: str
    sl_price: Decimal | None = None
    tp_price: Decimal | None = None
    confidence: float | None = None


class StrategyPlugin(Protocol):
    """Strategy interface the loop evaluates once per 4h bar close.

    ``bars_4h_multi_symbol`` maps symbol -> the complete-only ``Bar4h``
    history built by ``bars.build_complete_4h`` (H1 semantics — never
    forward-filled). Implementations that need the S3/H1 synchronized
    common-feature plane should call
    ``research.nautilus_scalping.rob974_features.compute_common_features``
    themselves; the loop only guarantees per-symbol complete-only bars.
    """

    strategy_id: str

    def evaluate(
        self,
        bars_4h_multi_symbol: Mapping[str, tuple[Bar4h, ...]],
        *,
        decision_ts: int,
    ) -> Signal | None: ...


@dataclass(frozen=True)
class NullStrategy:
    """Always returns ``None`` — the default, safe plugin.

    Lets the loop's bar aggregation / kill switch / execution wiring run
    (and be smoke-tested) with zero chance of placing an order until a
    real strategy is plugged in.
    """

    strategy_id: str = "null"

    def evaluate(
        self,
        bars_4h_multi_symbol: Mapping[str, tuple[Bar4h, ...]],
        *,
        decision_ts: int,
    ) -> Signal | None:
        return None


# ROB-1145: the symbol priority the first non-null plugin walks. Ordered
# view of ``FUTURES_DEMO_FALLBACK_SYMBOLS`` (a frozenset, so it carries no
# order) with the lane's documented primary symbol first. This is NOT a new
# allowlist surface: ``LastBarDirectionStrategy`` intersects it with the
# canonical allowlist on every call, and ``orchestrator.run_tick``
# independently re-runs ``assert_symbol_allowed`` on the emitted signal.
LAST_BAR_DIRECTION_SYMBOL_PRIORITY: tuple[str, ...] = ("XRPUSDT", "DOGEUSDT", "SOLUSDT")


@dataclass(frozen=True)
class LastBarDirectionStrategy:
    """ROB-1145 — the loop's first signal-emitting plugin. **Not an alpha.**

    Deliberately the simplest deterministic rule that consumes the bar
    plane the loop hands it, so that "does the loop place an order on its
    own?" can be answered without entangling it with signal research:

      * pick the target symbol as the first entry of
        ``symbol_priority`` that (a) is in the canonical futures-demo
        allowlist and (b) has a complete 4h bar whose ``close_ts`` equals
        ``decision_ts``;
      * ``close > open`` -> ``BUY``, ``close < open`` -> ``SELL``,
        ``close == open`` -> no signal.

    No lookback beyond the single decision bar, no parameters, no
    thresholds, no state — the same bar always produces the same signal.
    It makes **no claim of expected return**; the ROB-316 lesson (an
    OOS gross-negative micro-breakout signal) is exactly why this is
    labelled infrastructure proof rather than a validated strategy, and
    why it is not the CLI default (``NullStrategy`` still is).

    Every hard lane invariant (leg notional ``[6, 10]`` USDT, one
    concurrent position, two consecutive stop-losses, 1x leverage,
    reduceOnly close, ``demo-fapi.binance.com``) lives downstream in
    ``sizing``/``kill_switch``/``execution`` and is untouched by this
    plugin — a plugin can only propose a symbol and a side.
    """

    strategy_id: str = "last-bar-direction"
    symbol_priority: tuple[str, ...] = LAST_BAR_DIRECTION_SYMBOL_PRIORITY

    def evaluate(
        self,
        bars_4h_multi_symbol: Mapping[str, tuple[Bar4h, ...]],
        *,
        decision_ts: int,
    ) -> Signal | None:
        for symbol in self.symbol_priority:
            candidate = symbol.upper()
            if candidate in FUTURES_DEMO_EXCLUDED_SYMBOLS:
                continue
            if candidate not in FUTURES_DEMO_FALLBACK_SYMBOLS:
                continue
            bars = bars_4h_multi_symbol.get(candidate)
            if not bars:
                continue
            bar = bars[-1]
            if bar.close_ts != decision_ts:
                # H1 semantics: absence is NO_SIGNAL. Never reach further
                # back for a stale bar to manufacture a decision.
                continue
            if bar.close == bar.open:
                return None
            side: Side = "BUY" if bar.close > bar.open else "SELL"
            return Signal(
                symbol=candidate,
                side=side,
                decision_ts=decision_ts,
                strategy_id=self.strategy_id,
                reason=(
                    f"last complete 4h bar direction: open={bar.open} "
                    f"close={bar.close} -> {side} (infrastructure proof, "
                    "no expected-return claim)"
                ),
            )
        return None
