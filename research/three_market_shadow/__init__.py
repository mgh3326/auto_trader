"""Pure research calculations shared by shadow runners and backtests."""

from .calculations import (
    CONTRACT_HASH,
    CRYPTO_SYNTHETIC_SIGNAL,
    calculate_crypto_synthetic,
    calculate_kr_rev3_reclaim,
    calculate_signal,
    calculate_us_mom_cont_z126,
)

__all__ = [
    "CONTRACT_HASH",
    "CRYPTO_SYNTHETIC_SIGNAL",
    "calculate_crypto_synthetic",
    "calculate_kr_rev3_reclaim",
    "calculate_signal",
    "calculate_us_mom_cont_z126",
]
