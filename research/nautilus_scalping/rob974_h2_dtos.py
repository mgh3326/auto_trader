"""ROB-979 (H2, ROB-974 R2) CP1 -- immutable DTO + timestamp/type authority (pure, stdlib).

This module owns the H2 side of the frozen boundary described in the ROB-979
worker brief: H1 (ROB-978, unmerged at CP1-CP5 time) is a SEMANTIC contract
only here -- ``rob974_h2_ingress.py`` normalizes duck-typed H1-shaped input
(attribute access, never ``isinstance`` against a concrete H1 class) into the
DTOs defined here. CP6 (post-merge) integrates the real H1 module without
changing any of these types (AC: "H2 core is not coupled to H1 concrete class
identity").

ultrathink decisions (frozen for CP1-CP5; revisit only if orch authority
changes):

  * Exact-type authority (ROB-979 AC4) uses ``type(x) is int`` / ``type(x) is
    float`` -- NEVER ``isinstance`` -- because ``isinstance(True, int)`` is
    True (bool subclasses int) and ``isinstance`` would also silently accept
    a float subclass. ``type(x) is T`` is the only check that rejects bool,
    Decimal, and subclasses uniformly. This mirrors the frozen
    ``research_contracts.canonical_hash.encode_canonical`` convention (bool is
    checked/tagged separately from int there for the same reason).
  * S4 is ONE two-leg record (``S4PairSignalIntent``/``S4PairTrade``/
    ``S4NoTradeRecord``), never two single-leg records glued together (AC1).
    Both legs' prices/sides/weights live on the SAME frozen dataclass instance
    so there is no representable state where leg A exists without leg B.
  * ``S4PairTrade`` bakes the historical-null execution posture directly into
    its constructor invariants (AC24 in the H2 doc / this brief's CP3
    section): ``order_id_a``/``order_id_b`` must be ``None`` and
    ``demo_eligible`` must be ``False`` for EVERY historical S4 row, PASS or
    not -- there is no code path that can construct a "ready for demo" S4
    trade. This is enforced at the DTO layer (not just by engine discipline)
    so no future caller can accidentally fabricate demo-readiness by
    constructing the dataclass directly.
  * ``volatility_percentile`` is a real H3-supplied float for S3 (entry-time
    ``percentile_30d(A_i,t)``, carried through for record-keeping only -- H2
    does not recompute or gate on it) and is structurally forced to exactly
    ``(None, "not_defined_for_s4")`` for S4 (ROB-979 CP4 section / H2 doc
    AC32) via a dedicated provenance field rather than a bare ``None`` that a
    caller could confuse with "missing/forgot to supply".
  * Identity/collision-freedom (AC2, "accepted/rejected identities cannot
    ... collide across lists") is NOT a single-DTO invariant -- a lone
    ``S3Trade`` has no sibling list to collide with. It is enforced where the
    list is actually assembled: the CP2/CP3 engines' output-construction path
    asserts uniqueness of ``(symbol, signal_ts)`` / ``(pair, signal_ts)``
    identity tuples across everything they emit (trades + no-trades +
    incompletes) before returning a result. See ``rob974_h2_s3_engine.py`` /
    ``rob974_h2_s4_engine.py``.

No DB/network/app/broker/order/fill/scheduler/random/current-time imports --
pure stdlib, deterministic given its input.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

UNIVERSE: tuple[str, ...] = ("XRPUSDT", "DOGEUSDT", "SOLUSDT")

Side = Literal["long", "short"]
S3ExitReason = Literal["TP", "SL", "THESIS_EXIT", "TIMEOUT"]
S4ExitReason = Literal["TP", "SL", "MEAN_EXIT", "STALL_EXIT", "TIMEOUT"]


def _require_exact_int(value: object, name: str) -> int:
    if type(value) is not int:
        raise TypeError(
            f"{name} must be exact built-in int, got {type(value).__name__}"
        )
    return value


def _require_exact_float(value: object, name: str) -> float:
    if type(value) is not float:
        raise TypeError(
            f"{name} must be exact built-in float, got {type(value).__name__}"
        )
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite, got {value!r}")
    return value


def _require_exact_float_or_none(value: object, name: str) -> float | None:
    if value is None:
        return None
    return _require_exact_float(value, name)


def _require_symbol(value: object, name: str = "symbol") -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be str, got {type(value).__name__}")
    if value not in UNIVERSE:
        raise ValueError(f"{name}={value!r} not in frozen universe {UNIVERSE}")
    return value


def _require_side(value: object, name: str = "side") -> Side:
    if value not in ("long", "short"):
        raise ValueError(f"{name} must be 'long' or 'short', got {value!r}")
    return value  # type: ignore[return-value]


def _require_positive(value: float, name: str) -> float:
    if value <= 0.0:
        raise ValueError(f"{name} must be positive, got {value!r}")
    return value


@dataclass(frozen=True)
class MinuteBar:
    """H2-owned normalized 1m bar. Field names mirror the frozen H1 semantic
    fixture shape exactly: ``open_time/open/high/low/close`` (no volume --
    S3/S4 execution mechanics only ever need OHLC gap/touch prices)."""

    symbol: str
    open_time: int
    open: float
    high: float
    low: float
    close: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _require_symbol(self.symbol))
        object.__setattr__(
            self, "open_time", _require_exact_int(self.open_time, "open_time")
        )
        object.__setattr__(self, "open", _require_exact_float(self.open, "open"))
        object.__setattr__(self, "high", _require_exact_float(self.high, "high"))
        object.__setattr__(self, "low", _require_exact_float(self.low, "low"))
        object.__setattr__(self, "close", _require_exact_float(self.close, "close"))


@dataclass(frozen=True)
class S3CloseFeature:
    """Completed 4h close snapshot for S3 thesis-exit evaluation: exactly the
    frozen H1 fixture fields ``close_ts/symbol/close/VWAP24/M``."""

    symbol: str
    close_ts: int
    close: float
    vwap24: float
    m: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _require_symbol(self.symbol))
        object.__setattr__(
            self, "close_ts", _require_exact_int(self.close_ts, "close_ts")
        )
        object.__setattr__(self, "close", _require_exact_float(self.close, "close"))
        object.__setattr__(self, "vwap24", _require_exact_float(self.vwap24, "vwap24"))
        object.__setattr__(self, "m", _require_exact_float(self.m, "m"))


@dataclass(frozen=True)
class S4PairLegClose:
    """Synchronized completed 4h close for one S4 pair leg: ``close_ts``/
    ``symbol``/``close`` only -- MEAN/STALL exit needs only frozen-weight
    spread reconstruction, not the S3 market-breadth fields."""

    symbol: str
    close_ts: int
    close: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _require_symbol(self.symbol))
        object.__setattr__(
            self, "close_ts", _require_exact_int(self.close_ts, "close_ts")
        )
        object.__setattr__(self, "close", _require_exact_float(self.close, "close"))


@dataclass(frozen=True)
class S3SignalIntent:
    """H3-owned, H2-consumed entry candidate. ``entry_sl_distance``/
    ``entry_tp_distance`` are H3-resolved fractions (already clipped/gated by
    H3's own formulas) -- H2 never recomputes S/ER/Q/Range24 math, it only
    finite/positive-validates and walks execution mechanics against them."""

    symbol: str
    side: Side
    signal_ts: int  # == completed 4h close_ts of the signal bar (causal ts)
    entry_sl_distance: float  # d_SL fraction, > 0
    entry_tp_distance: float  # d_TP fraction, > 0
    config_id: str
    fold_id: str | None = None
    volatility_percentile: float | None = None  # H3-supplied percentile_30d(A)

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _require_symbol(self.symbol))
        object.__setattr__(self, "side", _require_side(self.side))
        object.__setattr__(
            self, "signal_ts", _require_exact_int(self.signal_ts, "signal_ts")
        )
        object.__setattr__(
            self,
            "entry_sl_distance",
            _require_positive(
                _require_exact_float(self.entry_sl_distance, "entry_sl_distance"),
                "entry_sl_distance",
            ),
        )
        object.__setattr__(
            self,
            "entry_tp_distance",
            _require_positive(
                _require_exact_float(self.entry_tp_distance, "entry_tp_distance"),
                "entry_tp_distance",
            ),
        )
        if type(self.config_id) is not str or not self.config_id:
            raise TypeError("config_id must be a non-empty str")
        object.__setattr__(
            self,
            "volatility_percentile",
            _require_exact_float_or_none(
                self.volatility_percentile, "volatility_percentile"
            ),
        )


@dataclass(frozen=True)
class S4PairSignalIntent:
    """H3-owned, H2-consumed pair-basket entry candidate. ONE record for the
    whole basket -- both legs' weights/sides/betas live here (AC1)."""

    pair: tuple[str, str]
    signal_ts: int
    side_a: Side
    side_b: Side
    weight_a: float
    weight_b: float
    beta_a: float
    beta_b: float
    mu: float
    sigma: float
    z_entry: float
    gross_notional: float  # G, entry-frozen deterministic gross basket notional
    entry_sl_distance: float
    entry_tp_distance: float
    config_id: str
    fold_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.pair) is not tuple or len(self.pair) != 2:
            raise TypeError("pair must be a 2-tuple of symbols")
        a, b = self.pair
        _require_symbol(a, "pair[0]")
        _require_symbol(b, "pair[1]")
        if a == b:
            raise ValueError(f"pair legs must be distinct symbols, got {self.pair!r}")
        object.__setattr__(
            self, "signal_ts", _require_exact_int(self.signal_ts, "signal_ts")
        )
        object.__setattr__(self, "side_a", _require_side(self.side_a, "side_a"))
        object.__setattr__(self, "side_b", _require_side(self.side_b, "side_b"))
        for name in (
            "weight_a",
            "weight_b",
            "beta_a",
            "beta_b",
            "mu",
            "sigma",
            "z_entry",
            "gross_notional",
            "entry_sl_distance",
            "entry_tp_distance",
        ):
            object.__setattr__(
                self, name, _require_exact_float(getattr(self, name), name)
            )
        _require_positive(self.weight_a, "weight_a")
        _require_positive(self.weight_b, "weight_b")
        # w_a + w_b == 1 exactly per the frozen beta-neutral construction;
        # binary-float sums of two already-computed weights can differ from
        # 1.0 by a few ULPs, so this uses a tight epsilon rather than `==`.
        if abs((self.weight_a + self.weight_b) - 1.0) > 1e-9:
            raise ValueError(
                f"weight_a+weight_b must equal 1.0, got {self.weight_a + self.weight_b!r}"
            )
        _require_positive(self.sigma, "sigma")
        _require_positive(self.gross_notional, "gross_notional")
        _require_positive(self.entry_sl_distance, "entry_sl_distance")
        _require_positive(self.entry_tp_distance, "entry_tp_distance")
        if type(self.config_id) is not str or not self.config_id:
            raise TypeError("config_id must be a non-empty str")


@dataclass(frozen=True)
class S3Trade:
    symbol: str
    side: Side
    config_id: str
    fold_id: str | None
    signal_ts: int
    entry_ts: int
    entry_price: float
    exit_ts: int
    exit_price: float
    exit_reason: S3ExitReason
    mfe_bps: float
    mae_bps: float
    gross_bps: float
    volatility_percentile: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _require_symbol(self.symbol))
        object.__setattr__(self, "side", _require_side(self.side))
        if self.exit_reason not in ("TP", "SL", "THESIS_EXIT", "TIMEOUT"):
            raise ValueError(f"invalid S3 exit_reason {self.exit_reason!r}")
        for name in ("signal_ts", "entry_ts", "exit_ts"):
            object.__setattr__(
                self, name, _require_exact_int(getattr(self, name), name)
            )
        for name in (
            "entry_price",
            "exit_price",
            "mfe_bps",
            "mae_bps",
            "gross_bps",
        ):
            object.__setattr__(
                self, name, _require_exact_float(getattr(self, name), name)
            )
        _require_positive(self.entry_price, "entry_price")
        _require_positive(self.exit_price, "exit_price")
        if self.exit_ts < self.entry_ts:
            raise ValueError("exit_ts must be >= entry_ts")
        object.__setattr__(
            self,
            "volatility_percentile",
            _require_exact_float_or_none(
                self.volatility_percentile, "volatility_percentile"
            ),
        )


@dataclass(frozen=True)
class S4PairTrade:
    """ONE two-leg pair-basket trade record (AC1) -- never representable as
    two independent single-leg records. Historical-null execution posture
    (order ids null, not demo-eligible) is a constructor invariant, not a
    convention callers must remember to honor."""

    pair: tuple[str, str]
    side_a: Side
    side_b: Side
    config_id: str
    fold_id: str | None
    signal_ts: int
    entry_ts: int
    weight_a: float
    weight_b: float
    entry_price_a: float
    entry_price_b: float
    exit_ts: int
    exit_price_a: float
    exit_price_b: float
    exit_reason: S4ExitReason
    mfe_bps: float
    mae_bps: float
    gross_bps: float
    order_id_a: str | None
    order_id_b: str | None
    pair_exec_status: str
    pair_executor_validated: bool
    demo_eligible: bool
    volatility_percentile: float | None
    volatility_percentile_provenance: str

    def __post_init__(self) -> None:
        if type(self.pair) is not tuple or len(self.pair) != 2:
            raise TypeError("pair must be a 2-tuple of symbols")
        a, b = self.pair
        _require_symbol(a, "pair[0]")
        _require_symbol(b, "pair[1]")
        if a == b:
            raise ValueError("pair legs must be distinct symbols")
        object.__setattr__(self, "side_a", _require_side(self.side_a, "side_a"))
        object.__setattr__(self, "side_b", _require_side(self.side_b, "side_b"))
        if self.exit_reason not in ("TP", "SL", "MEAN_EXIT", "STALL_EXIT", "TIMEOUT"):
            raise ValueError(f"invalid S4 exit_reason {self.exit_reason!r}")
        for name in ("signal_ts", "entry_ts", "exit_ts"):
            object.__setattr__(
                self, name, _require_exact_int(getattr(self, name), name)
            )
        for name in (
            "weight_a",
            "weight_b",
            "entry_price_a",
            "entry_price_b",
            "exit_price_a",
            "exit_price_b",
            "mfe_bps",
            "mae_bps",
            "gross_bps",
        ):
            object.__setattr__(
                self, name, _require_exact_float(getattr(self, name), name)
            )
        _require_positive(self.entry_price_a, "entry_price_a")
        _require_positive(self.entry_price_b, "entry_price_b")
        _require_positive(self.exit_price_a, "exit_price_a")
        _require_positive(self.exit_price_b, "exit_price_b")
        if self.exit_ts < self.entry_ts:
            raise ValueError("exit_ts must be >= entry_ts")
        # Historical-null execution posture (ROB-979 CP3/H2-doc AC): a
        # historical S4 row can NEVER carry a broker order id or claim
        # demo-readiness, PASS or not.
        if self.order_id_a is not None or self.order_id_b is not None:
            raise ValueError(
                "historical S4PairTrade order ids must both be None "
                f"(got {self.order_id_a!r}, {self.order_id_b!r})"
            )
        if self.demo_eligible is not False:
            raise ValueError("historical S4PairTrade.demo_eligible must be False")
        if (
            type(self.pair_executor_validated) is not bool
            or self.pair_executor_validated
        ):
            raise ValueError(
                "historical S4PairTrade.pair_executor_validated must be exact bool False"
            )
        if self.pair_exec_status != "historical_atomic_assumption":
            raise ValueError(
                "historical S4PairTrade.pair_exec_status must be "
                "'historical_atomic_assumption', got "
                f"{self.pair_exec_status!r}"
            )
        object.__setattr__(
            self,
            "volatility_percentile",
            _require_exact_float_or_none(
                self.volatility_percentile, "volatility_percentile"
            ),
        )
        if self.volatility_percentile_provenance != "not_defined_for_s4":
            raise ValueError(
                "S4PairTrade.volatility_percentile_provenance must be "
                "'not_defined_for_s4' (S4 never defines a pair-volatility "
                f"percentile), got {self.volatility_percentile_provenance!r}"
            )
        if self.volatility_percentile is not None:
            raise ValueError(
                "S4PairTrade.volatility_percentile must be exactly None "
                "(not_defined_for_s4), never a fabricated/zeroed value"
            )


@dataclass(frozen=True)
class S3NoTradeRecord:
    symbol: str
    side: Side
    config_id: str
    fold_id: str | None
    signal_ts: int
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _require_symbol(self.symbol))
        object.__setattr__(self, "side", _require_side(self.side))
        object.__setattr__(
            self, "signal_ts", _require_exact_int(self.signal_ts, "signal_ts")
        )
        if type(self.reason) is not str or not self.reason:
            raise TypeError("reason must be a non-empty str")


@dataclass(frozen=True)
class S4NoTradeRecord:
    pair: tuple[str, str]
    config_id: str
    fold_id: str | None
    signal_ts: int
    reason: str

    def __post_init__(self) -> None:
        if type(self.pair) is not tuple or len(self.pair) != 2:
            raise TypeError("pair must be a 2-tuple of symbols")
        _require_symbol(self.pair[0], "pair[0]")
        _require_symbol(self.pair[1], "pair[1]")
        object.__setattr__(
            self, "signal_ts", _require_exact_int(self.signal_ts, "signal_ts")
        )
        if type(self.reason) is not str or not self.reason:
            raise TypeError("reason must be a non-empty str")


@dataclass(frozen=True)
class S3IncompleteRecord:
    """Entered-but-unresolvable outcome (ROB-979 AC9/AC12): the position DID
    open but a data gap or fold/EOF boundary means the exit is genuinely
    unknown. Distinct from ``S3Trade`` (which always has a real exit) and
    ``S3NoTradeRecord`` (which never entered) -- never a TIMEOUT/TP/SL/
    THESIS_EXIT fabrication."""

    symbol: str
    side: Side
    config_id: str
    fold_id: str | None
    signal_ts: int
    entry_ts: int
    entry_price: float
    reason: str  # "data_gap_in_position" | "early_eof" | "missing_future_data" | "fold_horizon_rejected"

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _require_symbol(self.symbol))
        object.__setattr__(self, "side", _require_side(self.side))
        for name in ("signal_ts", "entry_ts"):
            object.__setattr__(
                self, name, _require_exact_int(getattr(self, name), name)
            )
        object.__setattr__(
            self, "entry_price", _require_exact_float(self.entry_price, "entry_price")
        )
        _require_positive(self.entry_price, "entry_price")
        allowed = (
            "data_gap_in_position",
            "early_eof",
            "missing_future_data",
            "fold_horizon_rejected",
        )
        if self.reason not in allowed:
            raise ValueError(f"S3IncompleteRecord.reason must be one of {allowed}")


@dataclass(frozen=True)
class S4IncompleteRecord:
    pair: tuple[str, str]
    side_a: Side
    side_b: Side
    config_id: str
    fold_id: str | None
    signal_ts: int
    entry_ts: int
    entry_price_a: float
    entry_price_b: float
    reason: str  # "data_gap_in_pair_position" | "early_eof" | "missing_future_data" | "fold_horizon_rejected"

    def __post_init__(self) -> None:
        if type(self.pair) is not tuple or len(self.pair) != 2:
            raise TypeError("pair must be a 2-tuple of symbols")
        _require_symbol(self.pair[0], "pair[0]")
        _require_symbol(self.pair[1], "pair[1]")
        object.__setattr__(self, "side_a", _require_side(self.side_a, "side_a"))
        object.__setattr__(self, "side_b", _require_side(self.side_b, "side_b"))
        for name in ("signal_ts", "entry_ts"):
            object.__setattr__(
                self, name, _require_exact_int(getattr(self, name), name)
            )
        for name in ("entry_price_a", "entry_price_b"):
            object.__setattr__(
                self, name, _require_exact_float(getattr(self, name), name)
            )
        _require_positive(self.entry_price_a, "entry_price_a")
        _require_positive(self.entry_price_b, "entry_price_b")
        allowed = (
            "data_gap_in_pair_position",
            "early_eof",
            "missing_future_data",
            "fold_horizon_rejected",
        )
        if self.reason not in allowed:
            raise ValueError(f"S4IncompleteRecord.reason must be one of {allowed}")


@dataclass(frozen=True)
class S3EngineResult:
    trades: tuple[S3Trade, ...]
    no_trades: tuple[S3NoTradeRecord, ...]
    incompletes: tuple[S3IncompleteRecord, ...]


@dataclass(frozen=True)
class S4EngineResult:
    trades: tuple[S4PairTrade, ...]
    no_trades: tuple[S4NoTradeRecord, ...]
    incompletes: tuple[S4IncompleteRecord, ...]
