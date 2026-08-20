"""Point-in-time evidence reconstruction that *calls the live code*.

Nothing here reimplements a gate input. Support/resistance comes from the live
``get_support_resistance_impl`` with a preloaded frame (so it never fetches),
and RSI comes from the live ``_compute_indicators``. The only substitution is
the US intraday-quote override, which is disabled because no corpus holds an
intraday price; the US run then takes the same close-based branch as KR.

🔴 Look-ahead: ``bars`` handed to this module is always sliced to
``session_date <= decision_date``. The forward window is opened only by
``scoring.score_window``, which drops every bar at or before the decision date.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pandas as pd

# The app package builds a global Settings singleton at import time. Research
# runs must never load operating credentials, so inert placeholders are pinned
# here *before* the first app import. No network or DB call is made either way.
os.environ.setdefault("ENV_FILE", "/dev/null")
os.environ.setdefault("KIS_APP_KEY", "research-offline-unused")
os.environ.setdefault("KIS_APP_SECRET", "research-offline-unused")
os.environ.setdefault("OPENDART_API_KEY", "research-offline-unused")
os.environ.setdefault("UPBIT_ACCESS_KEY", "research-offline-unused")
os.environ.setdefault("UPBIT_SECRET_KEY", "research-offline-unused")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://research:research@127.0.0.1:1/research_offline_unused",
)
os.environ.setdefault("SECRET_KEY", "ResearchOfflineOnlyPlaceholderKey0123456789")

from app.mcp_server.tooling.fundamentals import _support_resistance as _sr  # noqa: E402
from app.mcp_server.tooling.market_data_indicators import (  # noqa: E402
    _compute_indicators,
)
from app.services.buy_gate_ab_shadow.evaluate import (  # noqa: E402
    OTHER_GATE_KEYS,
    RSI_MAX,
    SUPPORT_WITHIN_PCT,
    UPSIDE_MIN_PCT,
)

RSI_WINDOW_BARS = 250
SR_WINDOW_BARS = 60


async def _no_live_price(symbol: str) -> None:  # noqa: ARG001
    """A backtest has no intraday quote. Fall back to the decision-day close."""
    return None


# Applied at import: the live US branch would otherwise open a Yahoo socket.
_sr.fetch_us_live_last_price = _no_live_price


class _Loop:
    """One event loop reused for every synchronous call into the async impl."""

    _loop: asyncio.AbstractEventLoop | None = None

    @classmethod
    def run(cls, coro: Any) -> Any:
        if cls._loop is None:
            cls._loop = asyncio.new_event_loop()
        return cls._loop.run_until_complete(coro)


@dataclass(frozen=True, slots=True)
class ReconstructionFailure:
    symbol: str
    reason: str


def _sr_market_arg(market: str) -> str:
    if market == "kr":
        return "KR"
    if market == "us":
        return "US"
    return "crypto"


def _sr_symbol_arg(market: str, symbol: str) -> str:
    # Upbit corpus symbols are already KRW-XXX; binance USDT pairs are not a
    # shape the live resolver knows, so they are routed through the crypto
    # branch under a KRW- prefix. Only the market *branch* matters here: the
    # branch decides whether a live quote is fetched, and the fetch is already
    # disabled above. No price or level is altered by the label.
    if market.startswith("crypto") and not symbol.startswith("KRW-"):
        return f"KRW-{symbol}"
    return symbol


def build_evidence(
    *,
    symbol: str,
    market: str,
    bars: pd.DataFrame,
) -> dict[str, Any] | ReconstructionFailure:
    """Return one ``CandidateEvidence`` mapping for a single decision session.

    ``bars`` must already end at the decision session and hold at least
    ``RSI_WINDOW_BARS`` rows.
    """

    if len(bars) < RSI_WINDOW_BARS:
        return ReconstructionFailure(symbol, "insufficient_history")

    window_250 = bars.iloc[-RSI_WINDOW_BARS:]
    window_60 = window_250.iloc[-SR_WINDOW_BARS:]

    indicator = _compute_indicators(window_250, ["rsi"])
    rsi_value = (indicator.get("rsi") or {}).get("14")
    if rsi_value is None:
        return ReconstructionFailure(symbol, "rsi_unavailable")

    if Decimal(str(rsi_value)) >= RSI_MAX:
        # Pre-registered shortcut: the shared RSI gate already rejects this row
        # for both arms, so the 60-bar support reconstruction cannot change any
        # cohort. Support fields are recorded as absent, not as a pass.
        return _evidence(
            symbol=symbol,
            market=market,
            current_price=float(window_250["close"].iloc[-1]),
            rsi_value=rsi_value,
            chosen={
                "price": None,
                "distance_pct": None,
                "strength": "not_computed",
                "sources": [],
                "family_count": 0,
            },
            support_resistance_computed=False,
        )

    try:
        sr = _Loop.run(
            _sr.get_support_resistance_impl(
                _sr_symbol_arg(market, symbol),
                market=_sr_market_arg(market),
                preloaded_df=window_60,
            )
        )
    except ValueError:
        # The live resolver rejects symbol shapes it does not route (dotted US
        # classes, for instance). Those rows are dropped and counted, never
        # guessed into a different symbol.
        return ReconstructionFailure(symbol, "symbol_not_resolvable_by_live_router")
    if not isinstance(sr, dict) or "error" in sr:
        return ReconstructionFailure(symbol, "support_resistance_error")

    current_price = float(sr["current_price"])
    if current_price <= 0:
        return ReconstructionFailure(symbol, "non_positive_price")

    chosen = _select_support(sr.get("supports") or [], current_price=current_price)
    return _evidence(
        symbol=symbol,
        market=market,
        current_price=current_price,
        rsi_value=rsi_value,
        chosen=chosen,
        support_resistance_computed=True,
    )


def _evidence(
    *,
    symbol: str,
    market: str,
    current_price: float,
    rsi_value: float,
    chosen: dict[str, Any],
    support_resistance_computed: bool,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "market": "kr" if market in ("kr", "crypto_upbit_krw") else "us",
        "_reported_market": market,
        "current_price": Decimal(str(current_price)),
        "support_strength": chosen["strength"],
        "support_distance_pct": (
            None
            if chosen["distance_pct"] is None
            else Decimal(str(chosen["distance_pct"]))
        ),
        "rsi": Decimal(str(rsi_value)),
        # 🔴 neutralised shared gates — identical for A and B (see addendum)
        "honest_upside_pct": UPSIDE_MIN_PCT,
        "other_gate_bits": dict.fromkeys(OTHER_GATE_KEYS, True),
        # annex-only, never a gate here
        "_support_family_count": chosen["family_count"],
        "_support_price": chosen["price"],
        "_support_sources": chosen["sources"],
        "_support_resistance_computed": support_resistance_computed,
    }


_STRENGTH_RANK = {"weak": 0, "moderate": 1, "strong": 2}


def _select_support(supports: list[Any], *, current_price: float) -> dict[str, Any]:
    """Pick the one support level both arms will see.

    Pre-registered rule: strongest level within ``SUPPORT_WITHIN_PCT`` below the
    price, ties broken by nearest. The rule may not consult either arm's
    strength threshold — both variants must read one identical record.
    """

    below: list[dict[str, Any]] = []
    for level in supports:
        if not isinstance(level, dict):
            continue
        price = level.get("price")
        if price is None or float(price) <= 0 or float(price) >= current_price:
            continue
        distance_pct = (current_price - float(price)) / current_price * 100
        sources = level.get("sources") or []
        below.append(
            {
                "price": float(price),
                "distance_pct": distance_pct,
                "strength": str(level.get("strength") or "").lower(),
                "sources": [str(source) for source in sources],
                "family_count": len(
                    {
                        family
                        for source in sources
                        if (family := _support_family(str(source))) is not None
                    }
                ),
            }
        )

    if not below:
        return {
            "price": None,
            "distance_pct": None,
            "strength": "none",
            "sources": [],
            "family_count": 0,
        }

    within = [
        level for level in below if level["distance_pct"] <= float(SUPPORT_WITHIN_PCT)
    ]
    if within:
        return min(
            within,
            key=lambda level: (
                -_STRENGTH_RANK.get(level["strength"], -1),
                level["distance_pct"],
            ),
        )
    # Nothing inside the band: report the nearest one so the shared distance
    # gate fails on a real number instead of on a missing field.
    return min(below, key=lambda level: level["distance_pct"])


_SUPPORT_FAMILY_ALIASES = (("fib_", "fib"), ("bb_lower", "bb_lower"), ("volume_", "volume_profile"))


def _support_family(source: str) -> str | None:
    normalized = source.strip().lower()
    for prefix, family in _SUPPORT_FAMILY_ALIASES:
        if normalized.startswith(prefix):
            return family
    return None
