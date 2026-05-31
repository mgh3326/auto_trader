"""Frozen Binance USD-M Futures Demo cost envelope for the 1m gross-edge spike.

These mirror the committed campaign envelope in
``research/nautilus_scalping/frozen_config.py`` (``taker_bps=4.0``,
``achievable_maker_bps=2.0``, ``economic_triviality_floor_bps=0.5``). They are
re-declared here — not imported — so this spike stays standalone and runs under a
bare ``python3`` with no repo/Nautilus dependency.

``test_spike.py::test_fee_envelope_matches_frozen_config`` pins these literals,
so an accidental edit to *this* file is caught by the tests. It does NOT import
``frozen_config`` (that would break standalone), so keeping the two in sync if
the canonical envelope ever changes is a MANUAL step — update both.

Per-leg fees; a round trip (entry + exit) pays two legs.
"""

from __future__ import annotations

TAKER_BPS_PER_LEG = 4.0
MAKER_BPS_PER_LEG = 2.0
ECONOMIC_TRIVIALITY_FLOOR_BPS = 0.5  # a gross mean below this is "no edge"

TAKER_ROUND_TRIP_BPS = 2 * TAKER_BPS_PER_LEG  # 8.0
MAKER_ROUND_TRIP_BPS = 2 * MAKER_BPS_PER_LEG  # 4.0


def net_bps(gross_bps: float, *, round_trip_bps: float) -> float:
    """Net per-trade bps after one round-trip commission (taker fills assumed)."""
    return gross_bps - round_trip_bps
