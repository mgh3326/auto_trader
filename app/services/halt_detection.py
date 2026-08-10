"""ROB-1236 — trading-halt *suspicion* detector for daily OHLCV series.

Why this exists
---------------
``analyze_stock_batch`` classified ``data_state`` purely from *whether a latest
bar exists*, never from whether that bar was **alive**. On 2026-08-10 a KRX
symbol under a 인적분할 매매거래정지 (000880 한화) had eight consecutive daily
bars with ``volume == 0`` and OHLC frozen at 83,800. The tool returned
``data_state: "fresh"``, a normal-looking current price, and RSI / support /
resistance / upside all computed from those dead candles. A live session ranked
it as buy candidate #2; only a later session's manual OHLCV cross-check caught
it before a real-account proposal went out.

What this module is — and is not
--------------------------------
It answers one narrow question: *do the most recent N daily bars look inert?*
That is **suspicion, not confirmation** — hence ``halted_suspect``. This
repository has **no KRX 거래정지 master feed**: there is no halt table, no halt
service, and no ingestion CLI for one, so nothing here can assert that a symbol
is actually halted. The only evidence available is the shape of the bars, and
the naming, the payload field (``krx_halt_master: "unavailable"``) and the
consumer wording all keep that distinction visible. Do not restate a verdict
from this module as a confirmed halt.

Detection rule
--------------
A daily bar is **frozen** when either:

* its volume is known and is exactly ``0``; or
* it has no intraday range at all (``high == low == close``, and ``open ==
  close`` when open is known) **and** its close equals the prior bar's close.

The second clause deliberately requires the unchanged-vs-prior-close check: a
limit-up / limit-down lock also prints ``open == high == low == close``, but at
a price that by construction differs from the prior close. Without that check
every 상한가 잠김 day would be misread as a halt.

A symbol is ``halted_suspect`` when the run of frozen bars **ending at the most
recent bar** reaches :data:`MIN_FROZEN_SESSIONS`.

Choosing N (= 3)
----------------
``MIN_FROZEN_SESSIONS = 3`` — see the constant's own docstring for the full
both-directions error argument.

The module is pure: stdlib + optional pandas duck-typing, no DB, no network, no
broker, no clock. It is imported by both ``app/`` and ``scripts/`` consumers.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, NamedTuple

#: The ``data_state`` value consumers must emit instead of ``"fresh"`` when a
#: series looks inert. Named "suspect" on purpose — it is never a confirmation.
HALTED_SUSPECT_DATA_STATE = "halted_suspect"

#: Number of consecutive frozen sessions (ending at the newest bar) required
#: before a symbol is flagged.
#:
#: **Why 3.**
#:
#: *Too large.* The 000880 incident ran eight sessions, but the contaminated
#: buy ranking happened *mid-halt*, not on day 8 — a threshold tuned to the
#: observed run length (N=8) would have fired only on the final day and would
#: have let the actual incident through. It would also miss every short KRX
#: halt outright: 조회공시 요구, 불성실공시 지정예고, and 단일가 조치 routinely
#: last one to three sessions, which is the bulk of real halts by count.
#:
#: *Too small.* At N=1 a single zero-volume print flags the symbol, and a
#: single zero-volume print is ordinary: genuinely illiquid microcaps trade
#: nothing on some days, and a candle row whose volume simply failed to ingest
#: is indistinguishable from a real zero. N=2 still fires on a two-row
#: ingestion gap, which is a plausible batch failure mode.
#:
#: *Residual error accepted at N=3.* False negatives — a one- or two-session
#: halt is not flagged; that window still reaches consumers, and the
#: cross-check in the operating session remains the backstop for it. False
#: positives — an ultra-thin symbol with three genuinely zero-volume sessions
#: is flagged; the blast radius is bounded because the screener and the policy
#: table both already apply a turnover floor that excludes such names, and
#: because every exclusion is reported with its symbol and reason rather than
#: silently dropped.
MIN_FROZEN_SESSIONS = 3

#: Reason codes attached to a suspicion. Both may be present in one run.
REASON_ZERO_VOLUME = "zero_volume"
REASON_ZERO_VARIATION = "zero_variation"

#: Recorded on every verdict. There is no KRX halt master in this repository;
#: stating so explicitly is preferable to leaving the reader to assume one was
#: consulted.
KRX_HALT_MASTER_STATUS = "unavailable"

_SUSPICION_NOTE = (
    "halted_suspect is a suspicion derived from inert daily bars "
    "(consecutive zero-volume or zero-variation sessions), not a confirmed "
    "trading halt — no KRX halt master is available to this service. "
    "Indicators are withheld rather than estimated."
)


class HaltBar(NamedTuple):
    """One daily bar. ``open``/``volume`` are optional evidence."""

    close: Decimal
    high: Decimal | None = None
    low: Decimal | None = None
    volume: Decimal | None = None
    open: Decimal | None = None


@dataclass(frozen=True)
class HaltSuspicion:
    """Verdict for one symbol's daily series."""

    suspected: bool
    frozen_sessions: int
    reasons: tuple[str, ...]
    bars_examined: int
    min_sessions: int = MIN_FROZEN_SESSIONS

    @property
    def insufficient_bars(self) -> bool:
        """True when the series is too short to reach a verdict either way."""
        return self.bars_examined < self.min_sessions

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe evidence block for MCP responses and policy-table payloads."""
        return {
            "suspected": self.suspected,
            "frozen_sessions": self.frozen_sessions,
            "min_sessions": self.min_sessions,
            "reasons": list(self.reasons),
            "bars_examined": self.bars_examined,
            "krx_halt_master": KRX_HALT_MASTER_STATUS,
            "note": _SUSPICION_NOTE,
        }


_NOT_SUSPECT_EMPTY = HaltSuspicion(
    suspected=False, frozen_sessions=0, reasons=(), bars_examined=0
)


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        # str() first so binary floats round-trip to their printed form; a
        # frozen bar stores byte-identical values, which stay equal here.
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    # NaN would silently poison every equality check below.
    return None if parsed.is_nan() else parsed


def _is_nan(value: Any) -> bool:
    return value != value  # noqa: PLR0124 — NaN is the only self-unequal value


def _bar_frozen_reason(bar: HaltBar, prev_close: Decimal | None) -> str | None:
    """Return why ``bar`` looks inert, or ``None`` if it looks alive."""
    if bar.volume is not None and bar.volume == 0:
        return REASON_ZERO_VOLUME

    if bar.high is None or bar.low is None:
        return None
    if not (bar.high == bar.low == bar.close):
        return None
    if bar.open is not None and bar.open != bar.close:
        return None
    # A limit-up/limit-down lock is also range-less, but never at the prior
    # close. Requiring the unchanged close is what separates the two.
    if prev_close is None or bar.close != prev_close:
        return None
    return REASON_ZERO_VARIATION


def classify_bars(bars: Sequence[HaltBar]) -> HaltSuspicion:
    """Classify an **ascending** (oldest → newest) sequence of daily bars."""
    if not bars:
        return _NOT_SUSPECT_EMPTY

    frozen_sessions = 0
    reasons: list[str] = []
    for index in range(len(bars) - 1, -1, -1):
        prev_close = bars[index - 1].close if index > 0 else None
        reason = _bar_frozen_reason(bars[index], prev_close)
        if reason is None:
            break
        frozen_sessions += 1
        if reason not in reasons:
            reasons.append(reason)

    return HaltSuspicion(
        suspected=frozen_sessions >= MIN_FROZEN_SESSIONS,
        frozen_sessions=frozen_sessions,
        reasons=tuple(sorted(reasons)),
        bars_examined=len(bars),
    )


def classify_ohlcv_rows(rows: Iterable[Sequence[Any]]) -> HaltSuspicion:
    """Classify ``[close, high, low, volume?]`` rows, ascending.

    This is the ``scripts/policy_table`` raw-candle shape. ``volume`` is a
    later addition, so three-element rows from an older replay dump are still
    accepted — they simply fall back to the zero-variation rule alone.
    """
    bars: list[HaltBar] = []
    for row in rows:
        if len(row) < 3:
            continue
        close = _to_decimal(row[0])
        if close is None:
            continue
        bars.append(
            HaltBar(
                close=close,
                high=_to_decimal(row[1]),
                low=_to_decimal(row[2]),
                volume=_to_decimal(row[3]) if len(row) > 3 else None,
            )
        )
    return classify_bars(bars)


def classify_ohlcv_frame(frame: Any) -> HaltSuspicion:
    """Classify a canonical OHLCV ``DataFrame`` (ascending by date).

    Accepts any frame exposing ``empty``/``columns``/column indexing, which is
    what every ``_fetch_ohlcv_for_indicators`` path returns. Missing columns or
    an unreadable frame yield "not suspected" rather than an exception — this
    detector must never be the thing that breaks an analysis call.
    """
    if frame is None:
        return _NOT_SUSPECT_EMPTY
    try:
        if getattr(frame, "empty", True):
            return _NOT_SUSPECT_EMPTY
        columns = set(frame.columns)
        if "close" not in columns:
            return _NOT_SUSPECT_EMPTY

        def column(name: str) -> list[Any]:
            if name not in columns:
                return [None] * len(frame)
            return list(frame[name])

        closes = column("close")
        highs = column("high")
        lows = column("low")
        volumes = column("volume")
        opens = column("open")
    except Exception:  # noqa: BLE001 — never let detection break the caller
        return _NOT_SUSPECT_EMPTY

    bars: list[HaltBar] = []
    for index, raw_close in enumerate(closes):
        if raw_close is None or _is_nan(raw_close):
            continue
        close = _to_decimal(raw_close)
        if close is None:
            continue
        bars.append(
            HaltBar(
                close=close,
                high=_to_decimal(highs[index]),
                low=_to_decimal(lows[index]),
                volume=_to_decimal(volumes[index]),
                open=_to_decimal(opens[index]),
            )
        )
    return classify_bars(bars)


__all__ = [
    "HALTED_SUSPECT_DATA_STATE",
    "KRX_HALT_MASTER_STATUS",
    "MIN_FROZEN_SESSIONS",
    "REASON_ZERO_VARIATION",
    "REASON_ZERO_VOLUME",
    "HaltBar",
    "HaltSuspicion",
    "classify_bars",
    "classify_ohlcv_frame",
    "classify_ohlcv_rows",
]
