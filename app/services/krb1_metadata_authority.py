"""AC1 — authoritative Toss metadata snapshots with a decision-clock upper bound.

The 07-29 selector run could not prove that the market/product metadata it read
was authoritative *as of that decision*. Three holes caused it:

1. metadata provenance was a mutable column (``toss_master_updated_at``) with no
   preserved raw payload, no payload hash, and no record of when *we* retrieved
   it;
2. the gate only had a lower bound, so a snapshot stamped *after* the decision
   could retroactively justify it; and
3. (ROB-1172 correction, 08:33) the first fix substituted our retrieval clock for
   the provider clock. A consumer retrieval time is a different clock from a
   provider publication/effective time, and labelling it
   ``authority_clock_source=http_retrieval`` does not create originating
   point-in-time authority. With that substitution a 07-28-vintage master body
   retrieved on 07-29 was accepted as authoritative for 07-29 — which is exactly
   the staleness this gate exists to catch.

So authority now has to come from the provider or not at all:

    provider_effective_session == as_of_session          (exact session identity)
    provider_published_at <= retrieved_at <= decision_at (upper bound: not retroactive)

``retrieved_at`` is still recorded — it bounds when we could have known — but it
can never *stand in* for the provider clock. A snapshot without a provider-origin
clock is ``unprovable``, and :class:`ProviderAuthorityClock` can only be built
from named provider response fields (see
:func:`extract_provider_authority_clock`), never synthesized from a local clock.
This mirrors the quote path, which accepts only KIS' own ``stck_bsop_date`` /
``stck_cntg_hour`` and keeps wrapper timestamps as labelled non-evidence.

Filling metadata in later is not proof of the state at ``decision_at``.

🔴 ROB-1172 F-INT-04 (2026-07-30): the declared-field contract is a module constant
and is **not** a function parameter. A refactor briefly exposed
``declared_published_at_fields`` / ``declared_effective_session_fields`` as optional
keyword arguments on the public evaluator, snapshot builder, appender, and clock
extractor, which let a caller substitute its own field names for D1's empty
allowlist and reach ``proven``. Those parameters are gone. Tests that need a
hypothetical contract monkeypatch the module attributes; that seam exists only
under pytest and no production call site can pass one.

🔴 ROB-1172 F-INT-03: ``symbol_count`` here is the number of rows *we* read. It
seals what our database said at capture time, which is what makes a later silent
truncation detectable — but it is not an external denominator, so it cannot prove
the database was complete when the snapshot was first written. That claim belongs
to :mod:`app.services.krb1_universe_denominator`, which fails closed.

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

SCHEMA_VERSION = "krb1.p0_3.metadata_authority.v2"
METADATA_SNAPSHOT_RECORD_TYPE = "TOSS_AUTHORITATIVE_METADATA_SNAPSHOT"
METADATA_SNAPSHOT_STREAM_ID = "krb1.p0_3.toss_metadata_snapshot"

# Only the Toss authoritative master is accepted. A caller-supplied screener or a
# derived table cannot satisfy this gate; widening the set is a separate reviewed
# change.
AUTHORITATIVE_METADATA_SOURCES = frozenset({"toss_openapi"})

# Provider response field names that may carry an authority clock. Both sets are
# EMPTY on purpose: no field in the wired Toss ``/api/v1/stocks`` projection
# carries a publication or effective clock (``TossStockInfo`` in
# app/services/brokers/toss/dto.py has none, and ``parse_toss_response`` unwraps
# the envelope to a bare row list), so extraction returns ``None`` and capture
# fails closed. Populating either set requires verified provider-contract
# evidence and is a separate reviewed change — it is not configuration.
PROVIDER_PUBLISHED_AT_FIELDS: frozenset[str] = frozenset()
PROVIDER_EFFECTIVE_SESSION_FIELDS: frozenset[str] = frozenset()

PROVIDER_AUTHORITY_CLOCK_ABSENT = "provider_authority_clock_absent"

# v1 stored the retrieval clock in this field and called it authority. A row that
# still carries it is refused on rehydrate even if it claims the v2 schema.
RETIRED_AUTHORITY_ROW_FIELDS: frozenset[str] = frozenset(
    {"metadata_as_of", "authority_clock_source"}
)


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
class ProviderAuthorityClock:
    """A clock that the provider itself stated, with the field it came from.

    Every component is provider-origin: the parsed value, the raw string exactly
    as received, and the response field name. A local clock cannot be dressed up
    as one of these — construction refuses empty field names and raw values, and
    the only sanctioned builder is :func:`extract_provider_authority_clock`.
    """

    published_at: dt.datetime
    published_at_field: str
    published_at_raw: str
    effective_session: dt.date
    effective_session_field: str
    effective_session_raw: str

    def __post_init__(self) -> None:
        if not is_aware(self.published_at):
            raise ValueError("provider published_at must be timezone-aware")
        for name, value in (
            ("published_at_field", self.published_at_field),
            ("published_at_raw", self.published_at_raw),
            ("effective_session_field", self.effective_session_field),
            ("effective_session_raw", self.effective_session_raw),
        ):
            if type(value) is not str or not value.strip():
                raise ValueError(
                    f"{name} must name provider-origin evidence; "
                    "a retrieval clock is not a provider clock"
                )

    def as_canonical(self) -> dict[str, Any]:
        return {
            "effective_session": self.effective_session.isoformat(),
            "effective_session_field": self.effective_session_field,
            "effective_session_raw": self.effective_session_raw,
            "published_at": self.published_at.isoformat(),
            "published_at_field": self.published_at_field,
            "published_at_raw": self.published_at_raw,
        }


def extract_provider_authority_clock(payload: object) -> ProviderAuthorityClock | None:
    """Extract a provider authority clock, or ``None`` when the provider sent none.

    Only declared provider field names are read, and both a publication clock and
    an effective session must be present — one without the other proves nothing
    about which session the master state applies to. A bare row list (what the
    wired Toss surface returns) carries no envelope clock, so it yields ``None``.

    🔴 The declared contract is :data:`PROVIDER_PUBLISHED_AT_FIELDS` /
    :data:`PROVIDER_EFFECTIVE_SESSION_FIELDS` and nothing else. ROB-1172 F-INT-04:
    the first refactor exposed these as optional keyword arguments, which let a
    caller *replace* the empty D1 contract with field names of its own and reach
    ``proven``. An allowlist a caller can supply is not an allowlist. Tests that
    need a hypothetical contract monkeypatch the module attributes — a test-only
    seam that no production call site can reach — and there is no production
    parameter for it.

    Two refusals worth naming, because both were unfixed mutation survivors:

    * **Ambiguity.** If more than one declared field is present for either clock,
      there is no specified precedence, so this returns ``None`` instead of
      silently picking one. Conflicting candidate clocks are a source conflict.
    * **Session widening.** ``effective_session`` must be a bare ``YYYY-MM-DD``
      date. Truncating a timestamp to its first ten characters would assign a
      session by discarding the offset — ``2026-07-29T22:00:00-05:00`` is
      2026-07-30 in KST — so a datetime value is refused rather than coerced.
    """
    declared_published = PROVIDER_PUBLISHED_AT_FIELDS
    declared_effective = PROVIDER_EFFECTIVE_SESSION_FIELDS
    if not isinstance(payload, Mapping):
        return None
    published_at_key = _single_present(payload, declared_published)
    effective_session_key = _single_present(payload, declared_effective)
    if published_at_key is None or effective_session_key is None:
        return None
    published_raw = str(payload[published_at_key])
    effective_raw = str(payload[effective_session_key])
    if not _is_bare_iso_date(effective_raw):
        return None
    try:
        published_at = dt.datetime.fromisoformat(published_raw)
        effective_session = dt.date.fromisoformat(effective_raw)
    except ValueError:
        return None
    if published_at.tzinfo is None or published_at.utcoffset() is None:
        return None
    return ProviderAuthorityClock(
        published_at=published_at,
        published_at_field=published_at_key,
        published_at_raw=published_raw,
        effective_session=effective_session,
        effective_session_field=effective_session_key,
        effective_session_raw=effective_raw,
    )


def _is_bare_iso_date(value: str) -> bool:
    """True only for an exact ``YYYY-MM-DD`` string."""
    if len(value) != 10:
        return False
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _single_present(payload: Mapping[str, Any], keys: frozenset[str]) -> str | None:
    """Return the one present declared key, or ``None`` if zero or many are present."""
    present = sorted(key for key in keys if key in payload and payload[key] is not None)
    if len(present) != 1:
        return None
    return present[0]


@dataclass(frozen=True, slots=True)
class MetadataAuthoritySnapshot:
    """One append-only authoritative metadata snapshot."""

    source: str
    market: str
    universe_metadata_hash: str
    raw_payload_sha256: str
    raw_payload_bytes: int
    symbol_count: int
    provider_clock: ProviderAuthorityClock | None
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
                "provider_clock": (
                    self.provider_clock.as_canonical() if self.provider_clock else None
                ),
                "raw_payload_bytes": self.raw_payload_bytes,
                "raw_payload_sha256": self.raw_payload_sha256,
                "retrieval_clock_is_not_authority": True,
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
    """Gate: is the metadata authoritative *and* pre-decision for this market?

    🔴 The declared provider-clock contract is read from the module constants only
    (ROB-1172 F-INT-04). There is no caller override: a gate whose allowlist is an
    argument is not a gate.
    """
    declared_published = PROVIDER_PUBLISHED_AT_FIELDS
    declared_effective = PROVIDER_EFFECTIVE_SESSION_FIELDS
    required = {
        "required_authoritative_sources": sorted(AUTHORITATIVE_METADATA_SOURCES),
        "required_provider_effective_session_equal_to": as_of_session.isoformat(),
        "required_clock_upper_bound_decision_at": normalize_evidence(decision_at),
        "required_provider_origin_clock": True,
        "required_declared_published_at_fields": sorted(declared_published),
        "required_declared_effective_session_fields": sorted(declared_effective),
        "retrieval_clock_cannot_substitute_for_provider_clock": True,
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

    # 🔴 The authority clock must be provider-origin. Our retrieval clock bounds
    # when we could have known, never what the provider asserted; a payload body
    # of any vintage can be retrieved today, so "we fetched it today" proves
    # nothing about the master state for this session (ROB-1172 correction 08:33).
    provider_clock = snapshot.provider_clock
    if provider_clock is None:
        return unprovable(
            "metadata_snapshot_provider_authority_clock_missing",
            market=market,
            snapshot=snapshot.as_evidence(),
            provider_clock_absent_reason=PROVIDER_AUTHORITY_CLOCK_ABSENT,
            declared_provider_published_at_fields=sorted(PROVIDER_PUBLISHED_AT_FIELDS),
            declared_provider_effective_session_fields=sorted(
                PROVIDER_EFFECTIVE_SESSION_FIELDS
            ),
            **required,
        )
    if not is_aware(snapshot.retrieved_at) or not is_aware(provider_clock.published_at):
        return unprovable(
            "metadata_snapshot_clock_not_timezone_aware",
            market=market,
            snapshot=snapshot.as_evidence(),
            **required,
        )
    # 🔴 The clock must come from a *declared* provider field. Without this the
    # invariant "a retrieval clock is not an authority clock" is enforced only by
    # naming convention: anyone can label a local clock
    # ``published_at_field="retrieved_at"`` and pass. Cross-checking the field name
    # against the declared contract is what makes the naming load-bearing, and it
    # also kills every AST-guard bypass at value-validation time.
    undeclared = sorted(
        name
        for name, allowed in (
            (provider_clock.published_at_field, declared_published),
            (provider_clock.effective_session_field, declared_effective),
        )
        if name not in allowed
    )
    if undeclared:
        return unprovable(
            "metadata_snapshot_provider_clock_field_not_declared",
            market=market,
            snapshot=snapshot.as_evidence(),
            undeclared_fields=undeclared,
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
    if provider_clock.published_at > decision_at:
        return unprovable(
            "metadata_snapshot_provider_published_after_decision_at",
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
    if provider_clock.published_at > snapshot.retrieved_at:
        return unprovable(
            "metadata_snapshot_published_after_retrieval_clock",
            market=market,
            snapshot=snapshot.as_evidence(),
            **required,
        )
    # 🔴 Lower bound now reads the *provider's* effective session. Previously it
    # read a clock that capture set to the retrieval time, so it degenerated to
    # "we fetched today" and a 07-28-vintage body passed.
    if provider_clock.effective_session < as_of_session:
        return unprovable(
            "metadata_snapshot_provider_effective_session_before_selection_session",
            market=market,
            snapshot=snapshot.as_evidence(),
            **required,
        )
    if provider_clock.effective_session > to_kst(decision_at).date():
        return unprovable(
            "metadata_snapshot_provider_effective_session_after_decision_at",
            market=market,
            snapshot=snapshot.as_evidence(),
            **required,
        )
    if provider_clock.effective_session > as_of_session:
        return unprovable(
            "metadata_snapshot_provider_effective_session_after_selection_session",
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
    provider_clock: ProviderAuthorityClock,
    retrieved_at: dt.datetime,
) -> dict[str, Any]:
    """Canonical append-only row for one authoritative metadata snapshot.

    ``provider_clock`` is not optional and its field names are cross-checked
    against the declared provider contract, so a capture path cannot persist a
    snapshot whose authority is really its retrieval time — however the clock was
    constructed, and whatever it is named. The contract is the module constant;
    a caller cannot supply one (ROB-1172 F-INT-04).
    """
    if not isinstance(provider_clock, ProviderAuthorityClock):
        raise ValueError(
            "provider_clock must be a ProviderAuthorityClock extracted from the "
            "provider payload; a retrieval clock cannot substitute for it"
        )
    declared_published = PROVIDER_PUBLISHED_AT_FIELDS
    declared_effective = PROVIDER_EFFECTIVE_SESSION_FIELDS
    undeclared = sorted(
        name
        for name, allowed in (
            (provider_clock.published_at_field, declared_published),
            (provider_clock.effective_session_field, declared_effective),
        )
        if name not in allowed
    )
    if undeclared:
        raise ValueError(
            "provider clock field names are not in the declared provider contract: "
            f"{undeclared}"
        )
    if not is_aware(retrieved_at):
        raise ValueError("retrieved_at must be timezone-aware")
    return {
        "market": market,
        "provider_clock": provider_clock.as_canonical(),
        "raw_payload_bytes": len(raw_payload),
        "raw_payload_sha256": compute_raw_payload_sha256(raw_payload),
        "recorded_at": retrieved_at.isoformat(),
        "record_type": METADATA_SNAPSHOT_RECORD_TYPE,
        "retrieval_clock_is_not_authority": True,
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
    provider_clock: ProviderAuthorityClock,
    retrieved_at: dt.datetime,
) -> MetadataAuthoritySnapshot:
    """Persist one snapshot append-only and return it with chain provenance."""
    row = snapshot_row(
        source=source,
        market=market,
        rows=rows,
        raw_payload=raw_payload,
        provider_clock=provider_clock,
        retrieved_at=retrieved_at,
    )
    open_stream(path, stream_id=METADATA_SNAPSHOT_STREAM_ID)
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
    """Rehydrate a snapshot from a persisted append-only row.

    An unknown schema version raises instead of being coerced: a v1 row carries a
    retrieval-clock-as-authority field that this contract no longer accepts, and
    silently reading it would resurrect the defect.
    """
    canonical_json_bytes(dict(row))
    version = str(row.get("schema_version"))
    if version != SCHEMA_VERSION:
        raise ValueError(
            f"metadata snapshot schema_version {version!r} is not {SCHEMA_VERSION!r}"
        )
    # A row that still carries the retired retrieval-as-authority field is a v1 row
    # wearing a v2 label. Refuse it even though the version string says v2.
    retired = sorted(key for key in RETIRED_AUTHORITY_ROW_FIELDS if key in row)
    if retired:
        raise ValueError(
            f"metadata snapshot row carries retired authority fields {retired}; "
            "it cannot be read under the provider-origin contract"
        )
    clock_row = row["provider_clock"]
    if not isinstance(clock_row, Mapping):
        raise ValueError("metadata snapshot row has no provider_clock object")
    provider_clock = ProviderAuthorityClock(
        published_at=dt.datetime.fromisoformat(str(clock_row["published_at"])),
        published_at_field=str(clock_row["published_at_field"]),
        published_at_raw=str(clock_row["published_at_raw"]),
        effective_session=dt.date.fromisoformat(str(clock_row["effective_session"])),
        effective_session_field=str(clock_row["effective_session_field"]),
        effective_session_raw=str(clock_row["effective_session_raw"]),
    )
    return MetadataAuthoritySnapshot(
        source=str(row["source"]),
        market=str(row["market"]),
        universe_metadata_hash=str(row["universe_metadata_hash"]),
        raw_payload_sha256=str(row["raw_payload_sha256"]),
        raw_payload_bytes=int(row["raw_payload_bytes"]),
        symbol_count=int(row["symbol_count"]),
        provider_clock=provider_clock,
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
    "PROVIDER_AUTHORITY_CLOCK_ABSENT",
    "PROVIDER_EFFECTIVE_SESSION_FIELDS",
    "PROVIDER_PUBLISHED_AT_FIELDS",
    "RETIRED_AUTHORITY_ROW_FIELDS",
    "SCHEMA_VERSION",
    "MetadataAuthoritySnapshot",
    "ProviderAuthorityClock",
    "SymbolMetadata",
    "append_metadata_snapshot",
    "load_latest_metadata_snapshot",
    "compute_raw_payload_sha256",
    "compute_universe_metadata_hash",
    "evaluate_metadata_authority",
    "extract_provider_authority_clock",
    "snapshot_from_row",
    "snapshot_row",
]
