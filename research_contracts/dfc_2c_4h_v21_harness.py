"""Read-only PIT collection and alignment primitives for DFC v2.1.

The harness is deliberately transport-agnostic: an operator supplies pages
from the Binance read endpoints, and this module validates, hashes, sorts and
aligns them.  It has no database, broker, scheduler, or order path.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from research_contracts.dfc_2c_4h_v21 import (
    IntegrityState,
    validate_evidence_manifest,
)

INTERVAL_MS = 4 * 60 * 60 * 1000
REQUIRED_WARMUP_OBSERVATIONS = 504


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
