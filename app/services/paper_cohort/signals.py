"""Pure canonical target signals and post-signal venue evidence."""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services.crypto_execution_mapping import (
    map_binance_public_spot_to_alpaca_paper,
)
from app.services.paper_cohort.market_snapshot import CanonicalSnapshotPayload
from app.services.research_canonical_hash import canonical_sha256


class FrozenSignalContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SignalComputationInput(FrozenSignalContract):
    cohort_id: str
    assignment_id: str
    experiment_id: str
    strategy_version_id: str
    strategy_hash: str
    config_hash: str
    policy_hash: str
    symbol: Literal["BTCUSDT", "ETHUSDT"]
    target_weight: Decimal = Field(gt=0, le=1, allow_inf_nan=False)
    capital_notional_usd: Decimal = Field(gt=0, allow_inf_nan=False)


class CanonicalTargetSignal(FrozenSignalContract):
    schema_id: Literal["canonical_target_signal.v1"]
    calculation_id: Literal["frozen_target_weight.v1"]
    cohort_id: str
    assignment_id: str
    experiment_id: str
    strategy_version_id: str
    strategy_hash: str
    config_hash: str
    policy_hash: str
    snapshot_id: str
    snapshot_hash: str
    symbol: Literal["BTCUSDT", "ETHUSDT"]
    side: Literal["buy", "sell"]
    target_weight: str
    capital_notional_usd: str
    reference_price: str
    target_notional: str
    target_quantity: str
    signal_hash: str

    def recomputed_signal_hash(self) -> str:
        return canonical_sha256(self.model_dump(mode="python", exclude={"signal_hash"}))


class VenueQuote(FrozenSignalContract):
    venue: Literal["binance", "alpaca"]
    symbol: str
    bid_price: Decimal = Field(gt=0, allow_inf_nan=False)
    ask_price: Decimal = Field(gt=0, allow_inf_nan=False)
    bid_qty: Decimal = Field(gt=0, allow_inf_nan=False)
    ask_qty: Decimal = Field(gt=0, allow_inf_nan=False)
    fetched_at: datetime
    qty_increment: Decimal = Field(gt=0, allow_inf_nan=False)
    min_qty: Decimal = Field(gt=0, allow_inf_nan=False)
    min_notional: Decimal = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_quote(self) -> VenueQuote:
        if self.bid_price >= self.ask_price:
            raise ValueError("venue quote is crossed or locked")
        if self.fetched_at.tzinfo is None:
            raise ValueError("venue quote timestamp must be timezone-aware")
        return self


class WouldOrderEvidence(FrozenSignalContract):
    reason_code: str
    order: dict[str, str] | None
    quote_evidence: dict[str, str]


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def compute_target_signal(
    snapshot: CanonicalSnapshotPayload,
    inputs: SignalComputationInput,
) -> CanonicalTargetSignal:
    symbol_payload = next(
        (item for item in snapshot.symbols if item.symbol == inputs.symbol), None
    )
    if symbol_payload is None:
        raise ValueError("canonical snapshot does not contain requested symbol")
    bid = Decimal(symbol_payload.ticker.bid_price)
    ask = Decimal(symbol_payload.ticker.ask_price)
    reference_price = (bid + ask) / Decimal("2")
    target_notional = inputs.capital_notional_usd * inputs.target_weight
    target_quantity = target_notional / reference_price
    content: dict[str, object] = {
        "schema_id": "canonical_target_signal.v1",
        "calculation_id": "frozen_target_weight.v1",
        "cohort_id": inputs.cohort_id,
        "assignment_id": inputs.assignment_id,
        "experiment_id": inputs.experiment_id,
        "strategy_version_id": inputs.strategy_version_id,
        "strategy_hash": inputs.strategy_hash,
        "config_hash": inputs.config_hash,
        "policy_hash": inputs.policy_hash,
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_hash": snapshot.content_hash,
        "symbol": inputs.symbol,
        "side": "buy",
        "target_weight": _decimal_text(inputs.target_weight),
        "capital_notional_usd": _decimal_text(inputs.capital_notional_usd),
        "reference_price": _decimal_text(reference_price),
        "target_notional": _decimal_text(target_notional),
        "target_quantity": _decimal_text(target_quantity),
    }
    return CanonicalTargetSignal(
        **content,
        signal_hash=canonical_sha256(content),  # type: ignore[arg-type]
    )


def _quote_evidence(quote: VenueQuote) -> dict[str, str]:
    return {
        "venue": quote.venue,
        "symbol": quote.symbol,
        "bid_price": _decimal_text(quote.bid_price),
        "ask_price": _decimal_text(quote.ask_price),
        "bid_qty": _decimal_text(quote.bid_qty),
        "ask_qty": _decimal_text(quote.ask_qty),
        "fetched_at": quote.fetched_at.isoformat(),
        "qty_increment": _decimal_text(quote.qty_increment),
        "min_qty": _decimal_text(quote.min_qty),
        "min_notional": _decimal_text(quote.min_notional),
    }


def build_would_order_evidence(
    signal: CanonicalTargetSignal,
    quote: VenueQuote,
) -> WouldOrderEvidence:
    quote_evidence = _quote_evidence(quote)
    if quote.venue == "binance":
        if signal.side != "buy" or quote.symbol != signal.symbol:
            return WouldOrderEvidence(
                reason_code="unsupported_capability",
                order=None,
                quote_evidence=quote_evidence,
            )
        return WouldOrderEvidence(
            reason_code="ok",
            order={
                "symbol": signal.symbol,
                "side": "buy",
                "order_type": "market",
                "sizing": "notional",
                "notional": signal.target_notional,
            },
            quote_evidence=quote_evidence,
        )

    mapping = map_binance_public_spot_to_alpaca_paper(signal.symbol)
    if quote.symbol != mapping.execution_symbol:
        return WouldOrderEvidence(
            reason_code="unsupported_capability",
            order=None,
            quote_evidence=quote_evidence,
        )
    raw_qty = Decimal(signal.target_notional) / quote.ask_price
    rounded_qty = raw_qty.quantize(quote.qty_increment, rounding=ROUND_DOWN)
    if (
        rounded_qty < quote.min_qty
        or rounded_qty * quote.ask_price < quote.min_notional
    ):
        return WouldOrderEvidence(
            reason_code="unsupported_capability",
            order=None,
            quote_evidence=quote_evidence,
        )
    return WouldOrderEvidence(
        reason_code="ok",
        order={
            "symbol": mapping.execution_symbol,
            "side": signal.side,
            "order_type": "limit",
            "sizing": "qty",
            "qty": _decimal_text(rounded_qty),
            "price": _decimal_text(quote.ask_price),
            "time_in_force": "gtc",
        },
        quote_evidence=quote_evidence,
    )


__all__ = [
    "CanonicalTargetSignal",
    "SignalComputationInput",
    "VenueQuote",
    "WouldOrderEvidence",
    "build_would_order_evidence",
    "compute_target_signal",
]
