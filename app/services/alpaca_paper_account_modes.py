"""Supported Alpaca paper account identities and profile routing."""

from __future__ import annotations

from typing import Literal, cast

ALPACA_PAPER_ACCOUNT_MODE = "alpaca_paper"
ALPACA_PAPER_LAB_ACCOUNT_MODE = "alpaca_paper_lab"
ALPACA_PAPER_CRYPTO_ACCOUNT_MODE = "alpaca_paper_crypto"
AlpacaPaperAccountMode = Literal[
    "alpaca_paper", "alpaca_paper_lab", "alpaca_paper_crypto"
]

ALPACA_PAPER_ACCOUNT_MODES = frozenset(
    {
        ALPACA_PAPER_ACCOUNT_MODE,
        ALPACA_PAPER_LAB_ACCOUNT_MODE,
        ALPACA_PAPER_CRYPTO_ACCOUNT_MODE,
    }
)


def normalize_alpaca_paper_account_mode(
    account_mode: str = ALPACA_PAPER_ACCOUNT_MODE,
) -> AlpacaPaperAccountMode:
    normalized = str(account_mode or "").strip().lower()
    if normalized not in ALPACA_PAPER_ACCOUNT_MODES:
        allowed = ", ".join(sorted(ALPACA_PAPER_ACCOUNT_MODES))
        raise ValueError(f"account_mode must be one of: {allowed}")
    return cast(AlpacaPaperAccountMode, normalized)


def profile_for_account_mode(account_mode: str) -> str | None:
    normalized = normalize_alpaca_paper_account_mode(account_mode)
    if normalized == ALPACA_PAPER_LAB_ACCOUNT_MODE:
        return "lab"
    if normalized == ALPACA_PAPER_CRYPTO_ACCOUNT_MODE:
        return "clean"
    return None


def account_mode_for_profile(profile: str | None) -> AlpacaPaperAccountMode:
    from app.services.brokers.alpaca.exceptions import AlpacaPaperConfigurationError

    normalized = str(profile or "").strip().lower()
    if normalized in {"", "default"}:
        return ALPACA_PAPER_ACCOUNT_MODE
    if normalized == "lab":
        return ALPACA_PAPER_LAB_ACCOUNT_MODE
    if normalized == "clean":
        return ALPACA_PAPER_CRYPTO_ACCOUNT_MODE
    raise AlpacaPaperConfigurationError(
        "Alpaca paper profile must be one of: default, lab, clean"
    )


def validate_alpaca_paper_asset_class(
    account_mode: str,
    asset_class: str,
) -> None:
    """Enforce the asset-class contract owned by the selected account.

    The clean/crypto account is not a generic Alpaca account with a cosmetic
    symbol list.  It may only receive crypto orders; otherwise the crypto
    symbol and notional restrictions could be bypassed through the equity
    validator.
    """

    normalized_mode = normalize_alpaca_paper_account_mode(account_mode)
    normalized_asset_class = str(asset_class or "").strip().lower()
    if (
        normalized_mode == ALPACA_PAPER_CRYPTO_ACCOUNT_MODE
        and normalized_asset_class != "crypto"
    ):
        raise ValueError("alpaca_paper_crypto supports crypto orders only")


__all__ = [
    "ALPACA_PAPER_ACCOUNT_MODE",
    "ALPACA_PAPER_ACCOUNT_MODES",
    "ALPACA_PAPER_LAB_ACCOUNT_MODE",
    "ALPACA_PAPER_CRYPTO_ACCOUNT_MODE",
    "AlpacaPaperAccountMode",
    "account_mode_for_profile",
    "normalize_alpaca_paper_account_mode",
    "profile_for_account_mode",
    "validate_alpaca_paper_asset_class",
]
