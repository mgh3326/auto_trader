"""AC1 — append-only authoritative metadata snapshots and the decision-clock bound."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from pathlib import Path

import pytest

from app.services.krb1_evidence_chain import EvidenceChainError, verify_stream
from app.services.krb1_metadata_authority import (
    AUTHORITATIVE_METADATA_SOURCES,
    METADATA_SNAPSHOT_STREAM_ID,
    MetadataAuthoritySnapshot,
    SymbolMetadata,
    append_metadata_snapshot,
    compute_raw_payload_sha256,
    compute_universe_metadata_hash,
    evaluate_metadata_authority,
    load_latest_metadata_snapshot,
)

pytestmark = pytest.mark.unit

KST = dt.timezone(dt.timedelta(hours=9))
AS_OF = dt.date(2026, 7, 29)
DECISION_AT = dt.datetime(2026, 7, 29, 18, 0, tzinfo=KST)
RETRIEVED_AT = dt.datetime(2026, 7, 29, 17, 0, tzinfo=KST)
RAW_PAYLOAD = b'{"stocks":[{"symbol":"005930"}]}'
MARKET = "KOSPI"


def _rows() -> tuple[SymbolMetadata, ...]:
    return (
        SymbolMetadata(
            symbol="005930",
            exchange=MARKET,
            security_type="STOCK",
            is_common_share=True,
            listing_status="ACTIVE",
            list_date=dt.date(1975, 6, 11),
            krx_trading_suspended=False,
        ),
        SymbolMetadata(
            symbol="000660",
            exchange=MARKET,
            security_type="STOCK",
            is_common_share=True,
            listing_status="ACTIVE",
            list_date=dt.date(1996, 12, 26),
            krx_trading_suspended=False,
        ),
    )


def _snapshot(**overrides: object) -> MetadataAuthoritySnapshot:
    base = MetadataAuthoritySnapshot(
        source="toss_openapi",
        market=MARKET,
        universe_metadata_hash=compute_universe_metadata_hash(MARKET, _rows()),
        raw_payload_sha256=compute_raw_payload_sha256(RAW_PAYLOAD),
        raw_payload_bytes=len(RAW_PAYLOAD),
        symbol_count=len(_rows()),
        metadata_as_of=RETRIEVED_AT,
        retrieved_at=RETRIEVED_AT,
        stream_id=METADATA_SNAPSHOT_STREAM_ID,
        chain_index=2,
        chain_hash="c" * 64,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _evaluate(snapshot: MetadataAuthoritySnapshot | None, **kwargs: object):
    return evaluate_metadata_authority(
        snapshot=snapshot,
        market=MARKET,
        rows=_rows(),
        as_of_session=kwargs.get("as_of_session", AS_OF),  # type: ignore[arg-type]
        decision_at=kwargs.get("decision_at", DECISION_AT),  # type: ignore[arg-type]
    )


def test_authoritative_sources_are_narrow() -> None:
    assert AUTHORITATIVE_METADATA_SOURCES == frozenset({"toss_openapi"})


def test_proven_when_both_clocks_precede_the_decision() -> None:
    gate = _evaluate(_snapshot())

    assert gate.status == "proven"
    assert gate.reason == "metadata_snapshot_authoritative_within_decision_clock"
    assert gate.evidence["late_backfill_is_not_proof_of_state_at_decision_at"] is True


def test_missing_snapshot_is_unprovable() -> None:
    gate = _evaluate(None)
    assert gate.status == "unprovable"
    assert gate.reason == "authoritative_metadata_snapshot_missing"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"source": "caller_claim"}, "metadata_snapshot_source_not_authoritative"),
        ({"market": "KOSDAQ"}, "metadata_snapshot_market_mismatch"),
        ({"raw_payload_sha256": "nope"}, "metadata_snapshot_hash_malformed"),
        ({"chain_index": 1}, "metadata_snapshot_append_only_provenance_missing"),
        (
            {"stream_id": "some.other.stream"},
            "metadata_snapshot_append_only_provenance_missing",
        ),
        ({"raw_payload_bytes": 0}, "metadata_snapshot_append_only_provenance_missing"),
        ({"symbol_count": 99}, "metadata_snapshot_symbol_count_mismatch"),
        (
            {"universe_metadata_hash": "d" * 64},
            "metadata_snapshot_universe_hash_mismatch",
        ),
        (
            {"metadata_as_of": dt.datetime(2026, 7, 29, 17, 0)},
            "metadata_snapshot_clock_not_timezone_aware",
        ),
    ],
)
def test_defective_snapshots_are_unprovable(
    overrides: dict[str, object], reason: str
) -> None:
    gate = _evaluate(_snapshot(**overrides))
    assert gate.status == "unprovable"
    assert gate.reason == reason


def test_metadata_as_of_after_decision_at_is_unprovable() -> None:
    """🔴 The upper bound: filling metadata later is not proof of earlier state."""
    late = dt.datetime(2026, 7, 30, 8, 47, tzinfo=KST)
    gate = _evaluate(_snapshot(metadata_as_of=late, retrieved_at=late))

    assert gate.status == "unprovable"
    assert gate.reason == "metadata_snapshot_as_of_after_decision_at"


def test_retrieval_clock_after_decision_at_is_unprovable() -> None:
    gate = _evaluate(
        _snapshot(retrieved_at=dt.datetime(2026, 7, 29, 23, 0, tzinfo=KST))
    )
    assert gate.status == "unprovable"
    assert gate.reason == "metadata_snapshot_retrieved_at_after_decision_at"


def test_authority_clock_after_retrieval_clock_is_unprovable() -> None:
    gate = _evaluate(
        _snapshot(
            metadata_as_of=dt.datetime(2026, 7, 29, 17, 30, tzinfo=KST),
            retrieved_at=dt.datetime(2026, 7, 29, 17, 0, tzinfo=KST),
        )
    )
    assert gate.status == "unprovable"
    assert gate.reason == "metadata_snapshot_as_of_after_retrieval_clock"


def test_stale_snapshot_below_session_lower_bound_is_unprovable() -> None:
    stale = dt.datetime(2026, 7, 28, 8, 47, tzinfo=KST)
    gate = _evaluate(_snapshot(metadata_as_of=stale, retrieved_at=stale))

    assert gate.status == "unprovable"
    assert gate.reason == "metadata_snapshot_as_of_before_selection_session"


def test_naive_decision_clock_is_unprovable() -> None:
    gate = _evaluate(_snapshot(), decision_at=dt.datetime(2026, 7, 29, 18, 0))
    assert gate.status == "unprovable"
    assert gate.reason == "metadata_snapshot_decision_clock_not_timezone_aware"


def test_boundary_equality_is_accepted() -> None:
    gate = _evaluate(_snapshot(metadata_as_of=DECISION_AT, retrieved_at=DECISION_AT))
    assert gate.status == "proven"


def test_universe_hash_changes_when_any_metadata_field_changes() -> None:
    rows = _rows()
    baseline = compute_universe_metadata_hash(MARKET, rows)

    assert compute_universe_metadata_hash(MARKET, reversed(rows)) == baseline
    mutated = (replace(rows[0], krx_trading_suspended=True), rows[1])
    assert compute_universe_metadata_hash(MARKET, mutated) != baseline
    assert compute_universe_metadata_hash("KOSDAQ", rows) != baseline


def test_append_only_snapshot_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "toss_metadata_snapshot.jsonl"

    first = append_metadata_snapshot(
        path,
        source="toss_openapi",
        market=MARKET,
        rows=_rows(),
        raw_payload=RAW_PAYLOAD,
        metadata_as_of=RETRIEVED_AT,
        retrieved_at=RETRIEVED_AT,
    )
    prefix = path.read_bytes()
    later = RETRIEVED_AT + dt.timedelta(minutes=30)
    second = append_metadata_snapshot(
        path,
        source="toss_openapi",
        market=MARKET,
        rows=_rows(),
        raw_payload=RAW_PAYLOAD,
        metadata_as_of=later,
        retrieved_at=later,
    )

    assert first.chain_index == 2
    assert second.chain_index == 3
    assert path.read_bytes().startswith(prefix)
    head = verify_stream(path, stream_id=METADATA_SNAPSHOT_STREAM_ID)
    assert head.record_count == 3
    loaded = load_latest_metadata_snapshot(path, market=MARKET)
    assert loaded is not None
    assert loaded.chain_index == 3
    assert loaded.metadata_as_of == later
    assert loaded.raw_payload_sha256 == compute_raw_payload_sha256(RAW_PAYLOAD)
    assert _evaluate(loaded, decision_at=later).status == "proven"


def test_retrieval_clock_cannot_be_backdated_after_a_later_capture(
    tmp_path: Path,
) -> None:
    path = tmp_path / "toss_metadata_snapshot.jsonl"
    append_metadata_snapshot(
        path,
        source="toss_openapi",
        market=MARKET,
        rows=_rows(),
        raw_payload=RAW_PAYLOAD,
        metadata_as_of=RETRIEVED_AT,
        retrieved_at=RETRIEVED_AT,
    )

    with pytest.raises(EvidenceChainError):
        append_metadata_snapshot(
            path,
            source="toss_openapi",
            market=MARKET,
            rows=_rows(),
            raw_payload=RAW_PAYLOAD,
            metadata_as_of=RETRIEVED_AT - dt.timedelta(hours=2),
            retrieved_at=RETRIEVED_AT - dt.timedelta(hours=2),
        )


def test_naive_capture_clocks_are_rejected_at_the_write_boundary(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError):
        append_metadata_snapshot(
            tmp_path / "snapshot.jsonl",
            source="toss_openapi",
            market=MARKET,
            rows=_rows(),
            raw_payload=RAW_PAYLOAD,
            metadata_as_of=dt.datetime(2026, 7, 29, 17, 0),
            retrieved_at=dt.datetime(2026, 7, 29, 17, 0),
        )


def test_load_returns_none_for_absent_stream(tmp_path: Path) -> None:
    assert load_latest_metadata_snapshot(tmp_path / "nope.jsonl", market=MARKET) is None
