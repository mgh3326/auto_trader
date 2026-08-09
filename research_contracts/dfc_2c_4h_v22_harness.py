"""Read-only PIT collection and alignment primitives for DFC v2.2.

The harness is deliberately transport-agnostic: an operator supplies pages
from the Binance read endpoints, and this module validates, hashes, sorts and
aligns them.  It has no database, broker, scheduler, or order path.

Outcome construction binds BasketDecision to raw EvidenceCandle closes only
(NW-F4). Free bool / free price surfaces are not exposed.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from research_contracts.dfc_2c_4h_v22 import (
    BasketDecision,
    IntegrityState,
    OutcomeEpochRecord,
    _EpochFeatures,
    _EvidenceBinding,
    _PITBasket,
    evaluate_basket,
    extract_kline_close_evidence,
    make_outcome_epoch_record,
    ofi_from_base_volumes,
    premium_index_close_from_complete_4h,
    select_universe,
    validate_evidence_manifest,
)

INTERVAL_MS = 4 * 60 * 60 * 1000
REQUIRED_WARMUP_OBSERVATIONS = 504
HARNESS_SOURCE_SHA256 = (
    "762b5884e0e1bba4dbd4b6270b17dfe695047b38d6bd1c5b3717f9fba25d9386"
)
_SOURCE_DECLARATION = re.compile(
    r'HARNESS_SOURCE_SHA256 = \(\s*"([0-9a-f]{64})"\s*\)', re.DOTALL
)


class ForwardOnlyViolation(ValueError):
    """Fail-closed timestamp violation with the shared #1774 reason code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _assert_harness_source_frozen() -> None:
    source = inspect.getsource(inspect.getmodule(_assert_harness_source_frozen))
    matches = list(_SOURCE_DECLARATION.finditer(source))
    if len(matches) != 1:
        raise RuntimeError("harness source digest declaration count must equal one")
    declared = matches[0].group(1)
    normalized = source[: matches[0].start(1)] + "0" * 64 + source[matches[0].end(1) :]
    actual = hashlib.sha256(normalized.encode()).hexdigest()
    if declared != actual:
        raise RuntimeError(
            f"DFC v2.2 harness source hash mismatch: declared={declared}, actual={actual}"
        )


_assert_harness_source_frozen()


@dataclass(frozen=True, slots=True)
class EvidenceCandle:
    symbol: str
    endpoint: str
    epoch_start_ms: int
    epoch_end_ms: int
    payload: Sequence[Any] | Mapping[str, Any]
    manifest: Mapping[str, Any]


KLINE_MIN_FIELDS = 12
KLINE_OPEN_TIME_INDEX = 0
KLINE_CLOSE_INDEX = 4
KLINE_CLOSE_TIME_INDEX = 6
KLINE_QUOTE_VOLUME_INDEX = 7
KLINE_TAKER_BUY_BASE_INDEX = 9
PREMIUM_CLOSE_INDEX = 4


def raw_payload_sha256(payload: Sequence[Any]) -> str:
    if isinstance(payload, Mapping):
        canonical_payload: Any = {
            str(key): payload[key] for key in sorted(payload, key=str)
        }
    else:
        canonical_payload = list(payload)
    encoded = json.dumps(
        canonical_payload,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
        sort_keys=True,
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def _numeric_payload_value(value: Any, *, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"raw candle payload field {name} must be numeric")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"raw candle payload field {name} must be numeric") from exc
    if not parsed == parsed or parsed in (float("inf"), float("-inf")):
        raise ValueError(f"raw candle payload field {name} must be finite")
    return parsed


def validate_and_sort_candles(
    candles: Iterable[EvidenceCandle],
) -> tuple[EvidenceCandle, ...]:
    materialized = tuple(candles)
    for candle in materialized:
        validate_evidence_manifest(candle.manifest)
        if (
            not isinstance(candle.payload, Mapping)
            and len(candle.payload) < KLINE_MIN_FIELDS
        ):
            raise ValueError(
                "raw candle payload has fewer than the required 12 Binance fields"
            )
        if candle.symbol != candle.manifest["symbol"]:
            raise ValueError("candle symbol does not match evidence manifest")
        if candle.epoch_end_ms - candle.epoch_start_ms != INTERVAL_MS:
            raise ValueError("candle interval must be exactly one UTC 4h epoch")
        if candle.manifest["raw_payload_sha256"] != raw_payload_sha256(candle.payload):
            raise ValueError("raw payload hash does not match evidence manifest")
        open_time = _actual_candle_value(
            candle, mapping_key="open_time_ms", sequence_index=KLINE_OPEN_TIME_INDEX
        )
        close_time = _actual_candle_value(
            candle, mapping_key="close_time_ms", sequence_index=KLINE_CLOSE_TIME_INDEX
        )
        if open_time != candle.epoch_start_ms:
            raise ValueError("raw candle open time does not match epoch start")
        if close_time != open_time + INTERVAL_MS - 1:
            raise ValueError(
                "raw candle closeTime must equal openTime + interval - 1ms"
            )
    ordered = tuple(
        sorted(materialized, key=lambda c: (c.symbol, c.endpoint, c.epoch_start_ms))
    )
    for earlier, later in zip(ordered, ordered[1:], strict=False):
        if (earlier.symbol, earlier.endpoint) == (
            later.symbol,
            later.endpoint,
        ) and earlier.epoch_start_ms == later.epoch_start_ms:
            raise ValueError("duplicate symbol/endpoint/epoch evidence")
    return ordered


def inner_align_4h(
    klines: Iterable[EvidenceCandle], premium_klines: Iterable[EvidenceCandle]
) -> tuple[tuple[str, int], ...]:
    left = {(c.symbol, c.epoch_start_ms) for c in validate_and_sort_candles(klines)}
    right = {
        (c.symbol, c.epoch_start_ms) for c in validate_and_sort_candles(premium_klines)
    }
    return tuple(sorted(left & right))


def assert_forward_only(
    epoch_starts_ms: Sequence[int],
    prior_windows: Mapping[int, Sequence[int]],
    *,
    prior_evidence: Mapping[int, Sequence[EvidenceCandle]],
) -> None:
    for epoch in epoch_starts_ms:
        prior = tuple(prior_windows[epoch])
        if len(prior) != REQUIRED_WARMUP_OBSERVATIONS:
            raise ValueError("every epoch requires exactly 504 prior observations")
        if any(previous >= epoch for previous in prior):
            raise ValueError("prior window contains current or future epoch")
        raw_prior = validate_and_sort_candles(prior_evidence[epoch])
        starts = tuple(candle.epoch_start_ms for candle in raw_prior)
        if starts != prior:
            raise ValueError("prior window does not match raw evidence candle times")
        if any(start >= epoch for start in starts):
            raise ValueError("raw prior window contains current or future candle")


def _actual_candle_value(
    candle: EvidenceCandle, *, mapping_key: str, sequence_index: int
) -> int:
    """Read timestamps from the raw Binance candle payload, never the manifest."""
    payload = candle.payload
    if isinstance(payload, Mapping):
        value = payload.get(mapping_key)
    else:
        if len(payload) < KLINE_MIN_FIELDS:
            raise ValueError(
                "raw candle payload has fewer than the required 12 Binance fields"
            )
        value = payload[sequence_index]
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"raw candle payload lacks numeric {mapping_key}")
    return int(value)


def assert_signal_execution_forward_only(
    signal_bar: EvidenceCandle,
    execution_bar: EvidenceCandle,
    *,
    declared_signal_close_time_ms: int,
) -> None:
    """Compare actual raw candle timestamps and fail closed on overlap.

    Binance kline arrays use index 0 for open time and index 6 for close time.
    Mapping payloads may use the corresponding explicit ``*_time_ms`` keys.
    """
    actual_signal_close = _actual_candle_value(
        signal_bar, mapping_key="close_time_ms", sequence_index=6
    )
    actual_signal_end = actual_signal_close + 1
    if (
        actual_signal_close != declared_signal_close_time_ms
        or actual_signal_end != signal_bar.epoch_end_ms
    ):
        raise ForwardOnlyViolation(
            "SIGNAL_BAR_CLOSE_MISMATCH",
            "declared/manifest signal close does not match the raw signal candle close_time",
        )
    actual_execution_start = _actual_candle_value(
        execution_bar, mapping_key="open_time_ms", sequence_index=0
    )
    if actual_execution_start <= actual_signal_close:
        raise ForwardOnlyViolation(
            "NEXT_BAR_OVERLAPS_SIGNAL_BAR",
            "execution candle starts before the raw signal candle closes",
        )


def build_epoch_manifest(
    *,
    source_id: str,
    endpoint_host: str,
    endpoint_path: str,
    endpoint_version: str,
    symbol: str,
    epoch_start_utc: str,
    epoch_end_utc: str,
    payload: Sequence[Any],
    schema_version: str,
) -> dict[str, Any]:
    """Create provenance only; this function performs no network or persistence."""
    return {
        "source_id": source_id,
        "endpoint_host": endpoint_host,
        "endpoint_path": endpoint_path,
        "endpoint_version": endpoint_version,
        "symbol": symbol,
        "interval": "4h",
        "epoch_start_utc": epoch_start_utc,
        "epoch_end_utc": epoch_end_utc,
        "complete": True,
        "gap_status": "none",
        "raw_payload_sha256": raw_payload_sha256(payload),
        "schema_version": schema_version,
    }


def classify_alignment(ok: bool, *, missing: bool = False) -> IntegrityState:
    if ok:
        return IntegrityState.COMPLETE
    return IntegrityState.MISSING if missing else IntegrityState.GAP


def epoch_features_from_evidence(
    *,
    symbol: str,
    current_kline: EvidenceCandle,
    current_premium: EvidenceCandle,
    prior_klines: Sequence[EvidenceCandle],
    prior_premium: Sequence[EvidenceCandle],
    prior_30d_quote_volume: float,
) -> _EpochFeatures:
    """Build scoring features exclusively from validated raw endpoint evidence."""
    if current_kline.manifest.get("source_id") != "binance_usdm.klines_4h":
        raise ValueError("current kline evidence has the wrong source")
    if (
        current_premium.manifest.get("source_id")
        != "binance_usdm.premium_index_klines_4h"
    ):
        raise ValueError("current premium evidence has the wrong source")
    if any(
        c.manifest.get("source_id") != "binance_usdm.klines_4h" for c in prior_klines
    ):
        raise ValueError("prior kline evidence has the wrong source")
    if any(
        c.manifest.get("source_id") != "binance_usdm.premium_index_klines_4h"
        for c in prior_premium
    ):
        raise ValueError("prior premium evidence has the wrong source")
    klines = validate_and_sort_candles((*prior_klines, current_kline))
    premiums = validate_and_sort_candles((*prior_premium, current_premium))
    if len(klines) != 505 or len(premiums) != 505:
        raise ValueError(
            "feature construction requires exactly 504 prior candles and one current candle"
        )
    if any(c.symbol != symbol for c in (*klines, *premiums)):
        raise ValueError("feature evidence symbol mismatch")
    if current_kline.epoch_start_ms != current_premium.epoch_start_ms:
        raise ValueError("current kline and premium candle are not aligned")
    klines = tuple(sorted(klines, key=lambda candle: candle.epoch_start_ms))
    premiums = tuple(sorted(premiums, key=lambda candle: candle.epoch_start_ms))
    if klines[-1] != current_kline or premiums[-1] != current_premium:
        raise ValueError(
            "current evidence must be the latest candle in the feature window"
        )
    expected_starts = tuple(
        current_kline.epoch_start_ms - INTERVAL_MS * offset
        for offset in range(504, -1, -1)
    )
    if tuple(c.epoch_start_ms for c in klines) != expected_starts:
        raise ValueError(
            "kline feature window must be contiguous and strictly prior to current"
        )
    if tuple(c.epoch_start_ms for c in premiums) != expected_starts:
        raise ValueError(
            "premium feature window must be contiguous and strictly prior to current"
        )
    if tuple(c.epoch_start_ms for c in klines) != tuple(
        c.epoch_start_ms for c in premiums
    ):
        raise ValueError("kline and premium prior windows are not aligned")
    ofi: list[float] = []
    premium: list[float] = []
    for candle in klines:
        payload = candle.payload
        if isinstance(payload, Mapping):
            total = payload.get("volume")
            buy = payload.get("taker_buy_base_volume")
        else:
            total = payload[5]
            buy = payload[KLINE_TAKER_BUY_BASE_INDEX]
        ofi.append(
            ofi_from_base_volumes(
                _numeric_payload_value(total, name="volume"),
                _numeric_payload_value(buy, name="taker_buy_base_volume"),
            )
        )
    for candle in premiums:
        value = (
            candle.payload.get("close")
            if isinstance(candle.payload, Mapping)
            else candle.payload[PREMIUM_CLOSE_INDEX]
        )
        premium.append(
            premium_index_close_from_complete_4h(
                _numeric_payload_value(value, name="premium_close"), is_complete=True
            )
        )
    return _EpochFeatures(
        symbol=symbol,
        integrity=IntegrityState.COMPLETE,
        current_ofi=ofi[-1],
        current_premium_close=premium[-1],
        prior_ofi=tuple(ofi[:-1]),
        prior_premium_close=tuple(premium[:-1]),
        prior_quote_volume_30d=_numeric_payload_value(
            prior_30d_quote_volume, name="prior_30d_quote_volume"
        ),
        evidence=_EvidenceBinding(
            symbol=symbol,
            current_epoch_start_ms=current_kline.epoch_start_ms,
            kline_payload_hashes=tuple(
                c.manifest["raw_payload_sha256"] for c in klines
            ),
            premium_payload_hashes=tuple(
                c.manifest["raw_payload_sha256"] for c in premiums
            ),
        ),
    )


def evaluate_epoch_basket(
    prior_30d_quote_volume: Mapping[str, float], inputs: Mapping[str, _EpochFeatures]
):
    """Wire the PIT universe selection into the basket scorer for every epoch."""
    selected = select_universe(prior_30d_quote_volume)
    if tuple(sorted(inputs)) != tuple(sorted(selected)):
        raise ValueError("basket symbols do not equal the epoch PIT-selected universe")
    return evaluate_basket(
        _PITBasket(selected, tuple(inputs[symbol] for symbol in sorted(inputs)))
    )


def make_outcome_epoch_record_from_candles(
    decision: BasketDecision,
    *,
    entry_kline: EvidenceCandle,
    next_kline: EvidenceCandle | None,
) -> OutcomeEpochRecord:
    """NW-F4 harness entry: bind decision arm/symbol to raw kline EvidenceCandles.

    Missing next bar is recorded as RUN_INVALID_OUTCOME_EVIDENCE — never dropped.
    """
    entry = extract_kline_close_evidence(
        symbol=entry_kline.symbol,
        epoch_start_ms=entry_kline.epoch_start_ms,
        payload=entry_kline.payload,
        manifest=entry_kline.manifest,
    )
    next_bar = None
    if next_kline is not None:
        next_bar = extract_kline_close_evidence(
            symbol=next_kline.symbol,
            epoch_start_ms=next_kline.epoch_start_ms,
            payload=next_kline.payload,
            manifest=next_kline.manifest,
        )
    return make_outcome_epoch_record(decision, entry=entry, next_bar=next_bar)
