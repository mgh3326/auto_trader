"""Read-only PIT collection and alignment primitives for DFC v2.1.

The harness is deliberately transport-agnostic: an operator supplies pages
from the Binance read endpoints, and this module validates, hashes, sorts and
aligns them.  It has no database, broker, scheduler, or order path.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from research_contracts.dfc_2c_4h_v21 import (
    IntegrityState,
    validate_evidence_manifest,
)

INTERVAL_MS = 4 * 60 * 60 * 1000
REQUIRED_WARMUP_OBSERVATIONS = 504
HARNESS_SOURCE_SHA256 = "89dc38effce02cff0b6f31f31f5de6aae7f60d972d6daeee034fc07dd3afdb7e"
_SOURCE_DECLARATION = re.compile(r'HARNESS_SOURCE_SHA256 = "([0-9a-f]{64})"')


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
            f"DFC v2.1 harness source hash mismatch: declared={declared}, actual={actual}"
        )


_assert_harness_source_frozen()


@dataclass(frozen=True, slots=True)
class EvidenceCandle:
    symbol: str
    endpoint: str
    epoch_start_ms: int
    epoch_end_ms: int
    payload: tuple[Any, ...]
    manifest: Mapping[str, Any]


def raw_payload_sha256(payload: Sequence[Any]) -> str:
    encoded = json.dumps(list(payload), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def validate_and_sort_candles(candles: Iterable[EvidenceCandle]) -> tuple[EvidenceCandle, ...]:
    materialized = tuple(candles)
    for candle in materialized:
        validate_evidence_manifest(candle.manifest)
        if candle.epoch_end_ms - candle.epoch_start_ms != INTERVAL_MS:
            raise ValueError("candle interval must be exactly one UTC 4h epoch")
        if candle.manifest["raw_payload_sha256"] != raw_payload_sha256(candle.payload):
            raise ValueError("raw payload hash does not match evidence manifest")
    ordered = tuple(sorted(materialized, key=lambda c: (c.symbol, c.endpoint, c.epoch_start_ms)))
    for earlier, later in zip(ordered, ordered[1:], strict=False):
        if (earlier.symbol, earlier.endpoint) == (later.symbol, later.endpoint) and earlier.epoch_start_ms == later.epoch_start_ms:
            raise ValueError("duplicate symbol/endpoint/epoch evidence")
    return ordered


def inner_align_4h(
    klines: Iterable[EvidenceCandle], premium_klines: Iterable[EvidenceCandle]
) -> tuple[tuple[str, int], ...]:
    left = {(c.symbol, c.epoch_start_ms) for c in validate_and_sort_candles(klines)}
    right = {(c.symbol, c.epoch_start_ms) for c in validate_and_sort_candles(premium_klines)}
    return tuple(sorted(left & right))


def assert_forward_only(epoch_starts_ms: Sequence[int], prior_windows: Mapping[int, Sequence[int]]) -> None:
    for epoch in epoch_starts_ms:
        prior = tuple(prior_windows[epoch])
        if len(prior) != REQUIRED_WARMUP_OBSERVATIONS:
            raise ValueError("every epoch requires exactly 504 prior observations")
        if any(previous >= epoch for previous in prior):
            raise ValueError("prior window contains current or future epoch")


def _actual_candle_value(candle: EvidenceCandle, *, mapping_key: str, sequence_index: int) -> int:
    """Read timestamps from the raw Binance candle payload, never the manifest."""
    payload = candle.payload
    if isinstance(payload, Mapping):
        value = payload.get(mapping_key)
    else:
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
    actual_signal_end = _actual_candle_value(
        signal_bar, mapping_key="close_time_ms", sequence_index=6
    )
    if actual_signal_close != declared_signal_close_time_ms or actual_signal_end != signal_bar.epoch_end_ms:
        raise ForwardOnlyViolation(
            "SIGNAL_BAR_CLOSE_MISMATCH",
            "declared/manifest signal close does not match the raw signal candle close_time",
        )
    actual_execution_start = _actual_candle_value(
        execution_bar, mapping_key="open_time_ms", sequence_index=0
    )
    if actual_execution_start < actual_signal_close:
        raise ForwardOnlyViolation(
            "NEXT_BAR_OVERLAPS_SIGNAL_BAR",
            "execution candle starts before the raw signal candle closes",
        )


def build_epoch_manifest(
    *, source_id: str, endpoint_host: str, endpoint_path: str, endpoint_version: str,
    symbol: str, epoch_start_utc: str, epoch_end_utc: str, payload: Sequence[Any],
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
