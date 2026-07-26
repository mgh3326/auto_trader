"""ROB-1059 H1 (preregistration §14.2) — quote_mode mapping priority + SYNTH_USDC
price reconstruction + USDT_PROXY basis-drift flag.

Mapping priority is EXACTLY (no ad-hoc fallback, no priority inversion):

    (a) BASEUSDC with sufficient history (first-1m-bar at/before the required
        backtest start) -> ``USDC``
    (b) both BASEUSDT and USDCUSDT exist -> ``SYNTH_USDC``
        (``P = P_USDT / P_USDCUSDT``, same-minute alignment; USDCUSDT absent
        for a minute makes that symbol's minute missing — forward-fill
        forbidden)
    (c) BASEUSDT only -> ``USDT_PROXY``
        (record, never apply, a per-date flag when |USDCUSDT - 1| > 30bp)
    (d) no direct stable pair at all -> ``NO_MAPPING`` (permanently excluded)

Sealed reference: ``alpaca-basis-data/universe_map_2026-07-25.json``. Any
recomputed mapping or first-1m-bar date that disagrees with that sealed
document must fail closed via ``validate_against_sealed_universe_map`` — this
module never revives a symbol the seal marked ``NO_MAPPING`` (HYPE).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

QuoteModeLiteral = Literal["USDC", "SYNTH_USDC", "USDT_PROXY", "NO_MAPPING"]

BASIS_DRIFT_THRESHOLD_BP = 30.0
HYPE_BASE = "HYPE"  # sealed permanently NO_MAPPING; never revivable by any fallback

__all__ = [
    "BASIS_DRIFT_THRESHOLD_BP",
    "HYPE_BASE",
    "QuoteModeLiteral",
    "SealedUniverseMapMismatchError",
    "resolve_quote_mode",
    "synth_usdc_price",
    "usdcusdt_basis_drift_flag",
    "validate_against_sealed_universe_map",
]


class SealedUniverseMapMismatchError(ValueError):
    """A recomputed mapping/first-1m-bar date disagrees with the sealed
    ``universe_map_<asof>.json`` — fail closed, never silently reconcile."""


def resolve_quote_mode(
    *,
    base_usdc_first_1m: date | None,
    base_usdt_first_1m: date | None,
    usdc_usdt_available: bool,
    required_backtest_start: date,
) -> QuoteModeLiteral:
    """Exactly the §14.2 priority order — no reordering, no ad-hoc fallback.

    ``base_usdc_first_1m``/``base_usdt_first_1m`` are the Binance first-1m-bar
    dates for ``{BASE}USDC``/``{BASE}USDT`` (``None`` if that pair never
    existed). "Sufficient history" for (a) means the USDC pair's own history
    already covers the required backtest start; a late-listed USDC pair (e.g.
    listed in 2024 when the required start is 2024-06-01 but the pair opened
    2024-09) is NOT sufficient and falls through to (b)/(c).
    """
    if base_usdc_first_1m is not None and base_usdc_first_1m <= required_backtest_start:
        return "USDC"
    # SPEC NOTE (§14.2 rule ③'s Korean wording is "BASEUSDT만" = "BASEUSDT
    # ONLY", and Linear AC#7 explicitly names BAT/YFI -- the only sealed bases
    # with NO native BASEUSDC pair at all -- as the USDT_PROXY set): rule ③
    # ("BASEUSDT only") therefore reads as "no BASEUSDC pair exists at any
    # date", not merely "insufficient". A native BASEUSDC pair existing AT ALL
    # (even too late for required_backtest_start, e.g. AAVE/GRT/ONDO/SUSHI/
    # TRUMP/XTZ/SKY/POL/RENDER) selects SYNTH_USDC over USDT_PROXY; only a base
    # with NO BASEUSDC pair ever (BAT/YFI) falls to (c)/USDT_PROXY. This is the
    # exact partition AC#7 names, so it is applied directly rather than as an
    # inference from the sealed data.
    if (
        base_usdc_first_1m is not None
        and base_usdt_first_1m is not None
        and usdc_usdt_available
    ):
        return "SYNTH_USDC"
    if base_usdt_first_1m is not None:
        return "USDT_PROXY"
    if base_usdc_first_1m is not None:
        # A late (insufficient) native BASEUSDC pair with NO BASEUSDT pair
        # ever having existed: neither SYNTH_USDC nor USDT_PROXY is
        # reconstructible (both require a BASEUSDT leg to divide/proxy from).
        # Per rule ④, NO_MAPPING is reserved for "no direct stable pair AT
        # ALL" -- a (late) native BASEUSDC pair IS a direct stable pair, so
        # this base must not be permanently excluded; fall back to USDC,
        # starting from whatever date it actually began. (No sealed base hits
        # this branch today -- every sealed base with a BASEUSDC pair also has
        # a BASEUSDT pair -- but a future universe refresh could.)
        return "USDC"
    return "NO_MAPPING"


def synth_usdc_price(usdt_value: float, usdcusdt_price: float | None) -> float | None:
    """``V_USDC = V_USDT / P_USDCUSDT`` on same-minute alignment.

    Despite the historical name, ``usdt_value`` is not price-only: the caller
    (``quote_mode_pipeline``) applies this SAME division to both the four
    OHLC price legs AND the two USDT-denominated notional legs
    (``quote_volume``/``taker_buy_quote_volume``) of a ``{base}USDT`` row —
    dividing a quote-denominated notional by the same per-minute basis rate
    is dimensionally identical to dividing a price by it. The parameter/error
    naming previously said ``usdt_price`` unconditionally, which was simply
    wrong (and misleading in a raised exception's message) on every volume
    call.

    ``None`` (a missing USDCUSDT minute) propagates to ``None`` — the caller
    must treat that as a missing minute for this symbol, never forward-fill.
    """
    if type(usdt_value) is not float or not math.isfinite(usdt_value):
        raise TypeError("usdt_value must be a finite built-in float")
    if usdcusdt_price is None:
        return None
    if type(usdcusdt_price) is not float or not math.isfinite(usdcusdt_price):
        raise TypeError("usdcusdt_price must be a finite built-in float or None")
    if usdcusdt_price <= 0:
        raise ValueError("usdcusdt_price must be positive")
    result = usdt_value / usdcusdt_price
    if not math.isfinite(result):
        return None
    return result


def usdcusdt_basis_drift_flag(
    usdcusdt_price: float, threshold_bp: float = BASIS_DRIFT_THRESHOLD_BP
) -> bool:
    """``True`` iff ``|USDCUSDT - 1| > threshold_bp`` (default 30bp, §14.2/AC7).

    H1 only computes/records this per-date flag for ``USDT_PROXY`` symbols
    (BAT, YFI) — it never applies an exclusion; that is a downstream decision.
    """
    if type(usdcusdt_price) is not float or not math.isfinite(usdcusdt_price):
        raise TypeError("usdcusdt_price must be a finite built-in float")
    return abs(usdcusdt_price - 1.0) * 10_000.0 > threshold_bp


@dataclass(frozen=True)
class SealedPairRecord:
    base: str
    quote_mode: str
    binance_usdc_first_1m: date | None
    binance_usdt_first_1m: date | None
    excluded: bool
    ineligible_reason: str | None


def _parse_date(value: str | None) -> date | None:
    if value is None:
        return None
    return date.fromisoformat(value)


def load_sealed_universe_map(path: str | Path) -> dict[str, SealedPairRecord]:
    """Parse the sealed ``universe_map_<asof>.json`` into ``base -> record``."""
    data = json.loads(Path(path).read_text())
    out: dict[str, SealedPairRecord] = {}
    for pair in data["pairs"]:
        base = pair["base"]
        out[base] = SealedPairRecord(
            base=base,
            quote_mode=pair["quote_mode"],
            binance_usdc_first_1m=_parse_date(pair.get("binance_usdc_first_1m")),
            binance_usdt_first_1m=_parse_date(pair.get("binance_usdt_first_1m")),
            excluded=bool(pair.get("excluded", False)),
            ineligible_reason=pair.get("ineligible_reason"),
        )
    return out


def validate_against_sealed_universe_map(
    *,
    base: str,
    computed_quote_mode: str,
    computed_usdc_first_1m: date | None,
    computed_usdt_first_1m: date | None,
    sealed: dict[str, SealedPairRecord],
) -> None:
    """Fail closed if a freshly recomputed mapping disagrees with the sealed
    universe map. HYPE (sealed ``NO_MAPPING``) can never be revived by any
    recomputation disagreeing with the seal — this raises instead."""
    record = sealed.get(base)
    if record is None:
        raise SealedUniverseMapMismatchError(
            f"{base}: no sealed record to validate against"
        )
    if record.excluded:
        # stablecoin/PAXG rows carry no first-1m provenance to compare.
        return
    if base == HYPE_BASE and record.quote_mode == "NO_MAPPING":
        if computed_quote_mode != "NO_MAPPING":
            raise SealedUniverseMapMismatchError(
                f"{base}: sealed NO_MAPPING is permanent; recomputed "
                f"{computed_quote_mode!r} may never revive it"
            )
        return
    if computed_quote_mode != record.quote_mode:
        raise SealedUniverseMapMismatchError(
            f"{base}: recomputed quote_mode {computed_quote_mode!r} != sealed "
            f"{record.quote_mode!r}"
        )
    if computed_usdc_first_1m != record.binance_usdc_first_1m:
        raise SealedUniverseMapMismatchError(
            f"{base}: recomputed binance_usdc_first_1m {computed_usdc_first_1m} != "
            f"sealed {record.binance_usdc_first_1m}"
        )
    if computed_usdt_first_1m != record.binance_usdt_first_1m:
        raise SealedUniverseMapMismatchError(
            f"{base}: recomputed binance_usdt_first_1m {computed_usdt_first_1m} != "
            f"sealed {record.binance_usdt_first_1m}"
        )
