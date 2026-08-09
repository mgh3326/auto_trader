"""Canonical B0-X lane/account scope keys."""

from __future__ import annotations

from typing import Final

BINANCE_SPOT_DEMO_SIDECAR_SCOPE_KEY: Final[str] = "binance_spot_demo_sidecar"
UPBIT_SHADOW_SCOPE_KEY: Final[str] = "upbit_shadow"
KIS_MOCK_SCOPE_KEY: Final[str] = "kis_mock"
ALPACA_PAPER_LAB_SCOPE_KEY: Final[str] = "alpaca_paper_lab"

KNOWN_B0X_SCOPE_KEYS: Final[frozenset[str]] = frozenset(
    {
        BINANCE_SPOT_DEMO_SIDECAR_SCOPE_KEY,
        UPBIT_SHADOW_SCOPE_KEY,
        KIS_MOCK_SCOPE_KEY,
        ALPACA_PAPER_LAB_SCOPE_KEY,
    }
)

__all__ = [
    "ALPACA_PAPER_LAB_SCOPE_KEY",
    "BINANCE_SPOT_DEMO_SIDECAR_SCOPE_KEY",
    "KIS_MOCK_SCOPE_KEY",
    "KNOWN_B0X_SCOPE_KEYS",
    "UPBIT_SHADOW_SCOPE_KEY",
]
