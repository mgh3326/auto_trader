"""Pure crypto signal-to-paper-execution mapping helpers.

This module is intentionally side-effect free: no broker services, settings, DB,
or MCP order tooling imports belong here.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SignalVenue = Literal["upbit"]
ExecutionVenue = Literal["alpaca_paper"]
CryptoStage = Literal["crypto_weekend", "crypto_always_open"]
ExecutionMode = Literal["paper"]
PurposeLabel = Literal["paper_plumbing_smoke", "alpha_candidate_review"]


class CryptoExecutionMappingError(ValueError):
    """Raised when a signal symbol is not supported for paper execution."""


class CryptoSignalExecutionMapping(BaseModel):
    model_config = ConfigDict(frozen=True)

    signal_symbol: str
    signal_venue: SignalVenue = "upbit"
    execution_symbol: str
    execution_venue: ExecutionVenue = "alpaca_paper"
    asset_class: Literal["crypto"] = "crypto"
    execution_mode: ExecutionMode = "paper"


class CryptoWeekendReadiness(BaseModel):
    model_config = ConfigDict(frozen=True)

    market_scope: Literal["crypto"] = "crypto"
    stage: CryptoStage = "crypto_weekend"
    upbit_ohlcv: Literal["ready", "degraded", "unavailable"] = "ready"
    crypto_news: Literal["ready", "degraded", "unavailable"] = "degraded"
    alpaca_paper: Literal["ready", "unavailable"] = "unavailable"
    execution: Literal["approval_required"] = "approval_required"


class AlpacaPaperCryptoPreviewPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    side: Literal["buy"] = "buy"
    type: Literal["limit"] = "limit"
    notional: Decimal = Field(default=Decimal("10"), gt=0, le=Decimal("50"))
    limit_price: Decimal = Field(default=Decimal("1.00"), gt=0)
    time_in_force: Literal["gtc", "ioc"] = "gtc"
    asset_class: Literal["crypto"] = "crypto"


class CryptoPaperApprovalMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    mapping: CryptoSignalExecutionMapping
    stage: CryptoStage = "crypto_weekend"
    purpose: PurposeLabel
    preview_payload: AlpacaPaperCryptoPreviewPayload
    approval_copy: list[str]


_ALLOWED_SIGNAL_TO_EXECUTION = {
    "KRW-BTC": "BTC/USD",
    "KRW-ETH": "ETH/USD",
    "KRW-SOL": "SOL/USD",
}


def map_upbit_to_alpaca_paper(signal_symbol: str) -> CryptoSignalExecutionMapping:
    """Map an explicit Upbit KRW signal symbol to an Alpaca Paper USD pair."""
    normalized = (signal_symbol or "").strip().upper()
    try:
        execution_symbol = _ALLOWED_SIGNAL_TO_EXECUTION[normalized]
    except KeyError as exc:
        allowed = ", ".join(sorted(_ALLOWED_SIGNAL_TO_EXECUTION))
        raise CryptoExecutionMappingError(
            f"unsupported crypto signal symbol {signal_symbol!r}; allowed: {allowed}"
        ) from exc
    return CryptoSignalExecutionMapping(
        signal_symbol=normalized,
        execution_symbol=execution_symbol,
    )


def build_alpaca_paper_crypto_preview_payload(
    mapping: CryptoSignalExecutionMapping,
    *,
    notional: Decimal = Decimal("10"),
    limit_price: Decimal = Decimal("1.00"),
    time_in_force: Literal["gtc", "ioc"] = "gtc",
) -> AlpacaPaperCryptoPreviewPayload:
    """Build a pure Alpaca Paper preview payload without account/broker calls."""
    return AlpacaPaperCryptoPreviewPayload(
        symbol=mapping.execution_symbol,
        notional=notional,
        limit_price=limit_price,
        time_in_force=time_in_force,
    )


def build_crypto_paper_approval_metadata(
    signal_symbol: str,
    *,
    purpose: PurposeLabel = "paper_plumbing_smoke",
    stage: CryptoStage = "crypto_weekend",
    notional: Decimal = Decimal("10"),
    limit_price: Decimal = Decimal("1.00"),
    time_in_force: Literal["gtc", "ioc"] = "gtc",
) -> CryptoPaperApprovalMetadata:
    """Build provenance, pure preview payload, and operator approval copy."""
    mapping = map_upbit_to_alpaca_paper(signal_symbol)
    preview_payload = build_alpaca_paper_crypto_preview_payload(
        mapping,
        notional=notional,
        limit_price=limit_price,
        time_in_force=time_in_force,
    )
    approval_copy = [
        f"Signal source: Upbit {mapping.signal_symbol}",
        f"Execution venue: Alpaca Paper {mapping.execution_symbol}",
        f"Purpose: {purpose}",
        "Order: buy limit "
        f"${preview_payload.notional} @ ${preview_payload.limit_price} "
        f"{preview_payload.time_in_force.upper()}",
    ]
    return CryptoPaperApprovalMetadata(
        mapping=mapping,
        stage=stage,
        purpose=purpose,
        preview_payload=preview_payload,
        approval_copy=approval_copy,
    )


def build_operator_candidate_crypto_metadata(
    signal_symbol: str, **kwargs: Any
) -> dict[str, Any]:
    """Return JSON-ready fields for OperatorCandidate crypto paper metadata."""
    metadata = build_crypto_paper_approval_metadata(signal_symbol, **kwargs)
    return {
        "signal_symbol": metadata.mapping.signal_symbol,
        "signal_venue": metadata.mapping.signal_venue,
        "execution_symbol": metadata.mapping.execution_symbol,
        "execution_venue": metadata.mapping.execution_venue,
        "execution_mode": metadata.mapping.execution_mode,
        "execution_asset_class": metadata.mapping.asset_class,
        "workflow_stage": metadata.stage,
        "purpose": metadata.purpose,
        "preview_payload": metadata.preview_payload.model_dump(mode="json"),
        "approval_copy": metadata.approval_copy,
    }


__all__ = [
    "AlpacaPaperCryptoPreviewPayload",
    "CryptoExecutionMappingError",
    "CryptoPaperApprovalMetadata",
    "CryptoSignalExecutionMapping",
    "CryptoStage",
    "CryptoWeekendReadiness",
    "ExecutionMode",
    "ExecutionVenue",
    "PurposeLabel",
    "SignalVenue",
    "build_alpaca_paper_crypto_preview_payload",
    "build_crypto_paper_approval_metadata",
    "build_operator_candidate_crypto_metadata",
    "map_upbit_to_alpaca_paper",
]
