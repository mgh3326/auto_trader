# app/services/trade_journal/forecast_service.py
"""ROB-650 — resolvable forecast ledger: record, deterministic resolve, score.

The repository is the only write surface for ``review.trade_forecasts``.
Composition of a forecast (choosing the probability/thesis) is a Claude session
(LLM boundary); everything here — recording, OHLCV-backed resolution, Brier
scoring, calibration aggregation — is fully deterministic and side-effect-free
apart from the DB write.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import math
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.core.symbol import to_db_symbol
from app.core.timezone import now_kst
from app.models.review import TradeForecast
from app.models.trading import InstrumentType
from app.services.daily_candles.provenance import (
    DAILY_SOURCE_CONTRACTS,
    daily_source_row_id,
)
from app.services.daily_candles.repository import (
    DailyCandleRow,
    DailyCandlesRepository,
    MarketKey,
)
from app.services.trading_policy_service import policy_version_stamp

logger = logging.getLogger(__name__)


# Fail-open fallback policy stamp. ROB-659: the default now comes from the ROB-646
# trading-policy YAML single source via ``_default_policy_version`` (below); this
# literal is only used if that YAML is unreadable, so a forecast write never
# crashes on a missing policy file. A caller-supplied ``policy_version`` still wins.
POLICY_VERSION = "forecast.v1"

# Crypto pairs are stored with a market-prefix separated by '-' (e.g. "KRW-BTC");
# that dash is a real separator, unlike an equity ticker's ("BRK-B" -> "BRK.B").
_CRYPTO_QUOTE_CURRENCIES = {"KRW", "BTC", "USDT", "USD"}


def _default_policy_version() -> str:
    """ROB-659: stamp the ROB-646 policy version, fail-open to the legacy literal."""
    try:
        return policy_version_stamp()["version"]
    except Exception:
        return POLICY_VERSION


def _normalize_symbol_for_filter(
    symbol: str, instrument_type: str | None = None
) -> str:
    """Normalize a *query* symbol to the stored DB form for filtering.

    ROB-659: mirrors the write-side ``_normalize_symbol`` so a query like "BRK-B"
    matches the stored "BRK.B". When ``instrument_type`` is known we reuse the exact
    write-side normalization; without it we apply the dash/slash -> dot rewrite but
    leave crypto pairs ("KRW-BTC") intact (their dash is a real market separator).
    """
    if instrument_type is not None:
        return _normalize_symbol(symbol, instrument_type)
    normalized = symbol.strip().upper()
    quote, sep, _base = normalized.partition("-")
    if sep and quote in _CRYPTO_QUOTE_CURRENCIES:
        return normalized
    return to_db_symbol(normalized)


_KST = ZoneInfo("Asia/Seoul")

_VALID_INSTRUMENTS = {t.value for t in InstrumentType}
# Instrument types with a loaded daily-candle store → deterministic auto-resolve.
_AUTO_RESOLVABLE_INSTRUMENTS = {"equity_kr", "equity_us", "crypto"}
_PRICE_DIRECTIONS = {"at_or_above", "at_or_below"}
_PRICE_TOUCH_RULE_VERSION = "window-touch-v1-high-gte-low-lte"
_TERMINAL_CLOSE_KIND = "terminal_close"
_TERMINAL_CLOSE_DIRECTIONS = {"up", "down"}
_TERMINAL_CLOSE_INSTRUMENTS = {"equity_kr", "equity_us"}
_TERMINAL_CLOSE_RULE_VERSION = "terminal-close-v1-up-gte-down-lt"
_TERMINAL_CLOSE_ADJUSTMENT_POLICIES = {
    "explicit-factor-v1",
    "unverified_fail_closed",
}
_IMMUTABLE_CLAIM_VERSION = "forecast-immutable-claim-v1"
_SEMANTICS_ATTESTATION_VERSION = "forecast-semantics-attestation-v1"
_SEMANTICS_SUPERSESSION_VERSION = "forecast-semantics-supersession-v1"
_ADJUSTMENT_PROVENANCE_VERSION = "corporate-action-adjustment-v1"
_EVIDENCE_AUTHENTICATION_VERSION = "forecast-evidence-authentication-v1"
_TERMINAL_ADJUSTMENT_EVIDENCE_VERSION = "terminal-adjustment-evidence-v1"
_ADJUSTMENT_AUTHORITY_IDS = {
    "exchange": {"KRX", "NASDAQ", "NYSE", "AMEX"},
    "regulator": {"DART", "SEC_EDGAR"},
    "issuer": {"ISSUER_FILING"},
    "licensed_data_vendor": {"KIS", "TOSS", "YAHOO_FINANCE"},
}
_AUTHENTICATION_METHODS = {
    "mcp_bearer",
    "service_identity",
}
_ACTION_TYPES = {"none", "split", "reverse_split", "basis_change"}
_PRICE_BASES = {"raw", "provider_adjusted"}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ACTOR_RE = re.compile(r"^(?:user|service):[A-Za-z0-9._-]{3,128}$")
_ADJUSTMENT_PROVENANCE_KEYS = (
    "contract_version",
    "authority_type",
    "authority_id",
    "actor_principal",
    "authentication_method",
    "symbol",
    "action_type",
    "action_ratio",
    "effective_date",
    "verified_through_date",
    "source",
    "source_ref",
    "source_sha256",
    "source_price_basis",
)
_SEMANTICS_EVIDENCE_INPUT_KEYS = (
    "contract_version",
    "authority_type",
    "actor_principal",
    "authentication_method",
    "source_target_sha256",
    "evidence_sha256",
    "evidence_ref",
    "reason",
    "attested_at",
)
_GROUP_BY_FIELDS = {"created_by", "session_label", "model_label", "day"}
_NO_RESOLVABLE_FORECAST_KIND = "no_resolvable_forecast"
_CLOSED_NO_CLAIM_STATUS = "closed_no_claim"


class ForecastValidationError(ValueError):
    """Raised when a forecast payload violates a typed constraint."""


class TerminalCloseDataError(ForecastValidationError):
    """Typed fail-closed condition for terminal-close source data."""

    def __init__(
        self,
        status: str,
        message: str,
        *,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.evidence = evidence or {}


@dataclass(frozen=True, slots=True)
class AuthenticatedForecastActor:
    """Trusted application identity, supplied outside the forecast JSON payload."""

    principal: str
    authentication_method: Literal["mcp_bearer", "service_identity"]


def _validate_evidence_actor(
    evidence: dict[str, Any],
    authenticated_actor: AuthenticatedForecastActor | None,
    *,
    context: str,
) -> AuthenticatedForecastActor:
    """Bind self-described evidence fields to a trusted composition identity."""
    if authenticated_actor is None:
        raise ForecastValidationError(
            f"{context} requires an authenticated forecast evidence actor"
        )
    principal = authenticated_actor.principal.strip()
    if _ACTOR_RE.fullmatch(principal) is None:
        raise ForecastValidationError(
            f"{context} authenticated actor principal is invalid"
        )
    if authenticated_actor.authentication_method not in _AUTHENTICATION_METHODS:
        raise ForecastValidationError(
            f"{context} authenticated actor method is invalid"
        )
    if evidence.get("actor_principal") != principal:
        raise ForecastValidationError(
            f"{context} actor_principal does not match authenticated actor"
        )
    if (
        evidence.get("authentication_method")
        != authenticated_actor.authentication_method
    ):
        raise ForecastValidationError(
            f"{context} authentication_method does not match authenticated actor"
        )
    return authenticated_actor


def _evidence_authentication_binding(
    provenance: dict[str, Any],
    authenticated_actor: AuthenticatedForecastActor,
) -> dict[str, Any]:
    """Return durable proof of the application identity checked at write time."""
    return {
        "contract_version": _EVIDENCE_AUTHENTICATION_VERSION,
        "actor_principal": authenticated_actor.principal.strip(),
        "authentication_method": authenticated_actor.authentication_method,
        "provenance_sha256": _canonical_hash(provenance),
    }


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested in isolation)
# ---------------------------------------------------------------------------
def brier_score(probability: float, outcome: bool) -> float:
    """Brier score for a single binary forecast: ``(p - o)**2``, o in {0,1}.

    Boundaries: p=0/outcome=False -> 0; p=1/outcome=True -> 0; p=0.5 -> 0.25
    regardless of outcome; a fully-wrong confident call (p=1, outcome=False)
    -> 1.
    """
    o = 1.0 if outcome else 0.0
    return (float(probability) - o) ** 2


def classify_price_target_outcome(
    candles: list[DailyCandleRow],
    *,
    direction: str,
    target_price: float,
) -> tuple[bool, float]:
    """Deterministically resolve a price-target claim over a candle window.

    ``at_or_above``: outcome is True iff any bar's ``high`` reaches the target;
    the observed extreme is the window ``max(high)``. ``at_or_below``: True iff
    any bar's ``low`` reaches the target; observed extreme is ``min(low)``.
    Raises on empty candles (caller must guard) or an unknown direction.
    """
    if not candles:
        raise ForecastValidationError("cannot classify an empty candle window")
    if direction == "at_or_above":
        extreme = max(c.high for c in candles)
        return extreme >= target_price, extreme
    if direction == "at_or_below":
        extreme = min(c.low for c in candles)
        return extreme <= target_price, extreme
    raise ForecastValidationError(f"invalid price-target direction: {direction!r}")


def classify_terminal_close_outcome(
    candles: list[DailyCandleRow],
    *,
    review_date: date,
    direction: str,
    target_price: float,
) -> tuple[bool, float, DailyCandleRow]:
    """Resolve one review-session terminal close against a typed threshold.

    Exactly one trusted regular-session daily candle dated ``review_date`` is
    required. Only its ``close`` is observed. V1 defines complementary events:
    ``up`` is ``close >= target`` and ``down`` is ``close < target``.
    """
    if direction not in _TERMINAL_CLOSE_DIRECTIONS:
        raise ForecastValidationError(
            f"invalid terminal-close direction: {direction!r}"
        )

    matching = [candle for candle in candles if _row_date(candle) == review_date]
    if not matching:
        candidate_dates = sorted({_row_date(candle).isoformat() for candle in candles})
        status = (
            "unresolved_stale_data"
            if candidate_dates
            else "unresolved_no_review_candle"
        )
        reason = (
            f"no candle dated review_date={review_date.isoformat()}; "
            f"candidate_dates={candidate_dates}"
        )
        raise TerminalCloseDataError(
            status,
            reason,
            evidence={"candidate_source_dates": candidate_dates},
        )
    if len(matching) != 1:
        raise TerminalCloseDataError(
            "unresolved_ambiguous_review_candle",
            (
                f"expected exactly one review-date candle, found {len(matching)} "
                f"for {review_date.isoformat()}"
            ),
            evidence={"review_date_candle_count": len(matching)},
        )

    selected = matching[0]
    source = str(selected.source or "")
    source_contract = DAILY_SOURCE_CONTRACTS.get(source)
    if source_contract is None:
        raise TerminalCloseDataError(
            "unresolved_untrusted_source",
            f"daily candle source={source!r} is not a trusted regular-session source",
            evidence={"source": source},
        )

    expected_row_id = daily_source_row_id(selected)
    if (
        selected.is_final is not True
        or selected.session_scope != "regular"
        or selected.ingested_at is None
        or selected.source_row_id != expected_row_id
        or selected.source_row_version != source_contract.source_row_version
        or selected.price_basis != source_contract.price_basis
    ):
        raise TerminalCloseDataError(
            "unresolved_non_final_candle",
            "review-date candle lacks verified final regular-session provenance",
            evidence={
                "source": source,
                "is_final": selected.is_final,
                "session_scope": selected.session_scope,
                "source_row_id": selected.source_row_id,
                "expected_source_row_id": expected_row_id,
                "source_row_version": selected.source_row_version,
                "expected_source_row_version": source_contract.source_row_version,
                "source_price_basis": selected.price_basis,
                "expected_source_price_basis": source_contract.price_basis,
                "ingested_at": (
                    selected.ingested_at.isoformat()
                    if selected.ingested_at is not None
                    else None
                ),
            },
        )

    close = float(selected.close)
    if not math.isfinite(close) or close <= 0:
        raise TerminalCloseDataError(
            "unresolved_invalid_close",
            f"review-date close must be positive and finite: {selected.close!r}",
            evidence={"source": source, "source_price": selected.close},
        )

    outcome = close >= target_price if direction == "up" else close < target_price
    return outcome, close, selected


def _to_decimal(x: float | None) -> Decimal | None:
    return Decimal(str(x)) if x is not None else None


def _normalize_symbol(symbol: str, instrument_type: str) -> str:
    normalized = symbol.strip().upper()
    if instrument_type == "crypto":
        if normalized and "-" not in normalized:
            return f"KRW-{normalized}"
        return normalized
    if instrument_type == "equity_us":
        return to_db_symbol(normalized).upper()
    return normalized


def _kst_date(value: datetime | None) -> date | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.astimezone(_KST).date()


def _row_date(row: DailyCandleRow) -> date:
    ts = row.time_utc
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.UTC)
    return ts.date()


def _parse_date(value: str | date, field: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError) as exc:
        raise ForecastValidationError(f"{field} must be YYYY-MM-DD: {value!r}") from exc


def _validate_forecast_target(
    target: Any,
    *,
    instrument_type: str,
    review_date: date,
    symbol: str | None = None,
    forecast_start_date: date | None = None,
    authenticated_actor: AuthenticatedForecastActor | None = None,
    require_authenticated_actor: bool = True,
) -> None:
    if not isinstance(target, dict):
        raise ForecastValidationError("forecast_target must be an object")
    kind = target.get("kind")
    if not kind or not isinstance(kind, str):
        raise ForecastValidationError("forecast_target.kind is required")
    if kind == "price_target":
        rule_version = target.get("outcome_rule_version")
        if rule_version != _PRICE_TOUCH_RULE_VERSION:
            raise ForecastValidationError(
                "price_target.outcome_rule_version must be "
                f"{_PRICE_TOUCH_RULE_VERSION!r}"
            )
        direction = target.get("direction")
        if direction not in _PRICE_DIRECTIONS:
            raise ForecastValidationError(
                f"price_target.direction must be one of {sorted(_PRICE_DIRECTIONS)}"
            )
        price = target.get("target_price")
        try:
            price_f = float(price)
        except (TypeError, ValueError) as exc:
            raise ForecastValidationError(
                "price_target.target_price must be a number"
            ) from exc
        if not math.isfinite(price_f) or price_f <= 0:
            raise ForecastValidationError(
                "price_target.target_price must be positive and finite"
            )
        return
    if kind != _TERMINAL_CLOSE_KIND:
        return

    if instrument_type not in _TERMINAL_CLOSE_INSTRUMENTS:
        raise ForecastValidationError(
            "terminal_close requires instrument_type equity_kr or equity_us"
        )
    direction = target.get("direction")
    if direction not in _TERMINAL_CLOSE_DIRECTIONS:
        raise ForecastValidationError(
            "terminal_close.direction must be one of "
            f"{sorted(_TERMINAL_CLOSE_DIRECTIONS)}"
        )
    try:
        target_price = float(target.get("target_price"))
    except (TypeError, ValueError) as exc:
        raise ForecastValidationError(
            "terminal_close.target_price must be a number"
        ) from exc
    if not math.isfinite(target_price) or target_price <= 0:
        raise ForecastValidationError(
            "terminal_close.target_price must be positive and finite"
        )

    rule_version = target.get("outcome_rule_version")
    if rule_version != _TERMINAL_CLOSE_RULE_VERSION:
        raise ForecastValidationError(
            "terminal_close.outcome_rule_version must be "
            f"{_TERMINAL_CLOSE_RULE_VERSION!r}"
        )

    adjustment_policy = target.get("price_adjustment_policy")
    if adjustment_policy not in _TERMINAL_CLOSE_ADJUSTMENT_POLICIES:
        raise ForecastValidationError(
            "terminal_close.price_adjustment_policy must be one of "
            f"{sorted(_TERMINAL_CLOSE_ADJUSTMENT_POLICIES)}"
        )
    if adjustment_policy == "unverified_fail_closed":
        if (
            target.get("target_to_close_factor") is not None
            or target.get("adjustment_provenance") is not None
        ):
            raise ForecastValidationError(
                "unverified_fail_closed must not carry unverified factor/provenance"
            )
        return

    try:
        factor = float(target.get("target_to_close_factor"))
    except (TypeError, ValueError) as exc:
        raise ForecastValidationError(
            "terminal_close.target_to_close_factor must be a number"
        ) from exc
    if not math.isfinite(factor) or factor <= 0:
        raise ForecastValidationError(
            "terminal_close.target_to_close_factor must be positive and finite"
        )
    if not math.isfinite(target_price * factor):
        raise ForecastValidationError(
            "terminal_close effective target must be positive and finite"
        )

    provenance: Any = target.get("adjustment_provenance")
    if not isinstance(provenance, dict):
        raise ForecastValidationError(
            "terminal_close.adjustment_provenance must be an object"
        )
    if set(provenance) != set(_ADJUSTMENT_PROVENANCE_KEYS):
        raise ForecastValidationError(
            "terminal_close.adjustment_provenance fields must exactly match "
            "the typed contract"
        )
    if provenance.get("contract_version") != _ADJUSTMENT_PROVENANCE_VERSION:
        raise ForecastValidationError(
            "terminal_close.adjustment_provenance.contract_version must be "
            f"{_ADJUSTMENT_PROVENANCE_VERSION!r}"
        )

    authority_type = provenance.get("authority_type")
    authority_id = provenance.get("authority_id")
    allowed_ids = _ADJUSTMENT_AUTHORITY_IDS.get(str(authority_type))
    if allowed_ids is None or authority_id not in allowed_ids:
        raise ForecastValidationError(
            "terminal_close.adjustment_provenance authority_type/authority_id "
            "must identify an allowlisted authority"
        )

    actor = provenance.get("actor_principal")
    if not isinstance(actor, str) or _ACTOR_RE.fullmatch(actor) is None:
        raise ForecastValidationError(
            "terminal_close.adjustment_provenance.actor_principal must be a "
            "typed user: or service: principal"
        )
    if provenance.get("authentication_method") not in _AUTHENTICATION_METHODS:
        raise ForecastValidationError(
            "terminal_close.adjustment_provenance.authentication_method is invalid"
        )
    if require_authenticated_actor:
        _validate_evidence_actor(
            provenance,
            authenticated_actor,
            context="terminal_close.adjustment_provenance",
        )

    normalized_symbol = (
        _normalize_symbol(symbol, instrument_type) if symbol is not None else None
    )
    if normalized_symbol is not None and provenance.get("symbol") != normalized_symbol:
        raise ForecastValidationError(
            "terminal_close.adjustment_provenance.symbol must match forecast symbol"
        )

    action_type = provenance.get("action_type")
    if action_type not in _ACTION_TYPES:
        raise ForecastValidationError(
            "terminal_close.adjustment_provenance.action_type is invalid"
        )
    try:
        action_ratio = float(provenance.get("action_ratio"))
    except (TypeError, ValueError) as exc:
        raise ForecastValidationError(
            "terminal_close.adjustment_provenance.action_ratio must be a number"
        ) from exc
    if not math.isfinite(action_ratio) or action_ratio <= 0:
        raise ForecastValidationError(
            "terminal_close.adjustment_provenance.action_ratio must be positive "
            "and finite"
        )
    if action_type == "none" and (
        not math.isclose(action_ratio, 1.0) or not math.isclose(factor, 1.0)
    ):
        raise ForecastValidationError(
            "no-action evidence requires action_ratio=1 and factor=1"
        )
    if action_type == "split" and action_ratio <= 1:
        raise ForecastValidationError("split action_ratio must be > 1")
    if action_type == "reverse_split" and action_ratio >= 1:
        raise ForecastValidationError("reverse_split action_ratio must be < 1")
    if action_type != "none" and not math.isclose(
        factor, 1.0 / action_ratio, rel_tol=1e-12, abs_tol=1e-12
    ):
        raise ForecastValidationError(
            "terminal_close.target_to_close_factor must equal 1 / action_ratio"
        )

    effective_date = _parse_date(
        provenance.get("effective_date"),
        "terminal_close.adjustment_provenance.effective_date",
    )
    if effective_date > review_date:
        raise ForecastValidationError(
            "terminal_close.adjustment_provenance.effective_date must be on or "
            "before review_date"
        )
    if forecast_start_date is not None and effective_date < forecast_start_date:
        raise ForecastValidationError(
            "terminal_close.adjustment_provenance.effective_date must not precede "
            "forecast_start_date"
        )

    for field in ("source", "source_ref"):
        value = provenance.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ForecastValidationError(
                f"terminal_close.adjustment_provenance.{field} is required"
            )
    source_hash = provenance.get("source_sha256")
    if not isinstance(source_hash, str) or _SHA256_RE.fullmatch(source_hash) is None:
        raise ForecastValidationError(
            "terminal_close.adjustment_provenance.source_sha256 must be 64 "
            "lowercase hex characters"
        )
    if provenance.get("source_price_basis") not in _PRICE_BASES:
        raise ForecastValidationError(
            "terminal_close.adjustment_provenance.source_price_basis must be raw "
            "or provider_adjusted"
        )
    verified_through = _parse_date(
        provenance.get("verified_through_date"),
        "terminal_close.adjustment_provenance.verified_through_date",
    )
    if verified_through != review_date:
        raise ForecastValidationError(
            "terminal_close.adjustment_provenance.verified_through_date "
            "must equal review_date"
        )


def _json_ready(value: Any) -> Any:
    if isinstance(value, Decimal):
        normalized = value.normalize()
        return "0" if normalized == 0 else format(normalized, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, dict):
        return {str(k): _json_ready(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(v) for v in value]
    return value


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        _json_ready(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _immutable_target_identity(target: dict[str, Any]) -> dict[str, Any]:
    kind = target.get("kind")
    if kind in {"price_target", _TERMINAL_CLOSE_KIND}:
        return {
            "kind": kind,
            "direction": target.get("direction"),
            "target_price": target.get("target_price"),
            "outcome_rule_version": target.get("outcome_rule_version"),
        }
    return dict(target)


def _immutable_claim_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    target = payload.get("forecast_target") or {}
    return _json_ready(
        {
            "contract_version": _IMMUTABLE_CLAIM_VERSION,
            "target_kind": target.get("kind"),
            "target_identity": _immutable_target_identity(target),
            "instrument_type": payload.get("instrument_type"),
            "symbol": payload.get("symbol"),
            "probability": payload.get("probability"),
            "probability_range_low": payload.get("probability_range_low"),
            "probability_range_high": payload.get("probability_range_high"),
            "forecast_start_date": payload.get("forecast_start_date"),
            "review_date": payload.get("review_date"),
            "horizon": payload.get("horizon"),
            "attribution": {
                "created_by": payload.get("created_by"),
                "session_label": payload.get("session_label"),
                "model_label": payload.get("model_label"),
                "policy_version": payload.get("policy_version"),
            },
            "origin_evidence_cutoff": {
                "artifact_uuid": payload.get("artifact_uuid"),
                "journal_id": payload.get("journal_id"),
                "report_uuid": payload.get("report_uuid"),
                "report_item_uuid": payload.get("report_item_uuid"),
                "correlation_id": payload.get("correlation_id"),
                "evidence_ids": payload.get("evidence_ids"),
                "contrary_evidence": payload.get("contrary_evidence"),
                "resolution_source": payload.get("resolution_source"),
            },
        }
    )


def _row_claim_payload(row: TradeForecast) -> dict[str, Any]:
    return {
        "created_by": row.created_by,
        "symbol": row.symbol,
        "instrument_type": row.instrument_type,
        "forecast_target": row.forecast_target,
        "probability": row.probability,
        "probability_range_low": row.probability_range_low,
        "probability_range_high": row.probability_range_high,
        "review_date": row.review_date,
        "forecast_start_date": row.forecast_start_date,
        "horizon": row.horizon,
        "evidence_ids": row.evidence_ids,
        "contrary_evidence": row.contrary_evidence,
        "resolution_source": row.resolution_source,
        "session_label": row.session_label,
        "model_label": row.model_label,
        "policy_version": row.policy_version,
        "artifact_uuid": row.artifact_uuid,
        "journal_id": row.journal_id,
        "report_uuid": row.report_uuid,
        "report_item_uuid": row.report_item_uuid,
        "correlation_id": row.correlation_id,
    }


def _original_target_kind(row: TradeForecast) -> str | None:
    claim = row.immutable_claim
    if isinstance(claim, dict) and isinstance(claim.get("target_kind"), str):
        return str(claim["target_kind"])
    target = row.forecast_target
    return target.get("kind") if isinstance(target, dict) else None


def _claim_integrity_failure(row: TradeForecast) -> str | None:
    original_kind = _original_target_kind(row)
    if original_kind not in {"price_target", _TERMINAL_CLOSE_KIND}:
        return None
    if row.target_version < 1 or not isinstance(row.immutable_claim, dict):
        return "typed forecast lacks immutable claim/version evidence"
    if not isinstance(row.immutable_claim_hash, str):
        return "typed forecast lacks immutable claim hash"
    if _canonical_hash(row.immutable_claim) != row.immutable_claim_hash:
        return "stored immutable claim hash does not match its snapshot"
    rebuilt = _immutable_claim_from_payload(_row_claim_payload(row))
    if rebuilt != row.immutable_claim:
        return "stored forecast fields no longer match immutable original claim"
    return None


def serialize_forecast(r: TradeForecast) -> dict[str, Any]:
    return {
        "id": r.id,
        "forecast_id": str(r.forecast_id),
        "artifact_uuid": r.artifact_uuid,
        "journal_id": r.journal_id,
        "report_uuid": r.report_uuid,
        "report_item_uuid": r.report_item_uuid,
        "correlation_id": r.correlation_id,
        "created_by": r.created_by,
        "session_label": r.session_label,
        "model_label": r.model_label,
        "policy_version": r.policy_version,
        "symbol": r.symbol,
        "instrument_type": (
            r.instrument_type.value
            if hasattr(r.instrument_type, "value")
            else str(r.instrument_type)
        ),
        "forecast_target": r.forecast_target,
        "immutable_claim": r.immutable_claim,
        "immutable_claim_hash": r.immutable_claim_hash,
        "target_version": r.target_version,
        "resolution_semantics_status": r.resolution_semantics_status,
        "semantics_evidence": r.semantics_evidence,
        "supersedes_forecast_id": (
            str(r.supersedes_forecast_id) if r.supersedes_forecast_id else None
        ),
        "superseded_by_forecast_id": (
            str(r.superseded_by_forecast_id) if r.superseded_by_forecast_id else None
        ),
        "horizon": r.horizon,
        "probability": float(r.probability) if r.probability is not None else None,
        "probability_range_low": (
            float(r.probability_range_low)
            if r.probability_range_low is not None
            else None
        ),
        "probability_range_high": (
            float(r.probability_range_high)
            if r.probability_range_high is not None
            else None
        ),
        "evidence_ids": r.evidence_ids,
        "contrary_evidence": r.contrary_evidence,
        "resolution_source": r.resolution_source,
        "forecast_start_date": (
            r.forecast_start_date.isoformat() if r.forecast_start_date else None
        ),
        "review_date": r.review_date.isoformat() if r.review_date else None,
        "status": r.status,
        "outcome": r.outcome,
        "observed_value": (
            float(r.observed_value) if r.observed_value is not None else None
        ),
        "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None,
        "brier_score": float(r.brier_score) if r.brier_score is not None else None,
        "resolution_detail": r.resolution_detail,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


# ---------------------------------------------------------------------------
# Repository — the only write surface
# ---------------------------------------------------------------------------
class ForecastRepository:
    """The only write surface for review.trade_forecasts."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_by_forecast_id(
        self, forecast_id: uuid.UUID, *, for_update: bool = False
    ) -> TradeForecast | None:
        stmt = select(TradeForecast).where(TradeForecast.forecast_id == forecast_id)
        if for_update:
            stmt = stmt.with_for_update()
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def create(self, payload: dict[str, Any]) -> TradeForecast:
        row = TradeForecast(**payload)
        self.db.add(row)
        await self.db.flush()
        return row


def _coerce_forecast_id(value: str | uuid.UUID | None) -> uuid.UUID:
    if value is None:
        return uuid.uuid4()
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError) as exc:
        raise ForecastValidationError(f"invalid forecast_id: {value!r}") from exc


def _validate_semantics_evidence(
    evidence: Any,
    *,
    contract_version: str,
    expected_source_target_hash: str,
    authenticated_actor: AuthenticatedForecastActor | None,
) -> dict[str, Any]:
    if not isinstance(evidence, dict):
        raise ForecastValidationError("typed semantics evidence is required")
    if set(evidence) != set(_SEMANTICS_EVIDENCE_INPUT_KEYS):
        raise ForecastValidationError(
            "semantics evidence fields must exactly match the typed contract"
        )
    if evidence.get("contract_version") != contract_version:
        raise ForecastValidationError(
            f"semantics evidence contract_version must be {contract_version!r}"
        )
    authority_type = evidence.get("authority_type")
    if authority_type not in {"operator", "service"}:
        raise ForecastValidationError(
            "semantics evidence authority_type must be operator or service"
        )
    actor = evidence.get("actor_principal")
    if not isinstance(actor, str) or _ACTOR_RE.fullmatch(actor) is None:
        raise ForecastValidationError(
            "semantics evidence actor_principal must be a typed user: or "
            "service: principal"
        )
    expected_prefix = "user:" if authority_type == "operator" else "service:"
    if not actor.startswith(expected_prefix):
        raise ForecastValidationError(
            "semantics evidence authority_type does not match actor_principal"
        )
    if evidence.get("authentication_method") not in _AUTHENTICATION_METHODS:
        raise ForecastValidationError(
            "semantics evidence authentication_method is invalid"
        )
    validated_actor = _validate_evidence_actor(
        evidence,
        authenticated_actor,
        context="semantics evidence",
    )
    if evidence.get("source_target_sha256") != expected_source_target_hash:
        raise ForecastValidationError(
            "semantics evidence source_target_sha256 does not match stored target"
        )
    evidence_hash = evidence.get("evidence_sha256")
    if (
        not isinstance(evidence_hash, str)
        or _SHA256_RE.fullmatch(evidence_hash) is None
    ):
        raise ForecastValidationError(
            "semantics evidence evidence_sha256 must be 64 lowercase hex characters"
        )
    for field in ("evidence_ref", "reason"):
        value = evidence.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ForecastValidationError(f"semantics evidence {field} is required")
    raw_attested_at = evidence.get("attested_at")
    if not isinstance(raw_attested_at, str):
        raise ForecastValidationError("semantics evidence attested_at is required")
    try:
        attested_at = datetime.fromisoformat(raw_attested_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ForecastValidationError(
            "semantics evidence attested_at must be RFC3339"
        ) from exc
    if attested_at.tzinfo is None:
        raise ForecastValidationError(
            "semantics evidence attested_at must include a timezone"
        )
    validated = _json_ready(dict(evidence))
    validated["authentication_binding"] = _evidence_authentication_binding(
        dict(evidence),
        validated_actor,
    )
    return validated


def _stored_adjustment_authentication_failure(
    row: TradeForecast,
    target: dict[str, Any],
) -> str | None:
    """Verify durable app-authenticated evidence before terminal resolution."""
    if target.get("price_adjustment_policy") != "explicit-factor-v1":
        return None
    provenance = target.get("adjustment_provenance")
    if not isinstance(provenance, dict):
        return "terminal adjustment provenance is missing"
    semantics_evidence = row.semantics_evidence
    if not isinstance(semantics_evidence, dict):
        return "terminal adjustment authentication evidence is missing"
    binding = semantics_evidence.get("adjustment_authentication")
    if not isinstance(binding, dict):
        return "terminal adjustment authentication binding is missing"
    expected = {
        "contract_version": _EVIDENCE_AUTHENTICATION_VERSION,
        "actor_principal": provenance.get("actor_principal"),
        "authentication_method": provenance.get("authentication_method"),
        "provenance_sha256": _canonical_hash(provenance),
    }
    if binding != expected:
        return "terminal adjustment authentication binding does not match provenance"
    return None


def _stored_semantics_authentication_failure(row: TradeForecast) -> str | None:
    """Verify app-authenticated attestation/supersession evidence, when present."""
    evidence = row.semantics_evidence
    if not isinstance(evidence, dict):
        return None
    contract_version = evidence.get("contract_version")
    if contract_version not in {
        _SEMANTICS_ATTESTATION_VERSION,
        _SEMANTICS_SUPERSESSION_VERSION,
    }:
        return None
    binding = evidence.get("authentication_binding")
    if not isinstance(binding, dict):
        return "forecast semantics authentication binding is missing"
    original_evidence = {
        key: evidence.get(key) for key in _SEMANTICS_EVIDENCE_INPUT_KEYS
    }
    expected = {
        "contract_version": _EVIDENCE_AUTHENTICATION_VERSION,
        "actor_principal": evidence.get("actor_principal"),
        "authentication_method": evidence.get("authentication_method"),
        "provenance_sha256": _canonical_hash(original_evidence),
    }
    if binding != expected:
        return "forecast semantics authentication binding does not match evidence"
    return None


_CLAIM_SCALAR_KEYS = (
    "created_by",
    "symbol",
    "instrument_type",
    "probability",
    "probability_range_low",
    "probability_range_high",
    "review_date",
    "forecast_start_date",
    "horizon",
    "evidence_ids",
    "contrary_evidence",
    "resolution_source",
    "session_label",
    "model_label",
    "policy_version",
    "artifact_uuid",
    "journal_id",
    "report_uuid",
    "report_item_uuid",
    "correlation_id",
)


def _merge_omitted_claim_fields(
    existing: TradeForecast, payload: dict[str, Any]
) -> dict[str, Any]:
    merged = dict(payload)
    for key in _CLAIM_SCALAR_KEYS:
        if merged.get(key) is None:
            merged[key] = getattr(existing, key)
    return merged


def _require_same_claim_scalars(
    existing: TradeForecast, payload: dict[str, Any], *, operation: str
) -> None:
    existing_payload = _row_claim_payload(existing)
    for key in _CLAIM_SCALAR_KEYS:
        if _json_ready(existing_payload.get(key)) != _json_ready(payload.get(key)):
            raise ForecastValidationError(
                f"{operation} cannot change immutable forecast field {key!r}"
            )


def _semantics_status_for_target(target: dict[str, Any]) -> str:
    if (
        target.get("kind") == _TERMINAL_CLOSE_KIND
        and target.get("price_adjustment_policy") != "explicit-factor-v1"
    ):
        return "quarantined"
    return "active"


async def save_forecast(
    db: AsyncSession,
    *,
    created_by: str,
    symbol: str,
    instrument_type: str,
    forecast_target: dict,
    probability: float,
    review_date: str | date,
    forecast_id: str | uuid.UUID | None = None,
    horizon: str | None = None,
    probability_range_low: float | None = None,
    probability_range_high: float | None = None,
    evidence_ids: list | None = None,
    contrary_evidence: str | None = None,
    forecast_start_date: str | date | None = None,
    resolution_source: str | None = None,
    session_label: str | None = None,
    model_label: str | None = None,
    policy_version: str | None = None,
    artifact_uuid: str | None = None,
    journal_id: int | None = None,
    report_uuid: str | None = None,
    report_item_uuid: str | None = None,
    correlation_id: str | None = None,
    expected_target_version: int | None = None,
    semantics_attestation: dict[str, Any] | None = None,
    supersedes_forecast_id: str | uuid.UUID | None = None,
    supersession_evidence: dict[str, Any] | None = None,
    authenticated_actor: AuthenticatedForecastActor | None = None,
) -> tuple[str, TradeForecast]:
    if not (created_by or "").strip():
        raise ForecastValidationError("created_by is required")
    if instrument_type not in _VALID_INSTRUMENTS:
        raise ForecastValidationError(f"invalid instrument_type: {instrument_type}")
    if not (symbol or "").strip():
        raise ForecastValidationError("symbol is required")
    try:
        prob = float(probability)
    except (TypeError, ValueError) as exc:
        raise ForecastValidationError("probability must be a number") from exc
    if not 0.0 <= prob <= 1.0:
        raise ForecastValidationError("probability must be within [0, 1]")

    lo = probability_range_low
    hi = probability_range_high
    if (lo is None) != (hi is None):
        raise ForecastValidationError(
            "probability_range requires both low and high (or neither)"
        )
    if lo is not None and hi is not None:
        lo_f, hi_f = float(lo), float(hi)
        if not (0.0 <= lo_f <= 1.0 and 0.0 <= hi_f <= 1.0):
            raise ForecastValidationError("probability_range must be within [0, 1]")
        if lo_f > hi_f:
            raise ForecastValidationError("probability_range_low must be <= high")
        if not lo_f <= prob <= hi_f:
            raise ForecastValidationError(
                "probability must fall within probability_range"
            )

    review = _parse_date(review_date, "review_date")
    start = (
        _parse_date(forecast_start_date, "forecast_start_date")
        if forecast_start_date is not None
        else None
    )
    if start is not None and start > review:
        raise ForecastValidationError("forecast_start_date must be <= review_date")

    normalized_symbol = _normalize_symbol(symbol, instrument_type)
    _validate_forecast_target(
        forecast_target,
        instrument_type=instrument_type,
        review_date=review,
        symbol=normalized_symbol,
        forecast_start_date=start,
        authenticated_actor=authenticated_actor,
    )

    repo = ForecastRepository(db)
    coerced_forecast_id = _coerce_forecast_id(forecast_id)
    existing = (
        await repo.get_by_forecast_id(coerced_forecast_id, for_update=True)
        if forecast_id is not None
        else None
    )
    effective_policy_version = (
        existing.policy_version
        if existing is not None and policy_version is None
        else policy_version or _default_policy_version()
    )
    payload: dict[str, Any] = {
        "forecast_id": coerced_forecast_id,
        "created_by": created_by.strip(),
        "symbol": normalized_symbol,
        "instrument_type": instrument_type,
        "forecast_target": dict(forecast_target),
        "probability": _to_decimal(prob),
        "probability_range_low": _to_decimal(lo),
        "probability_range_high": _to_decimal(hi),
        "review_date": review,
        "forecast_start_date": start,
        "horizon": horizon,
        "evidence_ids": evidence_ids,
        "contrary_evidence": contrary_evidence,
        "resolution_source": resolution_source,
        "session_label": session_label,
        "model_label": model_label,
        "policy_version": effective_policy_version,
        "artifact_uuid": artifact_uuid,
        "journal_id": journal_id,
        "report_uuid": report_uuid,
        "report_item_uuid": report_item_uuid,
        "correlation_id": correlation_id,
        "status": "open",
    }

    if supersession_evidence is not None and supersedes_forecast_id is None:
        raise ForecastValidationError(
            "supersession_evidence requires supersedes_forecast_id"
        )

    if existing is not None:
        if supersedes_forecast_id is not None:
            expected_supersedes = _coerce_forecast_id(supersedes_forecast_id)
            if existing.supersedes_forecast_id != expected_supersedes:
                raise ForecastValidationError(
                    "existing forecast supersession link does not match replay"
                )
        if existing.status != "open":
            raise ForecastValidationError(
                "cannot modify a closed (resolved) forecast; "
                f"forecast_id={coerced_forecast_id}"
            )

        original_kind = _original_target_kind(existing)
        current_target = (
            existing.forecast_target
            if isinstance(existing.forecast_target, dict)
            else {}
        )

        if original_kind == _TERMINAL_CLOSE_KIND:
            integrity_failure = _claim_integrity_failure(existing)
            if integrity_failure is not None:
                raise ForecastValidationError(
                    f"terminal immutable claim integrity failure: {integrity_failure}"
                )
            merged = _merge_omitted_claim_fields(existing, payload)
            _require_same_claim_scalars(
                existing, merged, operation="terminal immutable replay"
            )
            candidate_claim = _immutable_claim_from_payload(merged)
            if candidate_claim != existing.immutable_claim:
                raise ForecastValidationError(
                    "terminal immutable claim cannot change kind, instrument, symbol, "
                    "direction, target, probability, dates, attribution, or origin"
                )
            if merged["forecast_target"] == current_target:
                return "unchanged", existing

            old_policy = current_target.get("price_adjustment_policy")
            new_policy = merged["forecast_target"].get("price_adjustment_policy")
            if not (
                old_policy == "unverified_fail_closed"
                and new_policy == "explicit-factor-v1"
            ):
                raise ForecastValidationError(
                    "terminal immutable target only permits "
                    "unverified_fail_closed -> explicit-factor-v1 promotion"
                )
            if expected_target_version != existing.target_version:
                raise ForecastValidationError(
                    "terminal adjustment promotion target_version CAS mismatch"
                )
            existing.forecast_target = merged["forecast_target"]
            existing.target_version += 1
            existing.resolution_semantics_status = "active"
            prior_semantics = (
                dict(existing.semantics_evidence)
                if isinstance(existing.semantics_evidence, dict)
                else {}
            )
            promotion_evidence = {
                "adjustment_transition": ("unverified_fail_closed->explicit-factor-v1"),
                "adjustment_target_version": existing.target_version,
                "adjustment_provenance": merged["forecast_target"].get(
                    "adjustment_provenance"
                ),
                "adjustment_authentication": _evidence_authentication_binding(
                    merged["forecast_target"]["adjustment_provenance"],
                    _validate_evidence_actor(
                        merged["forecast_target"]["adjustment_provenance"],
                        authenticated_actor,
                        context="terminal_close.adjustment_provenance",
                    ),
                ),
                "adjustment_stored_at": now_kst().isoformat(),
            }
            if prior_semantics:
                existing.semantics_evidence = {
                    **prior_semantics,
                    **promotion_evidence,
                }
            else:
                existing.semantics_evidence = {
                    "contract_version": _TERMINAL_ADJUSTMENT_EVIDENCE_VERSION,
                    "transition": promotion_evidence["adjustment_transition"],
                    "target_version": promotion_evidence["adjustment_target_version"],
                    "stored_at": promotion_evidence["adjustment_stored_at"],
                    **promotion_evidence,
                }
            await db.flush()
            return "updated", existing

        if (
            current_target.get("kind") == "price_target"
            and current_target.get("outcome_rule_version") is None
        ):
            if expected_target_version != 0:
                raise ForecastValidationError(
                    "legacy touch attestation requires expected_target_version=0"
                )
            source_target_hash = _canonical_hash(current_target)
            evidence = _validate_semantics_evidence(
                semantics_attestation,
                contract_version=_SEMANTICS_ATTESTATION_VERSION,
                expected_source_target_hash=source_target_hash,
                authenticated_actor=authenticated_actor,
            )
            candidate_without_version = dict(payload["forecast_target"])
            candidate_without_version.pop("outcome_rule_version", None)
            if candidate_without_version != current_target:
                raise ForecastValidationError(
                    "touch attestation cannot change the stored legacy target"
                )
            merged = _merge_omitted_claim_fields(existing, payload)
            _require_same_claim_scalars(
                existing, merged, operation="legacy touch attestation"
            )
            immutable_claim = _immutable_claim_from_payload(merged)
            existing.forecast_target = merged["forecast_target"]
            existing.immutable_claim = immutable_claim
            existing.immutable_claim_hash = _canonical_hash(immutable_claim)
            existing.target_version = 1
            existing.resolution_semantics_status = "active"
            existing.semantics_evidence = {
                **evidence,
                "decision": "window_touch",
                "from_outcome_rule_version": None,
                "to_outcome_rule_version": _PRICE_TOUCH_RULE_VERSION,
                "stored_at": now_kst().isoformat(),
            }
            await db.flush()
            return "updated", existing

        if original_kind == "price_target":
            merged = _merge_omitted_claim_fields(existing, payload)
            if _immutable_target_identity(
                merged["forecast_target"]
            ) != _immutable_target_identity(current_target):
                raise ForecastValidationError(
                    "versioned price_target identity is immutable"
                )
            changed = any(
                _json_ready(getattr(existing, key, None)) != _json_ready(value)
                for key, value in merged.items()
                if key != "status"
            )
            if not changed:
                return "unchanged", existing
            for key, value in merged.items():
                setattr(existing, key, value)
            immutable_claim = _immutable_claim_from_payload(merged)
            existing.immutable_claim = immutable_claim
            existing.immutable_claim_hash = _canonical_hash(immutable_claim)
            existing.target_version += 1
            existing.resolution_semantics_status = "active"
            await db.flush()
            return "updated", existing

        for key, value in payload.items():
            setattr(existing, key, value)
        await db.flush()
        return "updated", existing

    if semantics_attestation is not None:
        raise ForecastValidationError(
            "semantics_attestation applies only to an existing legacy price_target"
        )

    superseded: TradeForecast | None = None
    durable_supersession: dict[str, Any] | None = None
    if supersedes_forecast_id is not None:
        if forecast_target.get("kind") != _TERMINAL_CLOSE_KIND:
            raise ForecastValidationError(
                "supersedes_forecast_id requires a new terminal_close target"
            )
        superseded_id = _coerce_forecast_id(supersedes_forecast_id)
        if superseded_id == coerced_forecast_id:
            raise ForecastValidationError("a forecast cannot supersede itself")
        superseded = await repo.get_by_forecast_id(superseded_id, for_update=True)
        if superseded is None:
            raise ForecastValidationError(
                f"superseded forecast not found: {superseded_id}"
            )
        old_target = (
            superseded.forecast_target
            if isinstance(superseded.forecast_target, dict)
            else {}
        )
        if (
            superseded.status != "open"
            or old_target.get("kind") != "price_target"
            or old_target.get("outcome_rule_version") is not None
            or superseded.superseded_by_forecast_id is not None
        ):
            raise ForecastValidationError(
                "terminal supersession requires one open, unsuperseded, "
                "versionless legacy price_target"
            )
        source_target_hash = _canonical_hash(old_target)
        evidence = _validate_semantics_evidence(
            supersession_evidence,
            contract_version=_SEMANTICS_SUPERSESSION_VERSION,
            expected_source_target_hash=source_target_hash,
            authenticated_actor=authenticated_actor,
        )
        if policy_version is None:
            payload["policy_version"] = superseded.policy_version
        payload = _merge_omitted_claim_fields(superseded, payload)
        _require_same_claim_scalars(
            superseded, payload, operation="terminal supersession"
        )
        expected_direction = {
            "at_or_above": "up",
            "at_or_below": "down",
        }.get(old_target.get("direction"))
        if forecast_target.get("direction") != expected_direction or float(
            forecast_target.get("target_price")
        ) != float(old_target.get("target_price")):
            raise ForecastValidationError(
                "terminal supersession must preserve threshold and map touch "
                "direction to its terminal complement"
            )
        durable_supersession = {
            **evidence,
            "decision": "terminal_close",
            "from_forecast_id": str(superseded.forecast_id),
            "to_forecast_id": str(coerced_forecast_id),
            "from_outcome_rule_version": None,
            "to_outcome_rule_version": _TERMINAL_CLOSE_RULE_VERSION,
            "stored_at": now_kst().isoformat(),
        }
        payload["supersedes_forecast_id"] = superseded.forecast_id

    target_kind = forecast_target.get("kind")
    if target_kind in {"price_target", _TERMINAL_CLOSE_KIND}:
        immutable_claim = _immutable_claim_from_payload(payload)
        adjustment_authentication: dict[str, Any] | None = None
        if (
            target_kind == _TERMINAL_CLOSE_KIND
            and forecast_target.get("price_adjustment_policy") == "explicit-factor-v1"
        ):
            validated_actor = _validate_evidence_actor(
                forecast_target["adjustment_provenance"],
                authenticated_actor,
                context="terminal_close.adjustment_provenance",
            )
            adjustment_authentication = _evidence_authentication_binding(
                forecast_target["adjustment_provenance"],
                validated_actor,
            )
            if durable_supersession is None:
                durable_supersession = {
                    "contract_version": _TERMINAL_ADJUSTMENT_EVIDENCE_VERSION,
                    "transition": "initial-explicit-factor-v1",
                    "target_version": 1,
                    "stored_at": now_kst().isoformat(),
                }
            durable_supersession["adjustment_authentication"] = (
                adjustment_authentication
            )
        payload.update(
            {
                "immutable_claim": immutable_claim,
                "immutable_claim_hash": _canonical_hash(immutable_claim),
                "target_version": 1,
                "resolution_semantics_status": _semantics_status_for_target(
                    forecast_target
                ),
                "semantics_evidence": durable_supersession,
            }
        )

    row = await repo.create(payload)
    if superseded is not None and durable_supersession is not None:
        superseded.superseded_by_forecast_id = row.forecast_id
        superseded.resolution_semantics_status = "superseded"
        superseded.semantics_evidence = durable_supersession
        await db.flush()
    return "created", row


async def get_forecast(
    db: AsyncSession, forecast_id: str | uuid.UUID
) -> TradeForecast | None:
    return await ForecastRepository(db).get_by_forecast_id(
        _coerce_forecast_id(forecast_id)
    )


async def list_due_forecasts(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    limit: int = 50,
) -> list[TradeForecast]:
    today = (now or now_kst()).astimezone(_KST).date()
    kind = TradeForecast.forecast_target["kind"].astext
    rule_version = TradeForecast.forecast_target["outcome_rule_version"].astext
    adjustment_policy = TradeForecast.forecast_target["price_adjustment_policy"].astext
    adjustment_binding_version = TradeForecast.semantics_evidence[
        "adjustment_authentication"
    ]["contract_version"].astext
    semantics_contract = TradeForecast.semantics_evidence["contract_version"].astext
    semantics_binding_version = TradeForecast.semantics_evidence[
        "authentication_binding"
    ]["contract_version"].astext
    no_semantics_evidence = or_(
        TradeForecast.semantics_evidence.is_(None),
        func.jsonb_typeof(TradeForecast.semantics_evidence) == "null",
    )
    typed_touch = and_(
        kind == "price_target",
        rule_version == _PRICE_TOUCH_RULE_VERSION,
        TradeForecast.target_version >= 1,
        TradeForecast.immutable_claim.is_not(None),
        TradeForecast.resolution_semantics_status == "active",
        or_(
            no_semantics_evidence,
            and_(
                semantics_contract == _SEMANTICS_ATTESTATION_VERSION,
                semantics_binding_version == _EVIDENCE_AUTHENTICATION_VERSION,
            ),
        ),
    )
    verified_terminal = and_(
        kind == _TERMINAL_CLOSE_KIND,
        rule_version == _TERMINAL_CLOSE_RULE_VERSION,
        adjustment_policy == "explicit-factor-v1",
        adjustment_binding_version == _EVIDENCE_AUTHENTICATION_VERSION,
        or_(
            semantics_contract == _TERMINAL_ADJUSTMENT_EVIDENCE_VERSION,
            and_(
                semantics_contract == _SEMANTICS_SUPERSESSION_VERSION,
                semantics_binding_version == _EVIDENCE_AUTHENTICATION_VERSION,
            ),
        ),
        TradeForecast.target_version >= 1,
        TradeForecast.immutable_claim.is_not(None),
        TradeForecast.resolution_semantics_status == "active",
    )
    existing_generic = and_(
        kind.is_not(None),
        kind.notin_(("price_target", _TERMINAL_CLOSE_KIND)),
    )
    stmt = (
        select(TradeForecast)
        .where(
            TradeForecast.status == "open",
            TradeForecast.review_date <= today,
            TradeForecast.superseded_by_forecast_id.is_(None),
            or_(typed_touch, verified_terminal, existing_generic),
        )
        .order_by(TradeForecast.review_date.asc())
        .limit(limit)
    )
    return list((await db.execute(stmt)).scalars().all())


async def list_due_quarantined_forecasts(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    limit: int = 50,
) -> list[TradeForecast]:
    """Diagnostic queue that never consumes the eligible due-row limit."""
    today = (now or now_kst()).astimezone(_KST).date()
    kind = TradeForecast.forecast_target["kind"].astext
    rule_version = TradeForecast.forecast_target["outcome_rule_version"].astext
    adjustment_policy = TradeForecast.forecast_target["price_adjustment_policy"].astext
    adjustment_binding_version = TradeForecast.semantics_evidence[
        "adjustment_authentication"
    ]["contract_version"].astext
    semantics_contract = TradeForecast.semantics_evidence["contract_version"].astext
    semantics_binding_version = TradeForecast.semantics_evidence[
        "authentication_binding"
    ]["contract_version"].astext
    has_semantics_evidence = and_(
        TradeForecast.semantics_evidence.is_not(None),
        func.jsonb_typeof(TradeForecast.semantics_evidence).is_distinct_from("null"),
    )
    invalid_touch = and_(
        kind == "price_target",
        or_(
            rule_version.is_distinct_from(_PRICE_TOUCH_RULE_VERSION),
            TradeForecast.target_version < 1,
            TradeForecast.immutable_claim.is_(None),
            TradeForecast.resolution_semantics_status.is_distinct_from("active"),
            and_(
                has_semantics_evidence,
                or_(
                    semantics_contract.is_distinct_from(_SEMANTICS_ATTESTATION_VERSION),
                    semantics_binding_version.is_distinct_from(
                        _EVIDENCE_AUTHENTICATION_VERSION
                    ),
                ),
            ),
        ),
    )
    invalid_terminal = and_(
        kind == _TERMINAL_CLOSE_KIND,
        or_(
            rule_version.is_distinct_from(_TERMINAL_CLOSE_RULE_VERSION),
            adjustment_policy.is_distinct_from("explicit-factor-v1"),
            adjustment_binding_version.is_distinct_from(
                _EVIDENCE_AUTHENTICATION_VERSION
            ),
            and_(
                semantics_contract.is_distinct_from(
                    _TERMINAL_ADJUSTMENT_EVIDENCE_VERSION
                ),
                or_(
                    semantics_contract.is_distinct_from(
                        _SEMANTICS_SUPERSESSION_VERSION
                    ),
                    semantics_binding_version.is_distinct_from(
                        _EVIDENCE_AUTHENTICATION_VERSION
                    ),
                ),
            ),
            TradeForecast.target_version < 1,
            TradeForecast.immutable_claim.is_(None),
            TradeForecast.resolution_semantics_status.is_distinct_from("active"),
        ),
    )
    stmt = (
        select(TradeForecast)
        .where(
            TradeForecast.status == "open",
            TradeForecast.review_date <= today,
            TradeForecast.superseded_by_forecast_id.is_(None),
            or_(kind.is_(None), invalid_touch, invalid_terminal),
        )
        .order_by(TradeForecast.review_date.asc())
        .limit(limit)
    )
    return list((await db.execute(stmt)).scalars().all())


async def list_forecasts(
    db: AsyncSession,
    *,
    status: str | None = None,
    symbol: str | None = None,
    created_by: str | None = None,
    correlation_id: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    filters = []
    if status is not None:
        filters.append(TradeForecast.status == status)
    if symbol is not None:
        filters.append(TradeForecast.symbol == _normalize_symbol_for_filter(symbol))
    if created_by is not None:
        filters.append(TradeForecast.created_by == created_by)
    if correlation_id is not None:
        filters.append(TradeForecast.correlation_id == correlation_id)
    stmt = (
        select(TradeForecast)
        .where(*filters)
        .order_by(TradeForecast.created_at.desc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).scalars().all()
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    return {
        "entries": [serialize_forecast(r) for r in rows],
        "summary": {"count": len(rows), "by_status": by_status},
    }


def _forecast_scope_filters(
    *,
    status: str | None,
    symbol: str | None,
    created_by: str | None,
    instrument_type: str | None,
) -> list[Any]:
    filters: list[Any] = []
    if status is not None:
        filters.append(TradeForecast.status == status)
    if symbol is not None:
        filters.append(
            TradeForecast.symbol
            == _normalize_symbol_for_filter(symbol, instrument_type)
        )
    if created_by is not None:
        filters.append(TradeForecast.created_by == created_by)
    if instrument_type is not None:
        filters.append(TradeForecast.instrument_type == instrument_type)
    return filters


async def _run_forecast_listing(
    db: AsyncSession, *, filters: list[Any], order_by: Any, limit: int
) -> dict[str, Any]:
    stmt = select(TradeForecast).where(*filters).order_by(order_by).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    return {
        "entries": [serialize_forecast(r) for r in rows],
        "summary": {"count": len(rows), "by_status": by_status},
    }


async def list_open_forecasts(
    db: AsyncSession,
    *,
    symbol: str | None = None,
    created_by: str | None = None,
    instrument_type: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Open forecasts ordered by ``review_date`` ASC — the scoring-due queue (ROB-663).

    Soonest (and overdue) review dates sort first so the web surface can show the
    "채점 due 대기열". Ordering is done in SQL so ``limit`` selects the most imminent
    rows rather than merely the most-recently-created ones.
    """
    filters = _forecast_scope_filters(
        status="open",
        symbol=symbol,
        created_by=created_by,
        instrument_type=instrument_type,
    )
    return await _run_forecast_listing(
        db, filters=filters, order_by=TradeForecast.review_date.asc(), limit=limit
    )


async def list_closed_forecasts(
    db: AsyncSession,
    *,
    symbol: str | None = None,
    created_by: str | None = None,
    instrument_type: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Closed/scored forecasts ordered by ``resolved_at`` DESC — recent scoring
    history with ``outcome``/``brier_score`` populated (ROB-663)."""
    filters = _forecast_scope_filters(
        status="closed",
        symbol=symbol,
        created_by=created_by,
        instrument_type=instrument_type,
    )
    return await _run_forecast_listing(
        db,
        filters=filters,
        order_by=TradeForecast.resolved_at.desc().nulls_last(),
        limit=limit,
    )


_BACKFILL_HORIZON_BARS = 200


async def _resolve_candle_partition(
    db: AsyncSession, *, symbol: str, instrument_type: str
) -> tuple[MarketKey, str] | None:
    """Resolve (market, partition) for the daily-candle store.

    Single source of truth so the resolution read (_read_window_candles) and the
    lazy backfill (_backfill_daily_candles) always use the SAME partition string
    — crypto in particular must be "upbit_krw" so both sides resolve the same
    crypto_instruments row (repository.py:141). Returns None when the US exchange
    lookup fails or the instrument has no daily store. ROB-712.
    """
    if instrument_type == "equity_kr":
        return MarketKey.KR, "KRX"
    if instrument_type == "crypto":
        return MarketKey.CRYPTO, "upbit_krw"
    if instrument_type == "equity_us":
        from app.services.us_symbol_universe_service import get_us_exchange_by_symbol

        try:
            partition = await get_us_exchange_by_symbol(symbol, db=db)
        except Exception:
            return None
        if not partition:
            return None
        return MarketKey.US, partition
    return None


async def _backfill_daily_candles(
    *,
    symbol: str,
    market: MarketKey,
    partition: str,
    horizon_bars: int = _BACKFILL_HORIZON_BARS,
) -> int:
    """Best-effort one-symbol daily-candle fetch+persist for a not-yet-loaded
    (typically rejected/non-held) symbol so its price_target forecast can
    resolve. Uses the shared sync service on its OWN session (commits+closes via
    close_callbacks). Never raises — returns 0 on any failure so resolve stays
    graceful (unresolved_no_data). ROB-712.
    """
    from app.services.daily_candles.sync_service import (
        SyncTarget,
        _build_default_service,
    )

    try:
        service = await _build_default_service()
    except Exception:
        logger.exception("ROB-712 backfill: service build failed symbol=%s", symbol)
        return 0
    try:
        result = await service.sync_one(
            target=SyncTarget(market=market, symbol=symbol, partition=partition),
            horizon_bars=horizon_bars,
        )
        return result.rows_upserted
    except Exception:
        logger.exception("ROB-712 backfill: sync_one failed symbol=%s", symbol)
        return 0
    finally:
        await service.close()


async def _read_window_candles(
    db: AsyncSession,
    *,
    symbol: str,
    instrument_type: str,
    start_date: date,
    review_date: date,
    for_share: bool = False,
) -> list[DailyCandleRow] | None:
    """Read loaded daily candles within [start_date, review_date] (inclusive).

    Returns ``None`` when the instrument's partition cannot be resolved (US
    exchange lookup failure) so the caller can mark the forecast unresolved
    rather than scoring against an empty window.
    """
    resolved = await _resolve_candle_partition(
        db, symbol=symbol, instrument_type=instrument_type
    )
    if resolved is None:
        return None
    market, partition = resolved

    # Pad the UTC window by 2 days each side to absorb tz/session boundary skew,
    # then filter by the candle's calendar date for a clean inclusive window.
    start_dt = datetime.combine(
        start_date - timedelta(days=2), dt.time(0, 0), tzinfo=dt.UTC
    )
    end_dt = datetime.combine(
        review_date + timedelta(days=2), dt.time(23, 59, 59), tzinfo=dt.UTC
    )
    repo = DailyCandlesRepository(session=db)
    rows = await repo.fetch_range(
        market=market,
        symbol=symbol,
        partition=partition,
        start=start_dt,
        end=end_dt,
        for_share=for_share,
    )
    return [r for r in rows if start_date <= _row_date(r) <= review_date]


def _terminal_close_session_failure(
    *,
    instrument_type: str,
    review_date: date,
    now: datetime,
) -> str | None:
    """Return a fail-closed reason unless the review session is final."""
    from app.services.daily_candles.read_service import (
        get_calendar,
        last_final_session_kr,
        last_final_session_us,
    )

    calendar_name = "XKRX" if instrument_type == "equity_kr" else "XNYS"
    try:
        if not bool(get_calendar(calendar_name).is_session(review_date.isoformat())):
            return (
                f"review_date={review_date.isoformat()} is not a "
                f"{calendar_name} regular session"
            )
    except Exception as exc:
        return f"could not verify {calendar_name} review session: {exc}"

    last_final = (
        last_final_session_kr(now)
        if instrument_type == "equity_kr"
        else last_final_session_us(now)
    )
    if last_final is None:
        return f"could not determine the latest final {calendar_name} session"
    if review_date > last_final:
        return (
            f"review session {review_date.isoformat()} is not final; "
            f"latest_final_session={last_final.isoformat()}"
        )
    return None


def _terminal_resolution_evidence(
    row: TradeForecast,
    target: dict[str, Any],
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "target_kind": _TERMINAL_CLOSE_KIND,
        "outcome_rule_version": target.get("outcome_rule_version"),
        "direction": target.get("direction"),
        "target_price": float(target.get("target_price")),
        "review_date": row.review_date.isoformat(),
        "price_adjustment_policy": target.get("price_adjustment_policy"),
    }
    if target.get("target_to_close_factor") is not None:
        evidence["target_to_close_factor"] = float(target.get("target_to_close_factor"))
    if target.get("adjustment_provenance") is not None:
        evidence["adjustment_provenance"] = target.get("adjustment_provenance")
    return evidence


def _terminal_candle_finality_failure(
    candle: DailyCandleRow,
    *,
    instrument_type: str,
    review_date: date,
) -> str | None:
    ingested_at = candle.ingested_at
    if ingested_at is None:
        return "review-date candle has no actual ingestion timestamp"
    if ingested_at.tzinfo is None:
        ingested_at = ingested_at.replace(tzinfo=dt.UTC)

    if instrument_type == "equity_kr":
        final_gate = datetime.combine(
            review_date, dt.time(15, 35), tzinfo=_KST
        ).astimezone(dt.UTC)
    else:
        import pandas as pd

        from app.services.daily_candles.read_service import get_calendar

        try:
            final_gate = (
                get_calendar("XNYS")
                .session_close(pd.Timestamp(review_date))
                .to_pydatetime()
            )
        except Exception as exc:
            return f"could not determine review-session close timestamp: {exc}"
        if final_gate.tzinfo is None:
            final_gate = final_gate.replace(tzinfo=dt.UTC)

    ingested_utc = ingested_at.astimezone(dt.UTC)
    final_gate_utc = final_gate.astimezone(dt.UTC)
    before_final = (
        ingested_utc < final_gate_utc
        if instrument_type == "equity_kr"
        else ingested_utc <= final_gate_utc
    )
    if before_final:
        return (
            f"candle ingested_at={ingested_at.isoformat()} does not follow final gate "
            f"{final_gate.isoformat()}"
        )
    return None


def _candle_fingerprint_payload(candle: DailyCandleRow) -> dict[str, Any]:
    return {
        "source_date": _row_date(candle).isoformat(),
        "time_utc": candle.time_utc.isoformat(),
        "symbol": candle.symbol,
        "partition": candle.partition,
        "open": candle.open,
        "high": candle.high,
        "low": candle.low,
        "close": candle.close,
        "adj_close": candle.adj_close,
        "volume": candle.volume,
        "value": candle.value,
        "source": candle.source,
        "is_final": candle.is_final,
        "session_scope": candle.session_scope,
        "source_row_id": candle.source_row_id,
        "source_row_version": candle.source_row_version,
        "price_basis": candle.price_basis,
        "ingested_at": (candle.ingested_at.isoformat() if candle.ingested_at else None),
    }


def _resolution_contract(
    row: TradeForecast,
    *,
    target: dict[str, Any],
    candle_evidence: Any,
) -> dict[str, Any]:
    evidence_fingerprint = _canonical_hash(candle_evidence)
    fingerprint_payload = {
        "forecast_id": str(row.forecast_id),
        "target_version": row.target_version,
        "immutable_claim_hash": row.immutable_claim_hash,
        "target_hash": _canonical_hash(target),
        "evidence_fingerprint": evidence_fingerprint,
    }
    return {
        "target_kind": target.get("kind"),
        "outcome_rule_version": target.get("outcome_rule_version"),
        "target_version": row.target_version,
        "immutable_claim_hash": row.immutable_claim_hash,
        "target_hash": fingerprint_payload["target_hash"],
        "evidence_fingerprint": evidence_fingerprint,
        "resolution_fingerprint": _canonical_hash(fingerprint_payload),
    }


def _resolution_cas_failure(
    contract: dict[str, Any],
    *,
    expected_target_version: int | None,
    expected_claim_hash: str | None,
    expected_resolution_fingerprint: str | None,
) -> str | None:
    if (
        expected_target_version is None
        or expected_claim_hash is None
        or expected_resolution_fingerprint is None
    ):
        return (
            "persist requires expected_target_version, expected_claim_hash, and "
            "expected_resolution_fingerprint from a dry-run preview"
        )
    if expected_target_version != contract["target_version"]:
        return "expected target_version no longer matches stored target"
    if expected_claim_hash != contract["immutable_claim_hash"]:
        return "expected immutable claim hash no longer matches stored claim"
    if expected_resolution_fingerprint != contract["resolution_fingerprint"]:
        return "expected candle/evidence fingerprint no longer matches current data"
    return None


def _typed_fail_closed(
    row: TradeForecast,
    *,
    status: str,
    reason: str,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "changed": False,
        "reason": reason,
        "resolution_evidence": evidence or {},
        "forecast": serialize_forecast(row),
    }


async def resolve_forecast(
    db: AsyncSession,
    *,
    forecast_id: str | uuid.UUID,
    persist: bool,
    manual_outcome: bool | None = None,
    manual_observed_value: float | None = None,
    manual_evidence: Any | None = None,
    now: datetime | None = None,
    backfill_missing: bool = True,
    expected_target_version: int | None = None,
    expected_claim_hash: str | None = None,
    expected_resolution_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Resolve one forecast. Idempotent: a closed forecast is never re-scored.

    ``persist=False`` computes the outcome/Brier and returns a preview without
    mutating the row (the dry-run default at the tool boundary). Price-target
    forecasts retain their window-touch OHLCV semantics. Terminal-close
    forecasts use exactly one final review-date regular-session ``close`` under
    their typed outcome/adjustment contract. Other kinds require
    ``manual_outcome`` + ``manual_evidence``.
    """
    repo = ForecastRepository(db)
    row = await repo.get_by_forecast_id(
        _coerce_forecast_id(forecast_id), for_update=persist
    )
    if row is None:
        raise ForecastValidationError(f"forecast not found: {forecast_id}")
    if row.status != "open":
        return {
            "status": "already_closed",
            "changed": False,
            "forecast": serialize_forecast(row),
        }

    resolved_now = now or now_kst()
    target = row.forecast_target or {}
    kind = target.get("kind")
    instrument = (
        row.instrument_type.value
        if hasattr(row.instrument_type, "value")
        else str(row.instrument_type)
    )
    original_kind = _original_target_kind(row)
    resolution_contract: dict[str, Any] | None = None

    if (
        row.resolution_semantics_status == "superseded"
        or row.superseded_by_forecast_id is not None
    ):
        return _typed_fail_closed(
            row,
            status="superseded",
            reason="forecast was durably superseded and cannot be resolved",
            evidence={
                "target_kind": original_kind,
                "superseded_by_forecast_id": (
                    str(row.superseded_by_forecast_id)
                    if row.superseded_by_forecast_id
                    else None
                ),
                "semantics_evidence": row.semantics_evidence,
            },
        )

    if original_kind == _TERMINAL_CLOSE_KIND:
        if kind in {"price_target", _TERMINAL_CLOSE_KIND}:
            try:
                _validate_forecast_target(
                    target,
                    instrument_type=instrument,
                    review_date=row.review_date,
                    symbol=row.symbol,
                    forecast_start_date=row.forecast_start_date,
                    require_authenticated_actor=False,
                )
            except ForecastValidationError as exc:
                return _typed_fail_closed(
                    row,
                    status="quarantined_invalid_target",
                    reason=str(exc),
                    evidence={
                        "original_target_kind": original_kind,
                        "stored_target": target,
                    },
                )
        integrity_failure = _claim_integrity_failure(row)
        if integrity_failure is not None:
            return _typed_fail_closed(
                row,
                status="quarantined_claim_integrity",
                reason=integrity_failure,
                evidence={
                    "original_target_kind": original_kind,
                    "current_target_kind": kind,
                    "immutable_claim_hash": row.immutable_claim_hash,
                    "target_version": row.target_version,
                },
            )
    elif kind == "price_target" and target.get("outcome_rule_version") is None:
        return _typed_fail_closed(
            row,
            status="quarantined_legacy_price_target",
            reason=(
                "versionless price_target has ambiguous touch-vs-terminal intent; "
                "explicit touch attestation or terminal supersession is required"
            ),
            evidence={
                "target_kind": "price_target",
                "outcome_rule_version": None,
                "target_hash": _canonical_hash(target),
                "target_version": row.target_version,
            },
        )
    elif kind in {"price_target", _TERMINAL_CLOSE_KIND}:
        try:
            _validate_forecast_target(
                target,
                instrument_type=instrument,
                review_date=row.review_date,
                symbol=row.symbol,
                forecast_start_date=row.forecast_start_date,
                require_authenticated_actor=False,
            )
        except ForecastValidationError as exc:
            return _typed_fail_closed(
                row,
                status="quarantined_invalid_target",
                reason=str(exc),
                evidence={"stored_target": target},
            )
        integrity_failure = _claim_integrity_failure(row)
        if integrity_failure is not None:
            return _typed_fail_closed(
                row,
                status="quarantined_claim_integrity",
                reason=integrity_failure,
                evidence={
                    "original_target_kind": original_kind,
                    "immutable_claim_hash": row.immutable_claim_hash,
                    "target_version": row.target_version,
                },
            )

    semantics_authentication_failure = _stored_semantics_authentication_failure(row)
    if semantics_authentication_failure is not None:
        return _typed_fail_closed(
            row,
            status="quarantined_untrusted_semantics_evidence",
            reason=semantics_authentication_failure,
            evidence={
                "original_target_kind": original_kind,
                "target_version": row.target_version,
                "semantics_evidence": row.semantics_evidence,
            },
        )

    if kind == _NO_RESOLVABLE_FORECAST_KIND:
        reason = "placeholder has no resolvable claim"
        if not persist:
            return {
                "status": "would_close_no_claim",
                "changed": False,
                "auto_close": True,
                "computed": None,
                "reason": reason,
                "forecast": serialize_forecast(row),
            }

        row.resolution_source = "not_applicable"
        row.resolution_detail = {
            "resolved_kind": kind,
            "reason": reason,
        }
        row.resolved_at = resolved_now
        row.status = _CLOSED_NO_CLAIM_STATUS
        await db.flush()
        await db.refresh(row)
        return {
            "status": _CLOSED_NO_CLAIM_STATUS,
            "changed": True,
            "auto_close": True,
            "computed": None,
            "reason": reason,
            "forecast": serialize_forecast(row),
        }

    if original_kind == _TERMINAL_CLOSE_KIND and manual_outcome is not None:
        return _typed_fail_closed(
            row,
            status="manual_resolution_forbidden",
            reason=(
                "immutable original terminal_close semantics do not accept a "
                "free-form manual outcome"
            ),
            evidence={
                "original_target_kind": original_kind,
                "target_version": row.target_version,
                "immutable_claim_hash": row.immutable_claim_hash,
            },
        )

    if manual_outcome is not None:
        if not manual_evidence:
            raise ForecastValidationError(
                "manual resolution requires evidence (manual_evidence)"
            )
        outcome = bool(manual_outcome)
        observed = manual_observed_value
        resolution_source = "manual"
        detail: dict[str, Any] = {
            "resolved_kind": kind,
            "manual_evidence": manual_evidence,
        }
    elif kind == "price_target":
        if instrument not in _AUTO_RESOLVABLE_INSTRUMENTS:
            return {
                "status": "requires_manual",
                "changed": False,
                "reason": (
                    f"instrument_type={instrument} has no daily candle store; "
                    "supply manual_outcome + manual_evidence"
                ),
                "forecast": serialize_forecast(row),
            }
        start_date = row.forecast_start_date or _kst_date(row.created_at)
        candles = await _read_window_candles(
            db,
            symbol=row.symbol,
            instrument_type=instrument,
            start_date=start_date,
            review_date=row.review_date,
            for_share=persist,
        )
        # ROB-712: rejected (non-held) symbols usually have no daily OHLCV in
        # the DB yet. Lazily fetch+persist once via the shared sync service, then
        # re-read. Never raises — backfill returns 0 on failure and the existing
        # unresolved_no_data branch still runs below.
        if not candles and backfill_missing:
            resolved = await _resolve_candle_partition(
                db, symbol=row.symbol, instrument_type=instrument
            )
            if resolved is not None:
                market, partition = resolved
                rows = await _backfill_daily_candles(
                    symbol=row.symbol, market=market, partition=partition
                )
                if rows:
                    candles = await _read_window_candles(
                        db,
                        symbol=row.symbol,
                        instrument_type=instrument,
                        start_date=start_date,
                        review_date=row.review_date,
                        for_share=persist,
                    )
        if not candles:
            return {
                "status": "unresolved_no_data",
                "changed": False,
                "reason": "no loaded daily candles in the resolution window",
                "forecast": serialize_forecast(row),
            }

        direction = target.get("direction")
        target_price = float(target.get("target_price"))
        outcome, observed = classify_price_target_outcome(
            candles, direction=direction, target_price=target_price
        )
        resolution_source = "ohlcv_day"
        detail = {
            "window_start": start_date.isoformat(),
            "window_end": row.review_date.isoformat(),
            "candles": len(candles),
            "direction": direction,
            "target_price": target_price,
            "observed_extreme": observed,
        }
        resolution_contract = _resolution_contract(
            row,
            target=target,
            candle_evidence=[_candle_fingerprint_payload(candle) for candle in candles],
        )
    elif kind == _TERMINAL_CLOSE_KIND:
        terminal_evidence = _terminal_resolution_evidence(row, target)
        if instrument not in _TERMINAL_CLOSE_INSTRUMENTS:
            return {
                "status": "requires_manual",
                "changed": False,
                "reason": (
                    f"instrument_type={instrument} has no regular-session "
                    "terminal-close contract"
                ),
                "resolution_evidence": terminal_evidence,
                "forecast": serialize_forecast(row),
            }

        adjustment_policy = target.get("price_adjustment_policy")
        if adjustment_policy != "explicit-factor-v1":
            return {
                "status": "requires_adjustment_evidence",
                "changed": False,
                "reason": (
                    "terminal_close is fail-closed until an explicit target-to-close "
                    "factor and review-date corporate-action provenance are stored"
                ),
                "resolution_evidence": terminal_evidence,
                "forecast": serialize_forecast(row),
            }

        authentication_failure = _stored_adjustment_authentication_failure(
            row,
            target,
        )
        if authentication_failure is not None:
            return _typed_fail_closed(
                row,
                status="quarantined_untrusted_adjustment_evidence",
                reason=authentication_failure,
                evidence={
                    **terminal_evidence,
                    "semantics_evidence": row.semantics_evidence,
                },
            )

        session_failure = _terminal_close_session_failure(
            instrument_type=instrument,
            review_date=row.review_date,
            now=resolved_now,
        )
        if session_failure is not None:
            return {
                "status": "unresolved_session_not_final",
                "changed": False,
                "reason": session_failure,
                "resolution_evidence": terminal_evidence,
                "forecast": serialize_forecast(row),
            }

        candles = await _read_window_candles(
            db,
            symbol=row.symbol,
            instrument_type=instrument,
            start_date=row.review_date - timedelta(days=7),
            review_date=row.review_date,
            for_share=persist,
        )
        if not candles and backfill_missing:
            resolved = await _resolve_candle_partition(
                db, symbol=row.symbol, instrument_type=instrument
            )
            if resolved is not None:
                market, partition = resolved
                await _backfill_daily_candles(
                    symbol=row.symbol, market=market, partition=partition
                )
                # Daily-candle batch upserts may report rowcount=0 even after a
                # successful write, so always re-read once after the attempt.
                candles = await _read_window_candles(
                    db,
                    symbol=row.symbol,
                    instrument_type=instrument,
                    start_date=row.review_date - timedelta(days=7),
                    review_date=row.review_date,
                    for_share=persist,
                )

        original_target = float(target.get("target_price"))
        adjustment_factor = float(target.get("target_to_close_factor"))
        effective_target = original_target * adjustment_factor
        try:
            outcome, observed, selected = classify_terminal_close_outcome(
                candles or [],
                review_date=row.review_date,
                direction=str(target.get("direction")),
                target_price=effective_target,
            )
        except TerminalCloseDataError as exc:
            return {
                "status": exc.status,
                "changed": False,
                "reason": str(exc),
                "resolution_evidence": {
                    **terminal_evidence,
                    **exc.evidence,
                },
                "forecast": serialize_forecast(row),
            }

        direction = str(target.get("direction"))
        source = str(selected.source)
        finality_failure = _terminal_candle_finality_failure(
            selected,
            instrument_type=instrument,
            review_date=row.review_date,
        )
        if finality_failure is not None:
            return {
                "status": "unresolved_non_final_candle",
                "changed": False,
                "reason": finality_failure,
                "resolution_evidence": {
                    **terminal_evidence,
                    **_candle_fingerprint_payload(selected),
                },
                "forecast": serialize_forecast(row),
            }
        adjustment_provenance = target.get("adjustment_provenance") or {}
        if adjustment_provenance.get("source_price_basis") != selected.price_basis:
            return {
                "status": "unresolved_adjustment_basis_mismatch",
                "changed": False,
                "reason": (
                    "corporate-action evidence price basis does not match selected "
                    "daily close basis"
                ),
                "resolution_evidence": {
                    **terminal_evidence,
                    "evidence_source_price_basis": adjustment_provenance.get(
                        "source_price_basis"
                    ),
                    "candle_source_price_basis": selected.price_basis,
                    "source_row_id": selected.source_row_id,
                },
                "forecast": serialize_forecast(row),
            }
        resolution_source = "ohlcv_day_terminal_close"
        detail = {
            **terminal_evidence,
            "comparison_operator": ">=" if direction == "up" else "<",
            "original_target_price": original_target,
            "target_to_close_factor": adjustment_factor,
            "effective_target_price": effective_target,
            "source_date": _row_date(selected).isoformat(),
            "source_timestamp": selected.time_utc.isoformat(),
            "source_ingested_at": selected.ingested_at.isoformat(),
            "source": source,
            "source_partition": selected.partition,
            "source_row_id": selected.source_row_id,
            "source_row_version": selected.source_row_version,
            "source_price": observed,
            "source_price_field": "close",
            "source_price_basis": selected.price_basis,
            "is_final": selected.is_final,
            "session_scope": selected.session_scope,
            "regular_session_only": (
                selected.is_final is True and selected.session_scope == "regular"
            ),
            "adj_close_used": False,
        }
        resolution_contract = _resolution_contract(
            row,
            target=target,
            candle_evidence=_candle_fingerprint_payload(selected),
        )
    else:
        return {
            "status": "requires_manual",
            "changed": False,
            "reason": (
                f"forecast_target.kind={kind!r} is non-price; "
                "supply manual_outcome + manual_evidence"
            ),
            "forecast": serialize_forecast(row),
        }

    if resolution_contract is not None:
        detail["resolution_contract"] = resolution_contract

    brier = brier_score(float(row.probability), outcome)
    computed = {
        "outcome": outcome,
        "observed_value": observed,
        "brier_score": round(brier, 5),
        "resolution_source": resolution_source,
        "resolution_detail": detail,
    }
    if not persist:
        preview = {
            "status": "previewed",
            "changed": False,
            "computed": computed,
            "forecast": serialize_forecast(row),
        }
        if resolution_contract is not None:
            preview["resolution_contract"] = resolution_contract
        return preview

    if resolution_contract is not None:
        cas_failure = _resolution_cas_failure(
            resolution_contract,
            expected_target_version=expected_target_version,
            expected_claim_hash=expected_claim_hash,
            expected_resolution_fingerprint=expected_resolution_fingerprint,
        )
        if cas_failure is not None:
            missing_expected = (
                expected_target_version is None
                or expected_claim_hash is None
                or expected_resolution_fingerprint is None
            )
            return {
                "status": (
                    "resolution_preview_required"
                    if missing_expected
                    else "resolution_cas_mismatch"
                ),
                "changed": False,
                "reason": cas_failure,
                "resolution_contract": resolution_contract,
                "forecast": serialize_forecast(row),
            }

    row.outcome = outcome
    row.observed_value = _to_decimal(observed)
    row.brier_score = _to_decimal(round(brier, 5))
    row.resolution_source = resolution_source
    row.resolution_detail = detail
    row.resolved_at = resolved_now
    row.status = "closed"
    await db.flush()
    # Reload server-computed columns (updated_at onupdate) within the async
    # context so serialize_forecast doesn't trigger a lazy sync refresh.
    await db.refresh(row)
    result = {
        "status": "resolved",
        "changed": True,
        "computed": computed,
        "forecast": serialize_forecast(row),
    }
    if resolution_contract is not None:
        result["resolution_contract"] = resolution_contract
    return result


def _group_key(r: TradeForecast, group_by: str) -> str:
    if group_by == "day":
        d = _kst_date(r.created_at)
        return d.isoformat() if d else "unknown"
    value = getattr(r, group_by, None)
    return value if value else "unlabeled"


async def _fetch_calibration_rows(
    db: AsyncSession, *, filters: list
) -> list[TradeForecast]:
    """Fetch closed+scored forecasts for calibration with the unused JSONB
    payload columns deferred. Calibration needs ALL matching rows (no LIMIT);
    it only reads brier_score/outcome/probability + the grouping attribute, so
    forecast_target / evidence_ids / resolution_detail are pure load waste."""
    result = await db.execute(
        select(TradeForecast)
        .where(*filters)
        .options(
            defer(TradeForecast.forecast_target),
            defer(TradeForecast.evidence_ids),
            defer(TradeForecast.resolution_detail),
        )
    )
    return list(result.scalars().all())


async def build_forecast_calibration_aggregate(
    db: AsyncSession,
    *,
    group_by: str = "created_by",
    created_by: str | None = None,
    symbol: str | None = None,
    instrument_type: str | None = None,
    days: int | None = None,
) -> dict[str, Any]:
    """Calibration: Brier + hit-rate per label cohort (closed forecasts only).

    Groups closed, scored forecasts by ``created_by`` / ``session_label`` /
    ``model_label`` / KST ``day`` — the objective metric behind an operator's
    "does another LLM reach the same result" comparison. ``calibration_gap`` is
    ``avg_probability - hit_rate`` (positive = over-confident).
    """
    if group_by not in _GROUP_BY_FIELDS:
        group_by = "created_by"

    filters = [
        TradeForecast.status == "closed",
        TradeForecast.brier_score.isnot(None),
    ]
    if created_by is not None:
        filters.append(TradeForecast.created_by == created_by)
    if symbol is not None:
        filters.append(
            TradeForecast.symbol
            == _normalize_symbol_for_filter(symbol, instrument_type)
        )
    if instrument_type is not None:
        filters.append(TradeForecast.instrument_type == instrument_type)
    if days is not None:
        filters.append(TradeForecast.resolved_at >= now_kst() - timedelta(days=days))

    rows = await _fetch_calibration_rows(db, filters=filters)

    groups: dict[str, list[TradeForecast]] = {}
    for r in rows:
        groups.setdefault(_group_key(r, group_by), []).append(r)

    out: list[dict[str, Any]] = []
    for key, items in groups.items():
        n = len(items)
        briers = [float(it.brier_score) for it in items if it.brier_score is not None]
        hits = sum(1 for it in items if it.outcome)
        probs = [float(it.probability) for it in items if it.probability is not None]
        avg_brier = sum(briers) / len(briers) if briers else None
        hit_rate = hits / n if n else None
        avg_prob = sum(probs) / len(probs) if probs else None
        calibration_gap = (
            avg_prob - hit_rate
            if (avg_prob is not None and hit_rate is not None)
            else None
        )
        out.append(
            {
                "group": key,
                "sample_size": n,
                "hits": hits,
                "misses": n - hits,
                "hit_rate": hit_rate,
                "avg_brier_score": avg_brier,
                "avg_probability": avg_prob,
                "calibration_gap": calibration_gap,
            }
        )
    out.sort(key=lambda g: -g["sample_size"])
    return {"group_by": group_by, "groups": out}
