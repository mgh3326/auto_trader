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
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol

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
