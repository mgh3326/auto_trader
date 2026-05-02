"""Operator-facing Trading Decision Session request/response schemas."""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.trading_decisions import (
    InstrumentTypeLiteral,
    ProposalKindLiteral,
    SessionStatusLiteral,
    SideLiteral,
)

OperatorMarketScopeLiteral = Literal["kr", "us", "crypto"]

_SYMBOL_RE = re.compile(r"^[A-Za-z0-9._/-]{1,32}$")
_ANALYST_RE = re.compile(r"^[a-z_]{1,32}$")
_CRYPTO_SIGNAL_RE = re.compile(r"^KRW-[A-Z0-9]{2,16}$")
_CRYPTO_EXECUTION_RE = re.compile(r"^[A-Z0-9]{2,16}/USD$")
_CRYPTO_ALLOWED_SIGNAL_TO_EXECUTION = {
    "KRW-BTC": "BTC/USD",
    "KRW-ETH": "ETH/USD",
    "KRW-SOL": "SOL/USD",
}
_CRYPTO_WORKFLOW_REQUIRED_FIELDS = {
    "signal_symbol",
    "signal_venue",
    "execution_symbol",
    "execution_venue",
    "execution_mode",
    "execution_asset_class",
    "workflow_stage",
    "purpose",
    "preview_payload",
    "approval_copy",
}
_CRYPTO_PREVIEW_REQUIRED_FIELDS = {
    "symbol",
    "side",
    "type",
    "notional",
    "limit_price",
    "time_in_force",
    "asset_class",
}
_CRYPTO_PREVIEW_FORBIDDEN_FIELDS = {
    "confirm",
    "dry_run",
    "order_id",
    "client_order_id",
    "submitted",
    "submit",
    "action",
}


class OperatorCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=32)
    instrument_type: InstrumentTypeLiteral
    side: SideLiteral = "none"
    confidence: int = Field(ge=0, le=100)
    proposal_kind: ProposalKindLiteral = "other"
    rationale: str = Field(default="", max_length=4000)
    quantity: Decimal | None = None
    quantity_pct: Decimal | None = Field(default=None, ge=0, le=100)
    amount: Decimal | None = Field(default=None, ge=0)
    price: Decimal | None = Field(default=None, ge=0)
    trigger_price: Decimal | None = Field(default=None, ge=0)
    threshold_pct: Decimal | None = Field(default=None, ge=0, le=100)
    currency: str | None = Field(default=None, max_length=8)
    signal_symbol: str | None = None
    signal_venue: Literal["upbit"] | None = None
    execution_symbol: str | None = None
    execution_venue: Literal["alpaca_paper"] | None = None
    execution_mode: Literal["paper"] | None = None
    execution_asset_class: Literal["crypto"] | None = None
    workflow_stage: Literal["crypto_weekend", "crypto_always_open"] | None = None
    purpose: Literal["paper_plumbing_smoke", "alpha_candidate_review"] | None = None
    preview_payload: dict[str, object] | None = None
    approval_copy: list[str] | None = None

    @field_validator("symbol")
    @classmethod
    def _symbol_charset(cls, value: str) -> str:
        if not _SYMBOL_RE.fullmatch(value):
            raise ValueError("symbol contains unsupported characters")
        return value

    @model_validator(mode="after")
    def _validate_crypto_paper_workflow(self) -> OperatorCandidate:
        flat_workflow = {
            "signal_symbol": self.signal_symbol,
            "signal_venue": self.signal_venue,
            "execution_symbol": self.execution_symbol,
            "execution_venue": self.execution_venue,
            "execution_mode": self.execution_mode,
            "execution_asset_class": self.execution_asset_class,
            "workflow_stage": self.workflow_stage,
            "purpose": self.purpose,
            "preview_payload": self.preview_payload,
            "approval_copy": self.approval_copy,
        }
        workflow = {
            key: value for key, value in flat_workflow.items() if value is not None
        }
        if not workflow:
            return self
        if self.instrument_type != "crypto":
            raise ValueError(
                "crypto_paper_workflow is only allowed for crypto candidates"
            )
        missing = _CRYPTO_WORKFLOW_REQUIRED_FIELDS - set(workflow)
        extra = set(workflow) - _CRYPTO_WORKFLOW_REQUIRED_FIELDS
        if missing or extra:
            raise ValueError(
                "crypto_paper_workflow must contain the complete expected fields"
            )
        self._validate_crypto_workflow_scalars(workflow)
        self._validate_crypto_preview_payload(
            workflow.get("preview_payload"),
            execution_symbol=workflow.get("execution_symbol"),
        )
        approval_copy = workflow.get("approval_copy")
        if not isinstance(approval_copy, list) or not approval_copy:
            raise ValueError("approval_copy must be a non-empty list")
        if not all(isinstance(line, str) and line.strip() for line in approval_copy):
            raise ValueError("approval_copy must contain non-empty strings")
        return self

    @staticmethod
    def _validate_crypto_workflow_scalars(workflow: dict[str, object]) -> None:
        if workflow.get("signal_venue") != "upbit":
            raise ValueError("crypto signal_venue must be upbit")
        if workflow.get("execution_venue") != "alpaca_paper":
            raise ValueError("crypto execution_venue must be alpaca_paper")
        if workflow.get("execution_mode") != "paper":
            raise ValueError("crypto execution_mode must be paper")
        if workflow.get("execution_asset_class") != "crypto":
            raise ValueError("crypto execution_asset_class must be crypto")
        if workflow.get("workflow_stage") not in {
            "crypto_weekend",
            "crypto_always_open",
        }:
            raise ValueError("crypto workflow_stage is unsupported")
        if workflow.get("purpose") not in {
            "paper_plumbing_smoke",
            "alpha_candidate_review",
        }:
            raise ValueError("crypto purpose is unsupported")
        signal_symbol = workflow.get("signal_symbol")
        execution_symbol = workflow.get("execution_symbol")
        if not isinstance(signal_symbol, str) or not _CRYPTO_SIGNAL_RE.fullmatch(
            signal_symbol
        ):
            raise ValueError("crypto signal_symbol must be an Upbit KRW symbol")
        if not isinstance(execution_symbol, str) or not _CRYPTO_EXECUTION_RE.fullmatch(
            execution_symbol
        ):
            raise ValueError("crypto execution_symbol must be an Alpaca USD pair")
        expected_execution_symbol = _CRYPTO_ALLOWED_SIGNAL_TO_EXECUTION.get(
            signal_symbol
        )
        if expected_execution_symbol is None:
            raise ValueError("crypto signal_symbol is unsupported for Alpaca paper")
        if execution_symbol != expected_execution_symbol:
            raise ValueError(
                "crypto signal_symbol and execution_symbol mapping mismatch"
            )

    @staticmethod
    def _validate_crypto_preview_payload(
        preview_payload: object, *, execution_symbol: object
    ) -> None:
        if not isinstance(preview_payload, dict):
            raise ValueError("preview_payload must be an object")
        keys = set(preview_payload)
        if _CRYPTO_PREVIEW_FORBIDDEN_FIELDS & keys:
            raise ValueError("preview_payload must not contain submit/order fields")
        missing = _CRYPTO_PREVIEW_REQUIRED_FIELDS - keys
        extra = keys - _CRYPTO_PREVIEW_REQUIRED_FIELDS
        if missing or extra:
            raise ValueError("preview_payload must contain only preview fields")
        if preview_payload.get("side") != "buy":
            raise ValueError("crypto preview side must be buy")
        if preview_payload.get("type") != "limit":
            raise ValueError("crypto preview type must be limit")
        if preview_payload.get("asset_class") != "crypto":
            raise ValueError("crypto preview asset_class must be crypto")
        if preview_payload.get("time_in_force") not in {"gtc", "ioc"}:
            raise ValueError("crypto preview time_in_force is unsupported")
        symbol = preview_payload.get("symbol")
        if not isinstance(symbol, str) or not _CRYPTO_EXECUTION_RE.fullmatch(symbol):
            raise ValueError("crypto preview symbol must be an Alpaca USD pair")
        if not isinstance(execution_symbol, str) or symbol != execution_symbol:
            raise ValueError("preview_payload symbol must match execution_symbol")
        try:
            notional = Decimal(str(preview_payload.get("notional")))
            limit_price = Decimal(str(preview_payload.get("limit_price")))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(
                "crypto preview notional and limit_price must be numeric"
            ) from exc
        if not notional.is_finite() or not limit_price.is_finite():
            raise ValueError("crypto preview notional and limit_price must be finite")
        if notional <= 0 or notional > Decimal("50"):
            raise ValueError("crypto preview notional must be > 0 and <= 50")
        if limit_price <= 0:
            raise ValueError("crypto preview limit_price must be > 0")


class OperatorDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market_scope: OperatorMarketScopeLiteral
    candidates: list[OperatorCandidate] = Field(min_length=1, max_length=20)
    include_tradingagents: bool = False
    analysts: list[str] | None = None
    strategy_name: str | None = Field(default=None, max_length=128)
    notes: str | None = Field(default=None, max_length=4000)
    source_profile: str = Field(
        default="operator_request",
        min_length=1,
        max_length=64,
    )
    generated_at: datetime | None = None

    @field_validator("analysts")
    @classmethod
    def _analyst_charset(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        for token in value:
            if not _ANALYST_RE.fullmatch(token):
                raise ValueError("analyst token contains unsupported characters")
        return value


class OperatorDecisionResponse(BaseModel):
    session_uuid: UUID
    session_url: str
    status: SessionStatusLiteral
    proposal_count: int
    advisory_used: bool
    advisory_skipped_reason: str | None = None
