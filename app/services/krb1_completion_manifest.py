"""AC2 — KIS raw daily vs DB exact **local** reconcile manifest.

🔴 Scope (A3, 2026-07-30): this module proves *local* agreement — the stored row
matches the raw response field for field, the whole universe is covered, and the
observations sit inside the allowed window. It does **not** prove that the provider
declared this revision final; that claim lives in
:mod:`app.services.krb1_completion_finality` and fails closed while unwired.
Reason strings say ``local_..._reconcile`` for exactly that reason.

``row_count`` and ``ingested_at`` do not prove a completed session: the 07-29
pre-open snapshot had 32 rows with ``volume=0`` stamped at 07:40 KST, and the
completed batch re-upserts ``ingested_at`` afterwards. The only thing that proves
completion is a symbol-by-symbol match between the KIS raw daily response and the
stored row, observed after the KRX daily completion cutoff and before the
decision clock.

The manifest is that proof, and it is append-only evidence rather than a log
line. It records, per symbol:

* endpoint + TR ID actually called,
* the raw session field (``stck_bsop_date``) as returned,
* raw OHLCV and traded value strings as returned (no float coercion),
* ``observed_at`` for each response,

plus manifest-level ``finalized_at`` and a universe hash so a partial sweep can
never masquerade as a full one.

Pure module: evaluation and building have no clock, network, or database access;
the only side effect available is an append to the service-layer evidence chain.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.services.krb1_evidence_chain import append_record, open_stream, read_records
from app.services.krb1_gate_result import (
    GateResult,
    examples,
    is_aware,
    is_sha256_hex,
    kst_datetime,
    normalize_evidence,
    parse_nonnegative_int_string,
    proven,
    to_kst,
    unprovable,
)
from app.services.krb1_p0_journal import compute_row_hash

SCHEMA_VERSION = "krb1.p0_3.completion_manifest.v1"
COMPLETION_MANIFEST_RECORD_TYPE = "KIS_COMPLETED_SESSION_COMPLETION_MANIFEST"
COMPLETION_MANIFEST_STREAM_ID = "krb1.p0_3.completion_manifest"

# Exact endpoint/TR the manifest is allowed to be built from. Cross-checked
# against app.services.brokers.kis.constants by a guard test.
KIS_DAILY_ENDPOINT = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
KIS_DAILY_TR_ID = "FHKST03010100"
KRX_DAILY_COMPLETION_CUTOFF = dt.time(15, 35)
KRX_VENUE = "KRX"

MATCH = "match"


@dataclass(frozen=True, slots=True)
class RawDailyBar:
    """One KIS daily response row, kept as returned (strings, never coerced).

    ``symbol`` is the symbol we requested (request context). ``raw_symbol`` is the
    identity the *provider* stated in the same response; the KIS daily payload has
    no such field, so it is ``None`` and reconciliation refuses the row rather than
    substituting the request context (ROB-1172 E1/F-02).
    """

    symbol: str
    endpoint: str
    tr_id: str
    raw_symbol: str | None
    raw_business_date: str | None
    raw_open: str | None
    raw_high: str | None
    raw_low: str | None
    raw_close: str | None
    raw_volume: str | None
    raw_value: str | None
    observed_at: dt.datetime
    rt_cd: str | None = None

    def as_canonical(self) -> dict[str, Any]:
        return {
            "acml_tr_pbmn": self.raw_value,
            "acml_vol": self.raw_volume,
            "endpoint": self.endpoint,
            "observed_at": normalize_evidence(self.observed_at),
            "rt_cd": self.rt_cd,
            "stck_bsop_date": self.raw_business_date,
            "stck_clpr": self.raw_close,
            "stck_hgpr": self.raw_high,
            "stck_lwpr": self.raw_low,
            "stck_oprc": self.raw_open,
            "symbol": self.symbol,
            "provider_raw_symbol": self.raw_symbol,
            "request_context_symbol_is_not_identity": True,
            "tr_id": self.tr_id,
        }


@dataclass(frozen=True, slots=True)
class DbDailyBar:
    """The stored row the raw response must match exactly."""

    symbol: str
    session_date: dt.date
    venue: str
    open: int
    high: int
    low: int
    close: int
    volume: int
    value: int

    def as_canonical(self) -> dict[str, Any]:
        return {
            "close": self.close,
            "high": self.high,
            "low": self.low,
            "open": self.open,
            "session_date": self.session_date.isoformat(),
            "symbol": self.symbol,
            "value": self.value,
            "venue": self.venue,
            "volume": self.volume,
        }


@dataclass(frozen=True, slots=True)
class CompletionManifest:
    """Full-universe reconcile result for one market and one session."""

    market: str
    session_date: dt.date
    endpoint: str
    tr_id: str
    universe_hash: str
    symbol_count: int
    reconciled_count: int
    mismatch_count: int
    missing_count: int
    extra_count: int
    first_observed_at: dt.datetime | None
    last_observed_at: dt.datetime | None
    finalized_at: dt.datetime
    manifest_hash: str
    stream_id: str | None = None
    chain_index: int | None = None
    chain_hash: str | None = None
    details: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    failures: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def as_evidence(self) -> dict[str, Any]:
        return normalize_evidence(
            {
                "chain_hash": self.chain_hash,
                "chain_index": self.chain_index,
                "endpoint": self.endpoint,
                "extra_count": self.extra_count,
                "finalized_at": self.finalized_at,
                "first_observed_at": self.first_observed_at,
                "last_observed_at": self.last_observed_at,
                "manifest_hash": self.manifest_hash,
                "market": self.market,
                "mismatch_count": self.mismatch_count,
                "missing_count": self.missing_count,
                "reconciled_count": self.reconciled_count,
                "session_date": self.session_date,
                "stream_id": self.stream_id,
                "symbol_count": self.symbol_count,
                "tr_id": self.tr_id,
                "universe_hash": self.universe_hash,
                "failure_examples": [
                    normalize_evidence(item) for item in self.failures[:20]
                ],
            }
        )


def compute_universe_hash(
    market: str, session_date: dt.date, symbols: Iterable[str]
) -> str:
    """SHA-256 over market + session + the exact symbol set that must be covered."""
    canonical = {
        "market": market,
        "schema_version": SCHEMA_VERSION,
        "session_date": session_date.isoformat(),
        "symbols": sorted({str(symbol) for symbol in symbols}),
    }
    return compute_row_hash(canonical)


def compute_manifest_hash(details: Iterable[Mapping[str, Any]]) -> str:
    canonical = {
        "details": sorted(
            (normalize_evidence(dict(item)) for item in details),
            key=lambda item: str(item["symbol"]),
        ),
        "schema_version": SCHEMA_VERSION,
    }
    return compute_row_hash(canonical)


def reconcile_symbol(
    *,
    raw: RawDailyBar | None,
    db: DbDailyBar | None,
    session_date: dt.date,
    decision_at: dt.datetime,
) -> tuple[str, dict[str, Any]]:
    """Return ``(status, detail)`` for one symbol. ``MATCH`` means proven."""
    if db is None:
        return "db_row_missing", {"raw": raw.as_canonical() if raw else None}
    detail: dict[str, Any] = {"db": db.as_canonical()}
    if raw is None:
        return "raw_response_missing", detail
    detail["raw"] = raw.as_canonical()
    if raw.raw_symbol is None:
        return "provider_identity_missing", detail
    if raw.raw_symbol != db.symbol:
        return "provider_identity_mismatch", detail
    if raw.endpoint != KIS_DAILY_ENDPOINT:
        return "endpoint_mismatch", detail
    if raw.tr_id != KIS_DAILY_TR_ID:
        return "tr_id_mismatch", detail
    if raw.rt_cd not in (None, "0"):
        return "upstream_error_code", detail
    if db.venue != KRX_VENUE or db.session_date != session_date:
        return "db_row_out_of_scope", detail
    if raw.raw_business_date != session_date.strftime("%Y%m%d"):
        return "raw_business_date_mismatch", detail
    if not is_aware(raw.observed_at):
        return "observed_at_not_timezone_aware", detail
    if to_kst(raw.observed_at) < kst_datetime(
        session_date, KRX_DAILY_COMPLETION_CUTOFF
    ):
        return "observed_before_daily_completion_cutoff", detail
    if raw.observed_at > decision_at:
        return "observed_after_decision_at", detail
    numeric_pairs = (
        ("stck_oprc", raw.raw_open, db.open),
        ("stck_hgpr", raw.raw_high, db.high),
        ("stck_lwpr", raw.raw_low, db.low),
        ("stck_clpr", raw.raw_close, db.close),
        ("acml_vol", raw.raw_volume, db.volume),
        ("acml_tr_pbmn", raw.raw_value, db.value),
    )
    for name, raw_text, stored in numeric_pairs:
        parsed = parse_nonnegative_int_string(raw_text)
        if parsed is None:
            return f"raw_{name}_not_exact_integer_string", detail
        if parsed != stored:
            return f"raw_{name}_mismatch", detail
    return MATCH, detail


def build_completion_manifest(
    *,
    market: str,
    session_date: dt.date,
    universe_symbols: Iterable[str],
    raw_bars: Iterable[RawDailyBar],
    db_bars: Iterable[DbDailyBar],
    finalized_at: dt.datetime,
    decision_at: dt.datetime,
) -> CompletionManifest:
    """Reconcile the full universe and return the manifest (no I/O)."""
    if not is_aware(finalized_at) or not is_aware(decision_at):
        raise ValueError("finalized_at and decision_at must be timezone-aware")
    expected = sorted({str(symbol) for symbol in universe_symbols})
    raw_index: dict[str, RawDailyBar] = {}
    duplicate_raw: list[str] = []
    for raw in raw_bars:
        if raw.symbol in raw_index:
            duplicate_raw.append(raw.symbol)
        else:
            raw_index[raw.symbol] = raw
    db_index: dict[str, DbDailyBar] = {}
    duplicate_db: list[str] = []
    for row in db_bars:
        if row.symbol in db_index:
            duplicate_db.append(row.symbol)
        else:
            db_index[row.symbol] = row

    details: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    reconciled = 0
    mismatched = 0
    missing = 0
    observed: list[dt.datetime] = []
    for symbol in expected:
        raw = raw_index.get(symbol)
        db = db_index.get(symbol)
        status, detail = reconcile_symbol(
            raw=raw,
            db=db,
            session_date=session_date,
            decision_at=decision_at,
        )
        if raw is not None and is_aware(raw.observed_at):
            observed.append(raw.observed_at)
        entry = {"status": status, "symbol": symbol, **normalize_evidence(detail)}
        details.append(entry)
        if status == MATCH:
            reconciled += 1
        elif status in {"raw_response_missing", "db_row_missing"}:
            missing += 1
            failures.append(entry)
        else:
            mismatched += 1
            failures.append(entry)

    extra_symbols = sorted((set(raw_index) | set(db_index)) - set(expected))
    for symbol in extra_symbols:
        entry = {
            "status": "outside_expected_universe",
            "symbol": symbol,
            "raw": raw_index[symbol].as_canonical() if symbol in raw_index else None,
            "db": db_index[symbol].as_canonical() if symbol in db_index else None,
        }
        details.append(entry)
        failures.append(entry)
    for symbol in sorted(set(duplicate_raw) | set(duplicate_db)):
        entry = {"status": "duplicate_evidence_row", "symbol": symbol}
        details.append(entry)
        failures.append(entry)

    return CompletionManifest(
        market=market,
        session_date=session_date,
        endpoint=KIS_DAILY_ENDPOINT,
        tr_id=KIS_DAILY_TR_ID,
        universe_hash=compute_universe_hash(market, session_date, expected),
        symbol_count=len(expected),
        reconciled_count=reconciled,
        mismatch_count=mismatched + len(duplicate_raw) + len(duplicate_db),
        missing_count=missing,
        extra_count=len(extra_symbols),
        first_observed_at=min(observed) if observed else None,
        last_observed_at=max(observed) if observed else None,
        finalized_at=finalized_at,
        manifest_hash=compute_manifest_hash(details),
        details=tuple(details),
        failures=tuple(failures),
    )


def evaluate_completion_manifest(
    *,
    manifest: CompletionManifest | None,
    market: str,
    session_date: dt.date,
    universe_symbols: Iterable[str],
    decision_at: dt.datetime,
) -> GateResult:
    """Gate: does a full-universe exact reconcile exist, pre-decision?"""
    expected = sorted({str(symbol) for symbol in universe_symbols})
    expected_hash = compute_universe_hash(market, session_date, expected)
    required = {
        "required_endpoint": KIS_DAILY_ENDPOINT,
        "required_tr_id": KIS_DAILY_TR_ID,
        "required_observation_cutoff_kst": KRX_DAILY_COMPLETION_CUTOFF.isoformat(),
        "required_universe_hash": expected_hash,
        "required_symbol_count": len(expected),
        "required_clock_upper_bound_decision_at": normalize_evidence(decision_at),
        "row_count_and_ingested_at_do_not_prove_completed_session": True,
        # 🔴 A3: this axis proves local consistency + coverage only. Provider
        # finality is a separate gate; see krb1_completion_finality.
        "local_reconcile_is_not_provider_finality": True,
    }
    if not is_aware(decision_at):
        return unprovable(
            "completion_manifest_decision_clock_not_timezone_aware", **required
        )
    if manifest is None:
        return unprovable(
            "completion_manifest_missing",
            market=market,
            append_only_stream_id=COMPLETION_MANIFEST_STREAM_ID,
            **required,
        )
    if manifest.market != market or manifest.session_date != session_date:
        return unprovable(
            "completion_manifest_scope_mismatch",
            manifest=manifest.as_evidence(),
            **required,
        )
    if manifest.endpoint != KIS_DAILY_ENDPOINT or manifest.tr_id != KIS_DAILY_TR_ID:
        return unprovable(
            "completion_manifest_endpoint_or_tr_mismatch",
            manifest=manifest.as_evidence(),
            **required,
        )
    if not is_sha256_hex(manifest.universe_hash) or not is_sha256_hex(
        manifest.manifest_hash
    ):
        return unprovable(
            "completion_manifest_hash_malformed",
            manifest=manifest.as_evidence(),
            **required,
        )
    if manifest.universe_hash != expected_hash or manifest.symbol_count != len(
        expected
    ):
        return unprovable(
            "completion_manifest_universe_hash_mismatch",
            manifest=manifest.as_evidence(),
            **required,
        )
    if manifest.details and compute_manifest_hash(manifest.details) != (
        manifest.manifest_hash
    ):
        return unprovable(
            "completion_manifest_detail_hash_mismatch",
            manifest=manifest.as_evidence(),
            **required,
        )
    if (
        manifest.stream_id != COMPLETION_MANIFEST_STREAM_ID
        or type(manifest.chain_index) is not int
        or manifest.chain_index < 2
        or not is_sha256_hex(manifest.chain_hash)
    ):
        return unprovable(
            "completion_manifest_append_only_provenance_missing",
            manifest=manifest.as_evidence(),
            required_stream_id=COMPLETION_MANIFEST_STREAM_ID,
            **required,
        )
    if (
        manifest.reconciled_count != manifest.symbol_count
        or manifest.mismatch_count
        or manifest.missing_count
        or manifest.extra_count
    ):
        return unprovable(
            "local_full_universe_exact_reconcile_unproven",
            manifest=manifest.as_evidence(),
            failure_symbols=examples(
                [str(item.get("symbol")) for item in manifest.failures]
            ),
            **required,
        )
    if manifest.first_observed_at is None or manifest.last_observed_at is None:
        return unprovable(
            "completion_manifest_observation_clock_missing",
            manifest=manifest.as_evidence(),
            **required,
        )
    if not (
        is_aware(manifest.first_observed_at)
        and is_aware(manifest.last_observed_at)
        and is_aware(manifest.finalized_at)
    ):
        return unprovable(
            "completion_manifest_clock_not_timezone_aware",
            manifest=manifest.as_evidence(),
            **required,
        )
    cutoff = kst_datetime(session_date, KRX_DAILY_COMPLETION_CUTOFF)
    if to_kst(manifest.first_observed_at) < cutoff:
        return unprovable(
            "completion_manifest_observed_before_daily_completion_cutoff",
            manifest=manifest.as_evidence(),
            **required,
        )
    if manifest.last_observed_at > decision_at:
        return unprovable(
            "completion_manifest_observed_after_decision_at",
            manifest=manifest.as_evidence(),
            **required,
        )
    if manifest.finalized_at < manifest.last_observed_at:
        return unprovable(
            "completion_manifest_finalized_before_last_observation",
            manifest=manifest.as_evidence(),
            **required,
        )
    if manifest.finalized_at > decision_at:
        return unprovable(
            "completion_manifest_finalized_after_decision_at",
            manifest=manifest.as_evidence(),
            **required,
        )
    return proven(
        "local_full_universe_exact_reconcile_proven",
        manifest=manifest.as_evidence(),
        **required,
    )


def manifest_row(manifest: CompletionManifest) -> dict[str, Any]:
    """Canonical append-only row for one manifest."""
    return {
        "details": [normalize_evidence(item) for item in manifest.details],
        "endpoint": manifest.endpoint,
        "extra_count": manifest.extra_count,
        "finalized_at": manifest.finalized_at.isoformat(),
        "first_observed_at": (
            manifest.first_observed_at.isoformat()
            if manifest.first_observed_at
            else None
        ),
        "last_observed_at": (
            manifest.last_observed_at.isoformat() if manifest.last_observed_at else None
        ),
        "manifest_hash": manifest.manifest_hash,
        "market": manifest.market,
        "mismatch_count": manifest.mismatch_count,
        "missing_count": manifest.missing_count,
        "recorded_at": manifest.finalized_at.isoformat(),
        "record_type": COMPLETION_MANIFEST_RECORD_TYPE,
        "reconciled_count": manifest.reconciled_count,
        "schema_version": SCHEMA_VERSION,
        "session_date": manifest.session_date.isoformat(),
        "symbol_count": manifest.symbol_count,
        "tr_id": manifest.tr_id,
        "universe_hash": manifest.universe_hash,
    }


def append_completion_manifest(
    path: Path, manifest: CompletionManifest
) -> CompletionManifest:
    """Persist one manifest append-only and return it with chain provenance."""
    open_stream(path, stream_id=COMPLETION_MANIFEST_STREAM_ID)
    record = append_record(
        path,
        stream_id=COMPLETION_MANIFEST_STREAM_ID,
        record_type=COMPLETION_MANIFEST_RECORD_TYPE,
        row=manifest_row(manifest),
    )
    return CompletionManifest(
        market=manifest.market,
        session_date=manifest.session_date,
        endpoint=manifest.endpoint,
        tr_id=manifest.tr_id,
        universe_hash=manifest.universe_hash,
        symbol_count=manifest.symbol_count,
        reconciled_count=manifest.reconciled_count,
        mismatch_count=manifest.mismatch_count,
        missing_count=manifest.missing_count,
        extra_count=manifest.extra_count,
        first_observed_at=manifest.first_observed_at,
        last_observed_at=manifest.last_observed_at,
        finalized_at=manifest.finalized_at,
        manifest_hash=manifest.manifest_hash,
        stream_id=record.stream_id,
        chain_index=record.index,
        chain_hash=record.chain_hash,
        details=manifest.details,
        failures=manifest.failures,
    )


def manifest_from_row(
    row: Mapping[str, Any],
    *,
    stream_id: str,
    chain_index: int,
    chain_hash: str,
) -> CompletionManifest:
    """Rehydrate a manifest from a persisted append-only row."""

    def _clock(key: str) -> dt.datetime | None:
        raw = row.get(key)
        return dt.datetime.fromisoformat(str(raw)) if raw is not None else None

    finalized_at = _clock("finalized_at")
    if finalized_at is None:
        raise ValueError("manifest row is missing finalized_at")
    details = tuple(dict(item) for item in row.get("details") or ())
    failures = tuple(item for item in details if item.get("status") != MATCH)
    return CompletionManifest(
        market=str(row["market"]),
        session_date=dt.date.fromisoformat(str(row["session_date"])),
        endpoint=str(row["endpoint"]),
        tr_id=str(row["tr_id"]),
        universe_hash=str(row["universe_hash"]),
        symbol_count=int(row["symbol_count"]),
        reconciled_count=int(row["reconciled_count"]),
        mismatch_count=int(row["mismatch_count"]),
        missing_count=int(row["missing_count"]),
        extra_count=int(row["extra_count"]),
        first_observed_at=_clock("first_observed_at"),
        last_observed_at=_clock("last_observed_at"),
        finalized_at=finalized_at,
        manifest_hash=str(row["manifest_hash"]),
        stream_id=stream_id,
        chain_index=chain_index,
        chain_hash=chain_hash,
        details=details,
        failures=failures,
    )


def load_latest_completion_manifest(
    path: Path, *, market: str, session_date: dt.date
) -> CompletionManifest | None:
    """Return the most recent verified manifest for one market and session."""
    if not path.exists():
        return None
    latest: CompletionManifest | None = None
    for record in read_records(path, stream_id=COMPLETION_MANIFEST_STREAM_ID):
        if record.record_type != COMPLETION_MANIFEST_RECORD_TYPE:
            continue
        if record.row.get("market") != market or record.row.get("session_date") != (
            session_date.isoformat()
        ):
            continue
        latest = manifest_from_row(
            record.row,
            stream_id=record.stream_id,
            chain_index=record.index,
            chain_hash=record.chain_hash,
        )
    return latest


__all__ = [
    "COMPLETION_MANIFEST_RECORD_TYPE",
    "COMPLETION_MANIFEST_STREAM_ID",
    "KIS_DAILY_ENDPOINT",
    "KIS_DAILY_TR_ID",
    "KRX_DAILY_COMPLETION_CUTOFF",
    "MATCH",
    "SCHEMA_VERSION",
    "CompletionManifest",
    "DbDailyBar",
    "RawDailyBar",
    "append_completion_manifest",
    "build_completion_manifest",
    "compute_manifest_hash",
    "compute_universe_hash",
    "evaluate_completion_manifest",
    "load_latest_completion_manifest",
    "manifest_from_row",
    "manifest_row",
    "reconcile_symbol",
]
