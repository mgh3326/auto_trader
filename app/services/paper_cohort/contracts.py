"""Frozen side-effect-free contracts for ROB-849 paper cohorts."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from app.services.research_canonical_hash import canonical_sha256

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Identifier128 = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)
]


def _normalize_hash_value(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, dict):
        return {key: _normalize_hash_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_hash_value(item) for item in value]
    return value


class PaperCohortError(Exception):
    """Stable fail-closed paper cohort error."""

    def __init__(self, reason_code: str, message: str | None = None) -> None:
        self.reason_code = reason_code
        super().__init__(message or reason_code)


class RunMode(StrEnum):
    SHADOW = "shadow"
    PAPER_ACTIVE = "paper_active"


class FrozenContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SymbolTargetWeight(FrozenContract):
    symbol: Literal["BTCUSDT", "ETHUSDT"]
    weight: Decimal = Field(gt=0, le=1, allow_inf_nan=False)


class CohortAssignmentInput(FrozenContract):
    assignment_id: Identifier128
    ordinal: int = Field(ge=0, le=2)
    role: Literal["champion", "challenger"]
    validation_id: Identifier128
    validation_version: int = Field(ge=1)
    experiment_id: Sha256
    source_backtest_run_id: int = Field(gt=0)
    strategy_version_id: Identifier128
    target_weights: tuple[SymbolTargetWeight, SymbolTargetWeight]
    experiment_hash: Sha256
    strategy_hash: Sha256
    config_hash: Sha256
    policy_hash: Sha256
    input_hash: Sha256

    @model_validator(mode="after")
    def validate_identity_and_weights(self) -> CohortAssignmentInput:
        if self.experiment_hash != self.experiment_id:
            raise ValueError("experiment_hash must exactly match experiment_id")
        if tuple(weight.symbol for weight in self.target_weights) != (
            "BTCUSDT",
            "ETHUSDT",
        ):
            raise ValueError("target weights must use exact ordered V1 symbols")
        if sum(weight.weight for weight in self.target_weights) > 1:
            raise ValueError("target weights must not exceed one")
        if (self.role, self.ordinal) not in {
            ("champion", 0),
            ("challenger", 1),
            ("challenger", 2),
        }:
            raise ValueError("role and ordinal do not match the V1 cohort contract")
        return self

    def weights_json(self) -> dict[str, str]:
        return {weight.symbol: str(weight.weight) for weight in self.target_weights}


class CohortActivation(FrozenContract):
    cohort_id: Identifier128
    expected_cohort_hash: Sha256
    venues: tuple[Literal["binance"], Literal["alpaca"]]
    symbols: tuple[Literal["BTCUSDT"], Literal["ETHUSDT"]]
    market: Literal["spot"]
    leverage: Decimal = Field(gt=0, allow_inf_nan=False)
    interval: Literal["1m"]
    required_lookback: int = Field(gt=0, le=1000)
    max_capture_skew_ms: int = Field(gt=0)
    max_ticker_age_ms: int = Field(gt=0)
    capital_notional_usd: Decimal = Field(
        gt=0, allow_inf_nan=False, max_digits=24, decimal_places=12
    )
    activated_at: datetime
    stop_at: datetime | None = None
    assignments: tuple[CohortAssignmentInput, ...] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def validate_v1_composition(self) -> CohortActivation:
        if self.leverage != Decimal("1"):
            raise ValueError("V1 leverage must be exactly one")
        if self.activated_at.tzinfo is None:
            raise ValueError("activated_at must be timezone-aware")
        if self.stop_at is not None:
            if self.stop_at.tzinfo is None:
                raise ValueError("stop_at must be timezone-aware")
            if self.stop_at <= self.activated_at:
                raise ValueError("stop_at must follow activated_at")
        expected = tuple(
            ("champion", 0) if ordinal == 0 else ("challenger", ordinal)
            for ordinal in range(len(self.assignments))
        )
        actual = tuple(
            (assignment.role, assignment.ordinal) for assignment in self.assignments
        )
        if actual != expected:
            raise ValueError("cohort requires one champion then up to two challengers")
        if len({item.assignment_id for item in self.assignments}) != len(
            self.assignments
        ):
            raise ValueError("assignment_id values must be unique")
        if len({item.validation_id for item in self.assignments}) != len(
            self.assignments
        ):
            raise ValueError("validation_id values must be unique")
        if len({item.experiment_id for item in self.assignments}) != len(
            self.assignments
        ):
            raise ValueError("experiment_id values must be unique")
        return self

    def identity_payload(self) -> dict[str, object]:
        payload = self.model_dump(mode="python", exclude={"expected_cohort_hash"})
        normalized = _normalize_hash_value(payload)
        assert isinstance(normalized, dict)
        return {"schema_id": "paper_validation_cohort.v1", **normalized}

    def computed_cohort_hash(self) -> str:
        return canonical_sha256(self.identity_payload())


__all__ = [
    "CohortActivation",
    "CohortAssignmentInput",
    "PaperCohortError",
    "RunMode",
    "SymbolTargetWeight",
]
