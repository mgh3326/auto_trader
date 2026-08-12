"""Canonical B0-X lane/account scope keys."""

from __future__ import annotations

from typing import Final

BINANCE_SPOT_DEMO_SIDECAR_SCOPE_KEY: Final[str] = "binance_spot_demo_sidecar"
UPBIT_SHADOW_SCOPE_KEY: Final[str] = "upbit_shadow"
KIS_MOCK_SCOPE_KEY: Final[str] = "kis_mock"
ALPACA_PAPER_LAB_SCOPE_KEY: Final[str] = "alpaca_paper_lab"

#: §39차 — a **second** KR venue, added while ``kis_mock`` is rejecting orders
#: at the account level (``40910000 모의투자 주문이 불가한 계좌입니다``). It is a
#: distinct scope key rather than a re-pointing of :data:`KIS_MOCK_SCOPE_KEY`
#: because the two lanes have separate accounts, separate ledgers and separate
#: artifact directories, and because the KR ``kis_mock`` lane must keep working
#: unchanged the moment its account participation is restored.
KIWOOM_MOCK_SCOPE_KEY: Final[str] = "kiwoom_mock"

KNOWN_B0X_SCOPE_KEYS: Final[frozenset[str]] = frozenset(
    {
        BINANCE_SPOT_DEMO_SIDECAR_SCOPE_KEY,
        UPBIT_SHADOW_SCOPE_KEY,
        KIS_MOCK_SCOPE_KEY,
        KIWOOM_MOCK_SCOPE_KEY,
        ALPACA_PAPER_LAB_SCOPE_KEY,
    }
)

__all__ = [
    "ALPACA_PAPER_LAB_SCOPE_KEY",
    "BINANCE_SPOT_DEMO_SIDECAR_SCOPE_KEY",
    "KIS_MOCK_SCOPE_KEY",
    "KIWOOM_MOCK_SCOPE_KEY",
    "KNOWN_B0X_SCOPE_KEYS",
    "UPBIT_SHADOW_SCOPE_KEY",
]
