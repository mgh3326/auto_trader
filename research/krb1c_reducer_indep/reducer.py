"""§6.2.1 C_stress_cap deterministic reducer — independent reimplementation.

Exact rational throughout: every quantity is ``int`` or ``fractions.Fraction``.
``float``, ``decimal.Decimal`` and ``math`` are never used or imported.

Clause map
----------
§1.4  stress components = buy fee + sell fee + sell taxes + 1 entry tick +
      1 exit tick, additive shortfall; no second-order fee on the synthetic
      move.
§2.9  rate_e12 = cost per 1 KRW × 1e12, non-negative integer.
§3    RATE_SCALE = 1e12. B_m / S_m / A_m are each *independently* the maximum
      over the market's effective records (no averaging, no trading-day
      weighting, no same-record assumption). No cross-market averaging/copy.
§4.8  rho_entry = tick(P)/P ; rho_exit = max over X_m(P) of tick(Q)/Q,
      witness = least Q attaining the max.
§5    entry_multiplier   = 1 + b_m + rho_entry(P)
      exit_multiplier_cap = 1 - s_m - tau_m - rho_exit(P)   (> 0 required)
      c_m(P) = entry_multiplier / exit_multiplier_cap - 1
      (the additive approximation b+s+tau+2*tick/P is explicitly non-binding).
§6    C_raw_m = max over P in E_m of c_m(P), witness = least P at a tie.
      C_raw   = max(KOSPI, KOSDAQ), witness market = KOSPI at a tie.
      C_stress_cap_bp = ceil(10_000 * C_raw) = (10_000*A + D - 1) // D
      for C_raw = A/D in lowest terms; cap = bp / 10_000. Never nearest.
§6.8  T_i = tick_ceil(L * (1 + cap)), exact rational product then tick_ceil.
§6.9  For every market and every P: T(P)*(1 - s - tau - tick(T)/T)
      >= P*(1 + b + tick(P)/P). One false row => P0-2 FAIL.
§8.1  fail-closed conditions surfaced as ``ReducerFailClosed``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .tick import PRICE_MAX, PRICE_MIN, TickTable

# §3 — RATE_SCALE = 1e12, written as an exact integer power.
RATE_SCALE: int = 10**12

# §6 — basis-point denominator.
BP_SCALE: int = 10_000

# §6 — witness market at a tie between the two markets.
MARKET_TIE_WINNER: str = "KOSPI"

ONE = Fraction(1)


class ReducerFailClosed(Exception):
    """P0-2 FAIL / NOT_DISCRIMINABLE (§8.1). Carries the clause reference."""

    def __init__(self, clause: str, message: str) -> None:
        super().__init__(f"[{clause}] {message}")
        self.clause = clause
        self.message = message


# --------------------------------------------------------------------------
# §2 input contract
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SellTaxComponent:
    """§2.5 sell_tax_components entry — ``component_code`` unique per record."""

    component_code: str
    rate_e12: int


@dataclass(frozen=True)
class CostRecord:
    """One §2.5 market_cost_record, reduced to the three numeric fields §2.17
    permits the reducer to consume, plus the identity fields §8.1 checks."""

    market: str
    buy_commission_rate_e12: int
    sell_commission_rate_e12: int
    sell_tax_components: Tuple[SellTaxComponent, ...]
    cost_basis: str = "REAL_TRADING_TARIFF"
    effective_from: Optional[str] = None
    effective_to: Optional[str] = None
    source_snapshot_sha256: Optional[str] = None
    probe_reconciliation_status: str = "PASS"
    mock_cost_relation: str = "DIFFERENT"
    broker_id: Optional[str] = None
    account_product_id: Optional[str] = None
    order_channel_id: Optional[str] = None

    def total_sell_tax_rate_e12(self) -> int:
        """Sum of this record's sell tax components (§ⓒ decomposition)."""
        return sum(c.rate_e12 for c in self.sell_tax_components)


@dataclass(frozen=True)
class MarketCostInput:
    """§3 per-market reduction: B_m, S_m, A_m as independent period maxima."""

    market: str
    buy_rate_e12: int
    sell_rate_e12: int
    sell_tax_rate_e12: int

    @property
    def b(self) -> Fraction:
        return Fraction(self.buy_rate_e12, RATE_SCALE)

    @property
    def s(self) -> Fraction:
        return Fraction(self.sell_rate_e12, RATE_SCALE)

    @property
    def tau(self) -> Fraction:
        return Fraction(self.sell_tax_rate_e12, RATE_SCALE)

    @property
    def a(self) -> Fraction:
        """a_m = s_m + tau_m (§3)."""
        return self.s + self.tau


def reduce_records(market: str, records: Sequence[CostRecord]) -> MarketCostInput:
    """§3 — independent period maxima of B_m, S_m, A_m over effective records.

    Deliberately three separate ``max`` calls: entry and exit may straddle a
    rate-change date, so the three maxima need not come from one record.
    """
    if not records:
        raise ReducerFailClosed("8.1(a)", f"no cost record for market {market}")

    for rec in records:
        if rec.market != market:
            raise ReducerFailClosed(
                "8.1(d)", f"record market {rec.market!r} != {market!r}"
            )
        if rec.cost_basis != "REAL_TRADING_TARIFF":
            raise ReducerFailClosed(
                "2.5",
                f"{market}: cost_basis must be REAL_TRADING_TARIFF, "
                f"got {rec.cost_basis!r} (mock 표시 요율 사용 금지)",
            )
        if rec.probe_reconciliation_status != "PASS":
            raise ReducerFailClosed(
                "8.1(i)",
                f"{market}: probe_reconciliation_status="
                f"{rec.probe_reconciliation_status!r}, PASS required",
            )
        if not rec.source_snapshot_sha256:
            raise ReducerFailClosed(
                "8.1(c)", f"{market}: source_snapshot_sha256 missing"
            )
        _require_rate_e12(rec.buy_commission_rate_e12, f"{market} buy")
        _require_rate_e12(rec.sell_commission_rate_e12, f"{market} sell")
        if not rec.sell_tax_components:
            raise ReducerFailClosed(
                "8.1(g)", f"{market}: sell_tax_components empty"
            )
        seen: set = set()
        for comp in rec.sell_tax_components:
            if comp.component_code in seen:
                raise ReducerFailClosed(
                    "8.1(g)",
                    f"{market}: duplicate component_code {comp.component_code!r}",
                )
            seen.add(comp.component_code)
            _require_rate_e12(comp.rate_e12, f"{market} {comp.component_code}")

    return MarketCostInput(
        market=market,
        buy_rate_e12=max(r.buy_commission_rate_e12 for r in records),
        sell_rate_e12=max(r.sell_commission_rate_e12 for r in records),
        sell_tax_rate_e12=max(r.total_sell_tax_rate_e12() for r in records),
    )


def _require_rate_e12(value: object, label: str) -> None:
    """§2.9 / §8.1(e) — rate_e12 must be a non-negative integer."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReducerFailClosed(
            "8.1(e)", f"{label}: rate_e12 must be an integer, got {value!r}"
        )
    if value < 0:
        raise ReducerFailClosed(
            "8.1(e)", f"{label}: rate_e12 must be >= 0, got {value}"
        )


# --------------------------------------------------------------------------
# §4.8 / §5 per-price quantities
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidateRow:
    """One row of the §7.5 ``c_stress_candidates.csv`` publication."""

    market: str
    price: int
    tick_at_price: int
    rho_entry: Fraction
    exit_witness_q: int
    tick_at_exit_witness: int
    rho_exit: Fraction
    entry_multiplier: Fraction
    exit_multiplier_cap: Fraction
    c: Fraction

    @property
    def c_bp_ceil(self) -> int:
        """Per-price ceil to bp — diagnostic; the binding ceil is on C_raw."""
        return ceil_to_bp(self.c)


def rho_entry_of(table: TickTable, price: int) -> Fraction:
    """§4.8 — rho_entry = tick(P)/P, exact rational."""
    return Fraction(table.tick(price), price)


def rho_exit_of(table: TickTable, price: int) -> Tuple[Fraction, int]:
    """§4.8 — (rho_exit, witness Q). Tie broken by the least Q.

    Maximum is taken over the finite candidate set X_m(P) of §4.6, which §4.7
    proves equals the supremum over all valid Q >= P.
    """
    best: Optional[Fraction] = None
    witness = 0
    for q in table.exit_candidates(price):  # ascending, so ">" keeps least Q
        ratio = Fraction(table.tick(q), q)
        if best is None or ratio > best:
            best = ratio
            witness = q
    if best is None:  # pragma: no cover - X_m(P) always contains P
        raise ReducerFailClosed("4.6", f"empty exit candidate set at {price}")
    return best, witness


def candidate_at(
    table: TickTable, cost: MarketCostInput, price: int
) -> CandidateRow:
    """§5 — the stress break-even cost rate c_m(P) at one price."""
    rho_entry = rho_entry_of(table, price)
    rho_exit, witness = rho_exit_of(table, price)

    entry_multiplier = ONE + cost.b + rho_entry
    exit_multiplier_cap = ONE - cost.s - cost.tau - rho_exit

    # §5 / §8.1(k) — must be strictly positive at every P.
    if exit_multiplier_cap <= 0:
        raise ReducerFailClosed(
            "8.1(k)",
            f"{table.market} P={price}: exit_multiplier_cap="
            f"{exit_multiplier_cap} <= 0",
        )

    c = entry_multiplier / exit_multiplier_cap - ONE

    return CandidateRow(
        market=table.market,
        price=price,
        tick_at_price=table.tick(price),
        rho_entry=rho_entry,
        exit_witness_q=witness,
        tick_at_exit_witness=table.tick(witness),
        rho_exit=rho_exit,
        entry_multiplier=entry_multiplier,
        exit_multiplier_cap=exit_multiplier_cap,
        c=c,
    )


# --------------------------------------------------------------------------
# §6 worst point, market combination, rounding
# --------------------------------------------------------------------------


def ceil_to_bp(value: Fraction) -> int:
    """§6 — ceil(10_000 * value) via ``(10_000*A + D - 1) // D``, exact.

    Exactly on a bp boundary adds nothing; strictly between boundaries rounds
    up. Nearest-rounding is forbidden.
    """
    if not isinstance(value, Fraction):  # pragma: no cover
        raise ReducerFailClosed("6", f"ceil_to_bp needs a Fraction, got {value!r}")
    a, d = value.numerator, value.denominator
    return (BP_SCALE * a + d - 1) // d


def bp_to_decimal_string(bp: int) -> str:
    """§6 canonical decimal rendering: cap = bp/10_000 as 4 decimal places.

    Pure integer string surgery — no float, no Decimal.
    """
    if isinstance(bp, bool) or not isinstance(bp, int):  # pragma: no cover
        raise ReducerFailClosed("6", f"bp must be an int, got {bp!r}")
    sign = "-" if bp < 0 else ""
    n = -bp if bp < 0 else bp
    return f"{sign}{n // BP_SCALE}.{n % BP_SCALE:04d}"


@dataclass(frozen=True)
class TargetCheckRow:
    """One §6.9 self-check row."""

    market: str
    price: int
    target: int
    lhs: Fraction
    rhs: Fraction
    passed: bool


@dataclass
class MarketResult:
    """Per-market §6 outcome plus the full candidate enumeration."""

    market: str
    cost: MarketCostInput
    candidates: List[CandidateRow]
    c_raw: Fraction
    witness_price: int
    target_checks: List[TargetCheckRow] = field(default_factory=list)

    @property
    def enumerated_count(self) -> int:
        return len(self.candidates)


@dataclass
class ReducerResult:
    """§6/§7.6 reducer output."""

    reducer_spec_id: str
    markets: Dict[str, MarketResult]
    c_raw: Fraction
    witness_market: str
    witness_price: int
    c_stress_cap_bp: int
    c_stress_cap: Fraction
    c_stress_cap_decimal: str
    all_target_checks_passed: bool
    target_check_failures: List[TargetCheckRow] = field(default_factory=list)

    @property
    def enumerated_count(self) -> int:
        return sum(m.enumerated_count for m in self.markets.values())

    @property
    def target_check_count(self) -> int:
        return sum(len(m.target_checks) for m in self.markets.values())


def reduce_market(
    table: TickTable,
    cost: MarketCostInput,
    price_min: int = PRICE_MIN,
    price_max: int = PRICE_MAX,
) -> MarketResult:
    """§6 — C_raw_m = max over the full enumeration of E_m; least P at a tie."""
    if table.market != cost.market:
        raise ReducerFailClosed(
            "3", f"tick table {table.market!r} vs cost {cost.market!r} mismatch"
        )

    prices = table.valid_prices(price_min, price_max)
    if not prices:
        raise ReducerFailClosed(
            "4.5", f"{table.market}: E_m is empty on [{price_min},{price_max}]"
        )

    candidates: List[CandidateRow] = []
    best: Optional[Fraction] = None
    witness = 0
    for price in prices:
        row = candidate_at(table, cost, price)
        candidates.append(row)
        # ascending prices + strict ">" ⇒ the least P wins any tie (§6)
        if best is None or row.c > best:
            best = row.c
            witness = price

    assert best is not None
    return MarketResult(
        market=table.market,
        cost=cost,
        candidates=candidates,
        c_raw=best,
        witness_price=witness,
    )


def run_target_checks(
    table: TickTable,
    cost: MarketCostInput,
    cap: Fraction,
    prices: Sequence[int],
) -> List[TargetCheckRow]:
    """§6.8 + §6.9 — build T(P) and verify the break-even inequality.

    ``T = tick_ceil(P * (1 + cap))`` with the product kept as an exact
    rational; the inequality compared is exactly the one in §6.9,

        T * (1 - s - tau - tick(T)/T)  >=  P * (1 + b + tick(P)/P)

    with both sides exact Fractions.
    """
    rows: List[TargetCheckRow] = []
    for price in prices:
        target = table.tick_ceil(Fraction(price) * (ONE + cap))
        lhs = target * (
            ONE - cost.s - cost.tau - Fraction(table.tick(target), target)
        )
        rhs = price * (ONE + cost.b + Fraction(table.tick(price), price))
        rows.append(
            TargetCheckRow(
                market=table.market,
                price=price,
                target=target,
                lhs=lhs,
                rhs=rhs,
                passed=lhs >= rhs,
            )
        )
    return rows


def reduce_c_stress_cap(
    tables: Mapping[str, TickTable],
    costs: Mapping[str, MarketCostInput],
    price_min: int = PRICE_MIN,
    price_max: int = PRICE_MAX,
    reducer_spec_id: str = "KRB1C-CSTRESS-REDUCER-v1",
) -> ReducerResult:
    """Full §6.2.1 reduction over every configured market.

    Raises ``ReducerFailClosed`` on any §8.1 condition, including a §6.9
    target-check failure — the clause forbids sealing on even one false row.
    """
    if set(tables) != set(costs):
        raise ReducerFailClosed(
            "8.1(a)",
            f"market sets differ: tables={sorted(tables)} costs={sorted(costs)}",
        )
    if not tables:
        raise ReducerFailClosed("8.1(a)", "no markets supplied")

    results: Dict[str, MarketResult] = {}
    for market in sorted(tables):
        results[market] = reduce_market(
            tables[market], costs[market], price_min, price_max
        )

    # §6 — C_raw = max(KOSPI, KOSDAQ); at a tie the witness market is KOSPI
    # (the value is unaffected, only the recorded witness).
    c_raw: Optional[Fraction] = None
    witness_market = ""
    for market in sorted(results):
        candidate = results[market].c_raw
        if c_raw is None or candidate > c_raw:
            c_raw, witness_market = candidate, market
        elif candidate == c_raw and market == MARKET_TIE_WINNER:
            witness_market = market
    assert c_raw is not None

    if c_raw < 0:
        raise ReducerFailClosed("1.1", f"C_raw must be non-negative, got {c_raw}")

    bp = ceil_to_bp(c_raw)
    cap = Fraction(bp, BP_SCALE)

    failures: List[TargetCheckRow] = []
    for market in sorted(results):
        res = results[market]
        res.target_checks = run_target_checks(
            tables[market],
            costs[market],
            cap,
            [row.price for row in res.candidates],
        )
        failures.extend(row for row in res.target_checks if not row.passed)

    result = ReducerResult(
        reducer_spec_id=reducer_spec_id,
        markets=results,
        c_raw=c_raw,
        witness_market=witness_market,
        witness_price=results[witness_market].witness_price,
        c_stress_cap_bp=bp,
        c_stress_cap=cap,
        c_stress_cap_decimal=bp_to_decimal_string(bp),
        all_target_checks_passed=not failures,
        target_check_failures=failures,
    )

    if failures:
        first = failures[0]
        raise ReducerFailClosed(
            "6.9",
            f"{len(failures)} target check(s) failed; first: "
            f"{first.market} P={first.price} T={first.target} "
            f"lhs={first.lhs} < rhs={first.rhs}",
        )

    return result
