"""AC1/A1 — provider-origin metadata authority, bounded above by the decision clock.

The correction this file encodes (ROB-1172, 08:33): a consumer retrieval time is
not a provider authority clock. Every test that used to pass by stamping the
retrieval clock must now fail closed.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from pathlib import Path

import pytest

from app.services.krb1_evidence_chain import EvidenceChainError, verify_stream
from app.services.krb1_metadata_authority import (
    AUTHORITATIVE_METADATA_SOURCES,
    METADATA_SNAPSHOT_STREAM_ID,
    PROVIDER_AUTHORITY_CLOCK_ABSENT,
    PROVIDER_EFFECTIVE_SESSION_FIELDS,
    PROVIDER_PUBLISHED_AT_FIELDS,
    SCHEMA_VERSION,
    MetadataAuthoritySnapshot,
    ProviderAuthorityClock,
    SymbolMetadata,
    append_metadata_snapshot,
    compute_raw_payload_sha256,
    compute_universe_metadata_hash,
    evaluate_metadata_authority,
    extract_provider_authority_clock,
    load_latest_metadata_snapshot,
    snapshot_from_row,
    snapshot_row,
)

pytestmark = pytest.mark.unit

KST = dt.timezone(dt.timedelta(hours=9))
AS_OF = dt.date(2026, 7, 29)
DECISION_AT = dt.datetime(2026, 7, 29, 18, 0, tzinfo=KST)
PUBLISHED_AT = dt.datetime(2026, 7, 29, 16, 30, tzinfo=KST)
RETRIEVED_AT = dt.datetime(2026, 7, 29, 17, 0, tzinfo=KST)
RAW_PAYLOAD = b'{"stocks":[{"symbol":"005930"}]}'
MARKET = "KOSPI"

# A hypothetical provider contract, used only to prove the gate logic is
# satisfiable. The wired Toss surface declares no such fields (both allowlists in
# the service module are empty), which is why capture fails closed today.
DECLARED_PUBLISHED_FIELDS = frozenset({"publishedAt"})
DECLARED_EFFECTIVE_FIELDS = frozenset({"effectiveSession"})


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


def _provider_clock(**overrides: object) -> ProviderAuthorityClock:
    base = ProviderAuthorityClock(
        published_at=PUBLISHED_AT,
        published_at_field="publishedAt",
        published_at_raw=PUBLISHED_AT.isoformat(),
        effective_session=AS_OF,
        effective_session_field="effectiveSession",
        effective_session_raw=AS_OF.isoformat(),
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _snapshot(**overrides: object) -> MetadataAuthoritySnapshot:
    base = MetadataAuthoritySnapshot(
        source="toss_openapi",
        market=MARKET,
        universe_metadata_hash=compute_universe_metadata_hash(MARKET, _rows()),
        raw_payload_sha256=compute_raw_payload_sha256(RAW_PAYLOAD),
        raw_payload_bytes=len(RAW_PAYLOAD),
        symbol_count=len(_rows()),
        provider_clock=_provider_clock(),
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


# ─────────────────────────── the correction, as tests ───────────────────────────


def test_no_provider_clock_field_is_declared_for_the_wired_surface() -> None:
    """Nothing in the wired Toss projection is known to carry an authority clock."""
    assert PROVIDER_PUBLISHED_AT_FIELDS == frozenset()
    assert PROVIDER_EFFECTIVE_SESSION_FIELDS == frozenset()


def test_stale_payload_retrieved_today_is_not_fresh_metadata() -> None:
    """🔴 Regression anchor for the 08:33 correction.

    A 07-28-vintage master body retrieved on 07-29 used to be ``proven`` because
    capture stamped ``metadata_as_of = retrieved_at``. There is now no clock field
    that a retrieval can fill, so the same input fails closed.
    """
    stale_body = b'{"result":[{"symbol":"005930","asOfDataVintage":"2026-07-28"}]}'
    assert extract_provider_authority_clock(stale_body.decode()) is None

    snapshot = _snapshot(
        provider_clock=None,
        raw_payload_sha256=compute_raw_payload_sha256(stale_body),
        raw_payload_bytes=len(stale_body),
    )
    gate = _evaluate(snapshot)

    assert gate.status == "unprovable"
    assert gate.reason == "metadata_snapshot_provider_authority_clock_missing"
    assert gate.evidence["provider_clock_absent_reason"] == (
        PROVIDER_AUTHORITY_CLOCK_ABSENT
    )
    assert gate.evidence["retrieval_clock_cannot_substitute_for_provider_clock"] is True


def test_provider_authority_clock_absent_is_unprovable() -> None:
    gate = _evaluate(_snapshot(provider_clock=None))
    assert gate.status == "unprovable"
    assert gate.reason == "metadata_snapshot_provider_authority_clock_missing"


def test_retrieval_clock_cannot_be_dressed_up_as_a_provider_clock() -> None:
    """A clock with no provider field name behind it cannot be constructed."""
    for overrides in (
        {"published_at_field": ""},
        {"published_at_raw": "   "},
        {"effective_session_field": ""},
        {"effective_session_raw": ""},
    ):
        with pytest.raises(ValueError):
            _provider_clock(**overrides)
    with pytest.raises(ValueError):
        _provider_clock(published_at=dt.datetime(2026, 7, 29, 16, 30))


def test_extract_returns_none_for_the_wired_toss_shapes() -> None:
    """``parse_toss_response`` unwraps to a bare row list — no envelope clock."""
    bare_row_list = [{"symbol": "005930", "securityType": "STOCK"}]
    assert extract_provider_authority_clock(bare_row_list) is None
    assert extract_provider_authority_clock({"result": bare_row_list}) is None
    assert extract_provider_authority_clock(None) is None


def test_extract_is_satisfiable_once_a_provider_contract_is_declared() -> None:
    """Guard against a false block: the extractor works when the provider sends it."""
    payload = {
        "publishedAt": "2026-07-29T16:30:00+09:00",
        "effectiveSession": "2026-07-29",
        "result": [{"symbol": "005930"}],
    }
    clock = extract_provider_authority_clock(
        payload,
        published_at_fields=DECLARED_PUBLISHED_FIELDS,
        effective_session_fields=DECLARED_EFFECTIVE_FIELDS,
    )
    assert clock is not None
    assert clock.published_at_field == "publishedAt"
    assert clock.published_at_raw == "2026-07-29T16:30:00+09:00"
    assert clock.effective_session == AS_OF
    assert _evaluate(_snapshot(provider_clock=clock)).status == "proven"


def test_a_per_row_clock_is_not_a_universe_scope_authority_clock() -> None:
    """A clock on individual rows says nothing about the whole master snapshot.

    Aggregating per-row clocks into one universe-scope authority claim needs
    provider-contract knowledge we do not have, so the extractor refuses to
    descend into rows: envelope level only.
    """
    row_with_clock = {
        "symbol": "005930",
        "publishedAt": "2026-07-29T16:30:00+09:00",
        "effectiveSession": "2026-07-29",
    }
    for payload in ([row_with_clock], {"result": [row_with_clock]}):
        assert (
            extract_provider_authority_clock(
                payload,
                published_at_fields=DECLARED_PUBLISHED_FIELDS,
                effective_session_fields=DECLARED_EFFECTIVE_FIELDS,
            )
            is None
        )


def test_extract_requires_both_clocks() -> None:
    only_published = {"publishedAt": "2026-07-29T16:30:00+09:00"}
    only_effective = {"effectiveSession": "2026-07-29"}
    for payload in (only_published, only_effective):
        assert (
            extract_provider_authority_clock(
                payload,
                published_at_fields=DECLARED_PUBLISHED_FIELDS,
                effective_session_fields=DECLARED_EFFECTIVE_FIELDS,
            )
            is None
        )


@pytest.mark.parametrize(
    "raw",
    [
        {"publishedAt": "not-a-timestamp", "effectiveSession": "2026-07-29"},
        {"publishedAt": "2026-07-29T16:30:00", "effectiveSession": "2026-07-29"},
        {"publishedAt": "2026-07-29T16:30:00+09:00", "effectiveSession": "nope"},
    ],
)
def test_extract_rejects_unusable_provider_values(raw: dict[str, str]) -> None:
    assert (
        extract_provider_authority_clock(
            raw,
            published_at_fields=DECLARED_PUBLISHED_FIELDS,
            effective_session_fields=DECLARED_EFFECTIVE_FIELDS,
        )
        is None
    )


def test_snapshot_row_cannot_be_built_without_a_provider_clock() -> None:
    with pytest.raises(ValueError):
        snapshot_row(
            source="toss_openapi",
            market=MARKET,
            rows=_rows(),
            raw_payload=RAW_PAYLOAD,
            provider_clock=None,  # type: ignore[arg-type]
            retrieved_at=RETRIEVED_AT,
        )
    with pytest.raises(ValueError):
        snapshot_row(
            source="toss_openapi",
            market=MARKET,
            rows=_rows(),
            raw_payload=RAW_PAYLOAD,
            provider_clock=RETRIEVED_AT,  # type: ignore[arg-type]
            retrieved_at=RETRIEVED_AT,
        )


def test_v1_rows_with_retrieval_authority_are_refused_on_rehydrate() -> None:
    """A stored v1 row carries retrieval-as-authority; reading it would resurrect it."""
    v1_row = {
        "market": MARKET,
        "metadata_as_of": RETRIEVED_AT.isoformat(),
        "raw_payload_bytes": len(RAW_PAYLOAD),
        "raw_payload_sha256": compute_raw_payload_sha256(RAW_PAYLOAD),
        "recorded_at": RETRIEVED_AT.isoformat(),
        "record_type": "TOSS_AUTHORITATIVE_METADATA_SNAPSHOT",
        "retrieved_at": RETRIEVED_AT.isoformat(),
        "schema_version": "krb1.p0_3.metadata_authority.v1",
        "source": "toss_openapi",
        "symbol_count": 2,
        "universe_metadata_hash": compute_universe_metadata_hash(MARKET, _rows()),
    }
    with pytest.raises(ValueError):
        snapshot_from_row(
            v1_row,
            stream_id=METADATA_SNAPSHOT_STREAM_ID,
            chain_index=2,
            chain_hash="c" * 64,
        )


# ───────────────────────────── bounds and provenance ─────────────────────────────


def test_authoritative_sources_are_narrow() -> None:
    assert AUTHORITATIVE_METADATA_SOURCES == frozenset({"toss_openapi"})


def test_proven_when_provider_clocks_precede_the_decision() -> None:
    gate = _evaluate(_snapshot())

    assert gate.status == "proven"
    assert gate.reason == "metadata_snapshot_authoritative_within_decision_clock"
    assert gate.evidence["required_provider_origin_clock"] is True
    assert gate.evidence["snapshot"]["retrieval_clock_is_not_authority"] is True


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
            {"retrieved_at": dt.datetime(2026, 7, 29, 17, 0)},
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


def test_provider_publication_after_decision_at_is_unprovable() -> None:
    """🔴 The upper bound: a 07-30 publication cannot justify a 07-29 decision."""
    late = dt.datetime(2026, 7, 30, 8, 47, tzinfo=KST)
    gate = _evaluate(
        _snapshot(
            provider_clock=_provider_clock(
                published_at=late, published_at_raw=late.isoformat()
            ),
            retrieved_at=late,
        )
    )
    assert gate.status == "unprovable"
    assert gate.reason == "metadata_snapshot_provider_published_after_decision_at"


def test_retrieval_clock_after_decision_at_is_unprovable() -> None:
    gate = _evaluate(
        _snapshot(retrieved_at=dt.datetime(2026, 7, 29, 23, 0, tzinfo=KST))
    )
    assert gate.status == "unprovable"
    assert gate.reason == "metadata_snapshot_retrieved_at_after_decision_at"


def test_publication_after_retrieval_clock_is_unprovable() -> None:
    gate = _evaluate(
        _snapshot(retrieved_at=dt.datetime(2026, 7, 29, 16, 0, tzinfo=KST))
    )
    assert gate.status == "unprovable"
    assert gate.reason == "metadata_snapshot_published_after_retrieval_clock"


def test_provider_effective_session_before_selection_session_is_unprovable() -> None:
    """The staleness bound now reads the provider's session, not our fetch time."""
    gate = _evaluate(
        _snapshot(
            provider_clock=_provider_clock(
                effective_session=dt.date(2026, 7, 28),
                effective_session_raw="2026-07-28",
            )
        )
    )
    assert gate.status == "unprovable"
    assert gate.reason == (
        "metadata_snapshot_provider_effective_session_before_selection_session"
    )


def test_provider_effective_session_after_decision_is_unprovable() -> None:
    gate = _evaluate(
        _snapshot(
            provider_clock=_provider_clock(
                effective_session=dt.date(2026, 7, 30),
                effective_session_raw="2026-07-30",
            )
        )
    )
    assert gate.status == "unprovable"
    assert gate.reason == (
        "metadata_snapshot_provider_effective_session_after_decision_at"
    )


def test_naive_decision_clock_is_unprovable() -> None:
    gate = _evaluate(_snapshot(), decision_at=dt.datetime(2026, 7, 29, 18, 0))
    assert gate.status == "unprovable"
    assert gate.reason == "metadata_snapshot_decision_clock_not_timezone_aware"


def test_boundary_equality_is_accepted() -> None:
    gate = _evaluate(
        _snapshot(
            provider_clock=_provider_clock(
                published_at=DECISION_AT, published_at_raw=DECISION_AT.isoformat()
            ),
            retrieved_at=DECISION_AT,
        )
    )
    assert gate.status == "proven"


def test_universe_hash_changes_when_any_metadata_field_changes() -> None:
    rows = _rows()
    baseline = compute_universe_metadata_hash(MARKET, rows)

    assert compute_universe_metadata_hash(MARKET, reversed(rows)) == baseline
    mutated = (replace(rows[0], krx_trading_suspended=True), rows[1])
    assert compute_universe_metadata_hash(MARKET, mutated) != baseline
    assert compute_universe_metadata_hash("KOSDAQ", rows) != baseline


# ─────────────────────────────── append-only store ───────────────────────────────


def test_append_only_snapshot_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "toss_metadata_snapshot.jsonl"

    first = append_metadata_snapshot(
        path,
        source="toss_openapi",
        market=MARKET,
        rows=_rows(),
        raw_payload=RAW_PAYLOAD,
        provider_clock=_provider_clock(),
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
        provider_clock=_provider_clock(),
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
    assert loaded.provider_clock == _provider_clock()
    assert loaded.retrieved_at == later
    assert loaded.raw_payload_sha256 == compute_raw_payload_sha256(RAW_PAYLOAD)
    assert _evaluate(loaded).status == "proven"


def test_persisted_row_records_the_provider_fields_and_schema(tmp_path: Path) -> None:
    path = tmp_path / "toss_metadata_snapshot.jsonl"
    append_metadata_snapshot(
        path,
        source="toss_openapi",
        market=MARKET,
        rows=_rows(),
        raw_payload=RAW_PAYLOAD,
        provider_clock=_provider_clock(),
        retrieved_at=RETRIEVED_AT,
    )
    row = snapshot_row(
        source="toss_openapi",
        market=MARKET,
        rows=_rows(),
        raw_payload=RAW_PAYLOAD,
        provider_clock=_provider_clock(),
        retrieved_at=RETRIEVED_AT,
    )

    assert row["schema_version"] == SCHEMA_VERSION
    assert row["retrieval_clock_is_not_authority"] is True
    assert row["provider_clock"]["published_at_field"] == "publishedAt"
    assert "metadata_as_of" not in row


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
        provider_clock=_provider_clock(),
        retrieved_at=RETRIEVED_AT,
    )

    with pytest.raises(EvidenceChainError):
        append_metadata_snapshot(
            path,
            source="toss_openapi",
            market=MARKET,
            rows=_rows(),
            raw_payload=RAW_PAYLOAD,
            provider_clock=_provider_clock(),
            retrieved_at=RETRIEVED_AT - dt.timedelta(hours=2),
        )


def test_naive_retrieval_clock_is_rejected_at_the_write_boundary(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError):
        append_metadata_snapshot(
            tmp_path / "snapshot.jsonl",
            source="toss_openapi",
            market=MARKET,
            rows=_rows(),
            raw_payload=RAW_PAYLOAD,
            provider_clock=_provider_clock(),
            retrieved_at=dt.datetime(2026, 7, 29, 17, 0),
        )


def test_load_returns_none_for_absent_stream(tmp_path: Path) -> None:
    assert load_latest_metadata_snapshot(tmp_path / "nope.jsonl", market=MARKET) is None
