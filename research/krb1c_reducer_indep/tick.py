"""P0-1 KRX tick function — exact integer arithmetic only.

Clause basis (§6.2.1 §4):

  4.1~4.3  tick_m(x) = P0-1 확정 시장별 함수. 구간표는 중첩·공백 없고 마지막은
           open-ended. symbol↔표 매핑 불완전이면 P0-1 FAIL.
  4.5      E_m: p_0 = tick_ceil(5,000), p_{k+1} = tick_ceil(p_k + 1), up to
           400,000. 엄격 증가 필수, 샘플링·연속 최적화 금지.
  4.6      X_m(P) = {P} ∪ {tick_ceil(d_h) : 구간 하한 d_h > P}.

Sealed P0-1 table (KRX standard 주권 호가단위, identical band structure for
KOSPI and KOSDAQ; carried in the sealed verification evidence
krb1c-math-verify-2026-07-28.md §1, cross-checked against the repository's
independent copies app/mcp_server/tick_size.py and
app/services/quote_parity_shadow.py):

    [0, 2,000)          1
    [2,000, 5,000)      5
    [5,000, 20,000)     10
    [20,000, 50,000)    50
    [50,000, 200,000)   100
    [200,000, 500,000)  500
    [500,000, inf)      1,000

NOTE ON INDEPENDENCE FROM THE REPO HELPER: app/mcp_server/tick_size.py takes a
``float`` argument and rounds with ``math.floor``/``math.ceil`` on a float
quotient. It is therefore unusable under the §6 "float/Decimal 유한정밀 비교
금지" constraint and is NOT imported here. Only the *band table* is reused, as
sealed data, and the tick_ceil semantics are re-derived from the clause.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction

# §6.2.1 §1.3 — 가격 범위 5,000 ≤ P ≤ 400,000 정수 KRW.
PRICE_MIN: int = 5_000
PRICE_MAX: int = 400_000


class TickTableError(ValueError):
    """P0-1 FAIL — tick table structural defect (§8.1(j))."""


@dataclass(frozen=True)
class TickBand:
    """A half-open price band ``[lower, upper)`` with a constant tick.

    ``upper is None`` marks the open-ended final band (§4.3).
    """

    lower: int
    upper: int | None
    tick: int

    def __post_init__(self) -> None:
        if not isinstance(self.lower, int) or isinstance(self.lower, bool):
            raise TickTableError(f"band lower must be int, got {self.lower!r}")
        if not isinstance(self.tick, int) or isinstance(self.tick, bool):
            raise TickTableError(f"band tick must be int, got {self.tick!r}")
        if self.upper is not None and (
            not isinstance(self.upper, int) or isinstance(self.upper, bool)
        ):
            raise TickTableError(f"band upper must be int, got {self.upper!r}")
        if self.lower < 0:
            raise TickTableError(f"band lower must be >= 0, got {self.lower}")
        if self.tick <= 0:
            raise TickTableError(f"band tick must be > 0, got {self.tick}")
        if self.upper is not None and self.upper <= self.lower:
            raise TickTableError(f"band upper {self.upper} <= lower {self.lower}")

    def contains(self, x: int) -> bool:
        if x < self.lower:
            return False
        return self.upper is None or x < self.upper


class TickTable:
    """Per-market tick function with exact integer ``tick`` and ``tick_ceil``.

    Structural invariants enforced at construction (§4.1~4.3, §8.1(j)):

    * bands are sorted strictly ascending by ``lower``;
    * no gap and no overlap — band ``i+1`` starts exactly where band ``i`` ends;
    * the first band starts at 0 (total coverage of non-negative prices);
    * exactly the last band is open-ended;
    * every band lower bound is a multiple of that band's own tick, so that the
      valid prices inside band ``[d_h, d_{h+1})`` are exactly the multiples of
      ``t_h`` in that interval. A table violating this is rejected rather than
      silently mishandled.
    """

    __slots__ = ("market", "bands", "provenance")

    def __init__(
        self,
        market: str,
        bands: Sequence[TickBand],
        provenance: str,
    ) -> None:
        if not bands:
            raise TickTableError("tick table must have at least one band")
        bands = tuple(bands)

        if bands[0].lower != 0:
            raise TickTableError(f"first band must start at 0, got {bands[0].lower}")
        for i in range(len(bands) - 1):
            if bands[i].upper is None:
                raise TickTableError(f"open-ended band at index {i} is not last (§4.3)")
            if bands[i].upper != bands[i + 1].lower:
                raise TickTableError(
                    f"band gap/overlap between {bands[i]} and {bands[i + 1]} (§4.2)"
                )
        if bands[-1].upper is not None:
            raise TickTableError("last band must be open-ended (§4.3)")

        for band in bands:
            if band.lower % band.tick != 0:
                raise TickTableError(
                    f"band lower {band.lower} is not a multiple of its tick "
                    f"{band.tick}; valid-price lattice would be ambiguous"
                )

        self.market = market
        self.bands = bands
        self.provenance = provenance

    # ---- core functions -------------------------------------------------

    def tick(self, x: int) -> int:
        """tick_m(x) — the tick size in force at price ``x``. Exact int."""
        _require_nonneg_int(x, "tick argument")
        for band in self.bands:
            if band.contains(x):
                return band.tick
        raise TickTableError(f"no band covers price {x}")  # pragma: no cover

    def band_lower_bounds(self) -> tuple[int, ...]:
        """The ordered band lower bounds ``d_h`` (§4.6 candidate generators)."""
        return tuple(band.lower for band in self.bands)

    def is_valid_price(self, x: int) -> bool:
        """True iff ``x`` is a quotable price: a multiple of its band's tick."""
        _require_nonneg_int(x, "price")
        return x % self.tick(x) == 0

    def tick_ceil(self, x: int | Fraction) -> int:
        """Smallest valid quote price ``>= x``. Exact arithmetic, int result.

        Accepts an exact ``Fraction`` as well as an ``int`` so that §6.8's
        ``T_i = tick_ceil(L × (1 + cap))`` can be evaluated on the exact
        rational product without ever materialising a float.

        Defined structurally rather than as ``ceil(x / tick(x)) * tick(x)``:
        that shortcut can land outside the band it was computed in whenever a
        band boundary is crossed. Here each band is probed in ascending order
        and the first band that actually admits a multiple of its own tick in
        ``[max(x, d_h), d_{h+1})`` wins, which is exactly "the least valid
        price >= x".
        """
        _require_nonneg_exact(x, "tick_ceil argument")
        for band in self.bands:
            start = x if x > band.lower else band.lower
            # least multiple of band.tick that is >= start
            candidate = _ceil_int(start, band.tick) * band.tick
            if band.upper is None or candidate < band.upper:
                return candidate
        raise TickTableError(  # pragma: no cover
            f"tick_ceil undefined for {x}"
        )

    def tick_floor(self, x: int) -> int:
        """Largest valid quote price ``<= x`` (not used by §6.2.1; provided so
        boundary tests can pin the lattice from both sides)."""
        _require_nonneg_int(x, "tick_floor argument")
        for band in reversed(self.bands):
            if x < band.lower:
                continue
            candidate = (x // band.tick) * band.tick
            if candidate >= band.lower:
                return candidate
        raise TickTableError(f"tick_floor undefined for {x}")  # pragma: no cover

    # ---- §4.5 valid price set -------------------------------------------

    def valid_prices(
        self, low: int = PRICE_MIN, high: int = PRICE_MAX
    ) -> tuple[int, ...]:
        """E_m — full enumeration, exactly per §4.5.

        ``p_0 = tick_ceil(low)``; ``p_{k+1} = tick_ceil(p_k + 1)``; stop once
        the iterate exceeds ``high``. Strict increase is asserted, not assumed
        (§4.5 "엄격 증가 필수"). No sampling, no continuous optimisation.
        """
        _require_nonneg_int(low, "low")
        _require_nonneg_int(high, "high")
        if high < low:
            raise TickTableError(f"high {high} < low {low}")

        out: list[int] = []
        p = self.tick_ceil(low)
        while p <= high:
            if out and p <= out[-1]:
                raise TickTableError(  # pragma: no cover
                    f"E_m not strictly increasing at {p} after {out[-1]}"
                )
            if not self.is_valid_price(p):
                raise TickTableError(  # pragma: no cover
                    f"E_m produced non-quotable price {p}"
                )
            out.append(p)
            p = self.tick_ceil(p + 1)
        return tuple(out)

    # ---- §4.6 exit candidate set ----------------------------------------

    def exit_candidates(self, price: int) -> tuple[int, ...]:
        """X_m(P) = {P} ∪ {tick_ceil(d_h) : d_h > P}, ascending, deduplicated.

        Finite by §4.7: inside a band ``tick(Q)/Q`` is strictly decreasing in
        ``Q``, so only the first valid price of each strictly-higher band can
        beat the incumbent, and the open-ended final band's supremum is
        attained at its own first valid price.
        """
        _require_nonneg_int(price, "price")
        found = {price}
        for lower in self.band_lower_bounds():
            if lower > price:
                found.add(self.tick_ceil(lower))
        return tuple(sorted(found))


def _require_nonneg_int(x: object, label: str) -> None:
    if not isinstance(x, int) or isinstance(x, bool):
        raise TickTableError(f"{label} must be a non-bool int, got {x!r}")
    if x < 0:
        raise TickTableError(f"{label} must be >= 0, got {x}")


def _require_nonneg_exact(x: object, label: str) -> None:
    """Accept only exact non-negative numerics: ``int`` or ``Fraction``.

    A ``float`` (or ``Decimal``) argument is a hard error, not a coercion:
    §6 forbids finite-precision comparison anywhere in the reducer.
    """
    if isinstance(x, bool) or not isinstance(x, (int, Fraction)):
        raise TickTableError(
            f"{label} must be an exact int or Fraction, got {type(x).__name__}"
        )
    if x < 0:
        raise TickTableError(f"{label} must be >= 0, got {x}")


def _ceil_int(x: int | Fraction, divisor: int) -> int:
    """``ceil(x / divisor)`` as an exact integer. ``divisor > 0`` required."""
    if divisor <= 0:  # pragma: no cover - guarded at construction
        raise TickTableError(f"divisor must be > 0, got {divisor}")
    q = Fraction(x, 1) / divisor if isinstance(x, int) else x / divisor
    num, den = q.numerator, q.denominator
    return -((-num) // den)


# --- sealed P0-1 tables ---------------------------------------------------
#
# KOSPI and KOSDAQ are declared separately and independently. They are not
# aliased to one object: §3 forbids copying across markets, and a future P0-1
# revision may diverge them (KOSDAQ historically capped its tick at 100).

_KRX_STANDARD_EQUITY_BANDS = (
    (0, 2_000, 1),
    (2_000, 5_000, 5),
    (5_000, 20_000, 10),
    (20_000, 50_000, 50),
    (50_000, 200_000, 100),
    (200_000, 500_000, 500),
    (500_000, None, 1_000),
)

_KOSPI_PROVENANCE = (
    "KRX 유가증권시장 호가가격단위 (표준 주권), sealed via "
    "krb1c-math-verify-2026-07-28.md §1 "
    "sha256 7382f94125c5a6f1fbcb143470734a7e0987e9c0d04cd64d88741739b6dcc059"
)
_KOSDAQ_PROVENANCE = (
    "KRX 코스닥시장 호가단위 (표준 주권), sealed via "
    "krb1c-math-verify-2026-07-28.md §1 "
    "sha256 7382f94125c5a6f1fbcb143470734a7e0987e9c0d04cd64d88741739b6dcc059"
)

KOSPI_TICK_TABLE = TickTable(
    "KOSPI",
    [TickBand(lo, hi, t) for (lo, hi, t) in _KRX_STANDARD_EQUITY_BANDS],
    _KOSPI_PROVENANCE,
)
KOSDAQ_TICK_TABLE = TickTable(
    "KOSDAQ",
    [TickBand(lo, hi, t) for (lo, hi, t) in _KRX_STANDARD_EQUITY_BANDS],
    _KOSDAQ_PROVENANCE,
)
