"""AC1 — authoritative Toss metadata snapshots with a decision-clock upper bound.

The 07-29 selector run could not prove that the market/product metadata it read
was authoritative *as of that decision*. Two holes caused it:

1. metadata provenance was a mutable column (``toss_master_updated_at``) with no
   preserved raw payload, no payload hash, and no record of when *we* retrieved
   it; and
2. the gate only had a lower bound. ``metadata_as_of >= as_of_session`` alone
   accepts a snapshot stamped *after* the decision, which would let a 07-30
   master refresh retroactively justify a 07-29 decision.

This module closes both. A snapshot is an append-only record (raw payload hash +
upstream authority clock + our retrieval clock + universe hash), and the gate
enforces both bounds:

    as_of_session <= metadata_as_of.date()      (lower bound: not stale)
    metadata_as_of <= retrieved_at <= decision_at   (upper bound: not retroactive)

Filling metadata in later is not proof of the state at ``decision_at``.

Pure evaluation; the only side effect available here is an append to the
service-layer evidence chain.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.services.krb1_evidence_chain import append_record, open_stream, read_records
from app.services.krb1_gate_result import (
    GateResult,
    is_aware,
    is_sha256_hex,
    normalize_evidence,
    proven,
    to_kst,
    unprovable,
)
from app.services.krb1_p0_journal import canonical_json_bytes, compute_row_hash

SCHEMA_VERSION = "krb1.p0_3.metadata_authority.v1"
METADATA_SNAPSHOT_RECORD_TYPE = "TOSS_AUTHORITATIVE_METADATA_SNAPSHOT"
METADATA_SNAPSHOT_STREAM_ID = "krb1.p0_3.toss_metadata_snapshot"

# Only the Toss authoritative master is accepted. A caller-supplied screener or a
# derived table cannot satisfy this gate; widening the set is a separate reviewed
# change.
AUTHORITATIVE_METADATA_SOURCES = frozenset({"toss_openapi"})


@dataclass(frozen=True, slots=True)
class SymbolMetadata:
    """Canonical projection of the metadata fields the selector depends on."""

    symbol: str
    exchange: str
    security_type: str | None
    is_common_share: bool | None
    listing_status: str | None
    list_date: dt.date | None
    krx_trading_suspended: bool | None

    def as_canonical(self) -> dict[str, Any]:
        return {
            "exchange": self.exchange,
            "is_common_share": self.is_common_share,
            "krx_trading_suspended": self.krx_trading_suspended,
            "list_date": self.list_date.isoformat() if self.list_date else None,
            "listing_status": self.listing_status,
            "security_type": self.security_type,
            "symbol": self.symbol,
        }


@dataclass(frozen=True, slots=True)
class MetadataAuthoritySnapshot:
    """One append-only authoritative metadata snapshot."""

    source: str
    market: str
    universe_metadata_hash: str
    raw_payload_sha256: str
    raw_payload_bytes: int
    symbol_count: int
    metadata_as_of: dt.datetime
    retrieved_at: dt.datetime
    stream_id: str
    chain_index: int
    chain_hash: str

    def as_evidence(self) -> dict[str, Any]:
        return normalize_evidence(
            {
                "chain_hash": self.chain_hash,
                "chain_index": self.chain_index,
                "market": self.market,
                "metadata_as_of": self.metadata_as_of,
                "raw_payload_bytes": self.raw_payload_bytes,
                "raw_payload_sha256": self.raw_payload_sha256,
                "retrieved_at": self.retrieved_at,
                "source": self.source,
                "stream_id": self.stream_id,
                "symbol_count": self.symbol_count,
                "universe_metadata_hash": self.universe_metadata_hash,
            }
        )


def compute_universe_metadata_hash(market: str, rows: Iterable[SymbolMetadata]) -> str:
    """SHA-256 over the market plus every symbol's canonical metadata.

    Binds a snapshot to the exact metadata rows the selector read. Any silent
    row edit between snapshot time and selection time changes this hash.
    """
    canonical = {
        "market": market,
        "schema_version": SCHEMA_VERSION,
        "symbols": sorted(
            (row.as_canonical() for row in rows),
            key=lambda item: str(item["symbol"]),
        ),
    }
    return compute_row_hash(canonical)


def compute_raw_payload_sha256(payload: bytes) -> str:
    """SHA-256 of the raw upstream payload exactly as it was received."""
    if type(payload) is not bytes:
        raise TypeError("payload must be raw bytes as returned by the source")
    return hashlib.sha256(payload).hexdigest()


def evaluate_metadata_authority(
    *,
    snapshot: MetadataAuthoritySnapshot | None,
    market: str,
    rows: tuple[SymbolMetadata, ...],
    as_of_session: dt.date,
    decision_at: dt.datetime,
) -> GateResult:
    """Gate: is the metadata authoritative *and* pre-decision for this market?"""
    required = {
        "required_authoritative_sources": sorted(AUTHORITATIVE_METADATA_SOURCES),
        "required_metadata_as_of_at_or_after": as_of_session.isoformat(),
        "required_clock_upper_bound_decision_at": normalize_evidence(decision_at),
        "late_backfill_is_not_proof_of_state_at_decision_at": True,
    }
    if not is_aware(decision_at):
        return unprovable(
            "metadata_snapshot_decision_clock_not_timezone_aware",
            market=market,
            decision_at=normalize_evidence(decision_at),
            **required,
        )
    if snapshot is None:
        return unprovable(
            "authoritative_metadata_snapshot_missing",
            market=market,
            append_only_stream_id=METADATA_SNAPSHOT_STREAM_ID,
            **required,
        )
    if snapshot.market != market:
        return unprovable(
            "metadata_snapshot_market_mismatch",
            market=market,
            snapshot=snapshot.as_evidence(),
            **required,
        )
    if snapshot.source not in AUTHORITATIVE_METADATA_SOURCES:
        return unprovable(
            "metadata_snapshot_source_not_authoritative",
            market=market,
            snapshot=snapshot.as_evidence(),
            **required,
        )
    if not (
        is_sha256_hex(snapshot.raw_payload_sha256)
        and is_sha256_hex(snapshot.universe_metadata_hash)
        and is_sha256_hex(snapshot.chain_hash)
    ):
        return unprovable(
            "metadata_snapshot_hash_malformed",
            market=market,
            snapshot=snapshot.as_evidence(),
            **required,
        )
    if (
        type(snapshot.chain_index) is not int
        or snapshot.chain_index < 2
        or snapshot.stream_id != METADATA_SNAPSHOT_STREAM_ID
        or snapshot.raw_payload_bytes <= 0
    ):
        return unprovable(
            "metadata_snapshot_append_only_provenance_missing",
            market=market,
            snapshot=snapshot.as_evidence(),
            required_stream_id=METADATA_SNAPSHOT_STREAM_ID,
            **required,
        )
    if not (is_aware(snapshot.metadata_as_of) and is_aware(snapshot.retrieved_at)):
        return unprovable(
            "metadata_snapshot_clock_not_timezone_aware",
            market=market,
            snapshot=snapshot.as_evidence(),
            **required,
        )

    computed_hash = compute_universe_metadata_hash(market, rows)
    if snapshot.symbol_count != len(rows):
        return unprovable(
            "metadata_snapshot_symbol_count_mismatch",
            market=market,
            snapshot_symbol_count=snapshot.symbol_count,
            selected_row_count=len(rows),
            snapshot=snapshot.as_evidence(),
            **required,
        )
    if snapshot.universe_metadata_hash != computed_hash:
        return unprovable(
            "metadata_snapshot_universe_hash_mismatch",
            market=market,
            snapshot_universe_metadata_hash=snapshot.universe_metadata_hash,
            computed_universe_metadata_hash=computed_hash,
            **required,
        )

    # 🔴 Upper bound. Without this a 07-30 snapshot could justify a 07-29
    # decision, which is exactly the retroactive path this AC exists to close.
    if snapshot.metadata_as_of > decision_at:
        return unprovable(
            "metadata_snapshot_as_of_after_decision_at",
            market=market,
            snapshot=snapshot.as_evidence(),
            **required,
        )
    if snapshot.retrieved_at > decision_at:
        return unprovable(
            "metadata_snapshot_retrieved_at_after_decision_at",
            market=market,
            snapshot=snapshot.as_evidence(),
            **required,
        )
    if snapshot.metadata_as_of > snapshot.retrieved_at:
        return unprovable(
            "metadata_snapshot_as_of_after_retrieval_clock",
            market=market,
            snapshot=snapshot.as_evidence(),
            **required,
        )
    if to_kst(snapshot.metadata_as_of).date() < as_of_session:
        return unprovable(
            "metadata_snapshot_as_of_before_selection_session",
            market=market,
            snapshot=snapshot.as_evidence(),
            **required,
        )
    return proven(
        "metadata_snapshot_authoritative_within_decision_clock",
        market=market,
        snapshot=snapshot.as_evidence(),
        checked_row_count=len(rows),
        **required,
    )


def snapshot_row(
    *,
    source: str,
    market: str,
    rows: tuple[SymbolMetadata, ...],
    raw_payload: bytes,
    metadata_as_of: dt.datetime,
    retrieved_at: dt.datetime,
) -> dict[str, Any]:
    """Canonical append-only row for one authoritative metadata snapshot."""
    if not (is_aware(metadata_as_of) and is_aware(retrieved_at)):
        raise ValueError("metadata_as_of and retrieved_at must be timezone-aware")
    return {
        "market": market,
        "metadata_as_of": metadata_as_of.isoformat(),
        "raw_payload_bytes": len(raw_payload),
        "raw_payload_sha256": compute_raw_payload_sha256(raw_payload),
        "recorded_at": retrieved_at.isoformat(),
        "record_type": METADATA_SNAPSHOT_RECORD_TYPE,
        "retrieved_at": retrieved_at.isoformat(),
        "schema_version": SCHEMA_VERSION,
        "source": source,
        "symbol_count": len(rows),
        "universe_metadata_hash": compute_universe_metadata_hash(market, rows),
    }


def append_metadata_snapshot(
    path: Path,
    *,
    source: str,
    market: str,
    rows: tuple[SymbolMetadata, ...],
    raw_payload: bytes,
    metadata_as_of: dt.datetime,
    retrieved_at: dt.datetime,
) -> MetadataAuthoritySnapshot:
    """Persist one snapshot append-only and return it with chain provenance."""
    open_stream(path, stream_id=METADATA_SNAPSHOT_STREAM_ID)
    row = snapshot_row(
        source=source,
        market=market,
        rows=rows,
        raw_payload=raw_payload,
        metadata_as_of=metadata_as_of,
        retrieved_at=retrieved_at,
    )
    record = append_record(
        path,
        stream_id=METADATA_SNAPSHOT_STREAM_ID,
        record_type=METADATA_SNAPSHOT_RECORD_TYPE,
        row=row,
    )
    return snapshot_from_row(
        row,
        stream_id=record.stream_id,
        chain_index=record.index,
        chain_hash=record.chain_hash,
    )


def snapshot_from_row(
    row: Mapping[str, Any],
    *,
    stream_id: str,
    chain_index: int,
    chain_hash: str,
) -> MetadataAuthoritySnapshot:
    """Rehydrate a snapshot from a persisted append-only row."""
    canonical_json_bytes(dict(row))
    return MetadataAuthoritySnapshot(
        source=str(row["source"]),
        market=str(row["market"]),
        universe_metadata_hash=str(row["universe_metadata_hash"]),
        raw_payload_sha256=str(row["raw_payload_sha256"]),
        raw_payload_bytes=int(row["raw_payload_bytes"]),
        symbol_count=int(row["symbol_count"]),
        metadata_as_of=dt.datetime.fromisoformat(str(row["metadata_as_of"])),
        retrieved_at=dt.datetime.fromisoformat(str(row["retrieved_at"])),
        stream_id=stream_id,
        chain_index=chain_index,
        chain_hash=chain_hash,
    )


def load_latest_metadata_snapshot(
    path: Path, *, market: str
) -> MetadataAuthoritySnapshot | None:
    """Return the most recent verified snapshot for ``market``, if any."""
    if not path.exists():
        return None
    latest: MetadataAuthoritySnapshot | None = None
    for record in read_records(path, stream_id=METADATA_SNAPSHOT_STREAM_ID):
        if record.record_type != METADATA_SNAPSHOT_RECORD_TYPE:
            continue
        if record.row.get("market") != market:
            continue
        latest = snapshot_from_row(
            record.row,
            stream_id=record.stream_id,
            chain_index=record.index,
            chain_hash=record.chain_hash,
        )
    return latest


__all__ = [
    "AUTHORITATIVE_METADATA_SOURCES",
    "METADATA_SNAPSHOT_RECORD_TYPE",
    "METADATA_SNAPSHOT_STREAM_ID",
    "SCHEMA_VERSION",
    "MetadataAuthoritySnapshot",
    "SymbolMetadata",
    "append_metadata_snapshot",
    "load_latest_metadata_snapshot",
    "compute_raw_payload_sha256",
    "compute_universe_metadata_hash",
    "evaluate_metadata_authority",
    "snapshot_from_row",
    "snapshot_row",
]
