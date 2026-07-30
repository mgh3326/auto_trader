"""AC2 — KIS raw daily vs DB exact reconcile completion manifest."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from pathlib import Path

import pytest

from app.services.brokers.kis import constants as kis_constants
from app.services.krb1_completion_manifest import (
    COMPLETION_MANIFEST_STREAM_ID,
    KIS_DAILY_ENDPOINT,
    KIS_DAILY_TR_ID,
    MATCH,
    REQUIRED_DETAIL_IDENTITY_FIELDS,
    RETIRED_SCHEMA_VERSIONS,
    SCHEMA_VERSION,
    CompletionManifest,
    DbDailyBar,
    RawDailyBar,
    append_completion_manifest,
    build_completion_manifest,
    compute_manifest_hash,
    compute_universe_hash,
    evaluate_completion_manifest,
    load_latest_completion_manifest,
    manifest_from_row,
    manifest_row,
    reconcile_symbol,
)
from app.services.krb1_evidence_chain import verify_stream

pytestmark = pytest.mark.unit

KST = dt.timezone(dt.timedelta(hours=9))
SESSION = dt.date(2026, 7, 29)
DECISION_AT = dt.datetime(2026, 7, 29, 18, 0, tzinfo=KST)
OBSERVED_AT = dt.datetime(2026, 7, 29, 16, 30, tzinfo=KST)
FINALIZED_AT = dt.datetime(2026, 7, 29, 17, 0, tzinfo=KST)
MARKET = "KOSPI"
SYMBOLS = ("000660", "005930")


def _db(symbol: str, *, close: int = 70_000) -> DbDailyBar:
    return DbDailyBar(
        symbol=symbol,
        session_date=SESSION,
        venue="KRX",
        open=close - 500,
        high=close + 700,
        low=close - 900,
        close=close,
        volume=12_345_678,
        value=987_654_321_000,
    )


def _raw(db: DbDailyBar, **overrides: object) -> RawDailyBar:
    base = RawDailyBar(
        symbol=db.symbol,
        endpoint=KIS_DAILY_ENDPOINT,
        tr_id=KIS_DAILY_TR_ID,
        raw_symbol=db.symbol,
        raw_business_date="20260729",
        raw_open=str(db.open),
        raw_high=str(db.high),
        raw_low=str(db.low),
        raw_close=str(db.close),
        raw_volume=str(db.volume),
        raw_value=str(db.value),
        observed_at=OBSERVED_AT,
        rt_cd="0",
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _manifest(**kwargs: object) -> CompletionManifest:
    db_bars = [_db(symbol) for symbol in SYMBOLS]
    return build_completion_manifest(
        market=MARKET,
        session_date=SESSION,
        universe_symbols=kwargs.get("universe_symbols", SYMBOLS),  # type: ignore[arg-type]
        raw_bars=kwargs.get("raw_bars", [_raw(row) for row in db_bars]),  # type: ignore[arg-type]
        db_bars=kwargs.get("db_bars", db_bars),  # type: ignore[arg-type]
        finalized_at=kwargs.get("finalized_at", FINALIZED_AT),  # type: ignore[arg-type]
        decision_at=kwargs.get("decision_at", DECISION_AT),  # type: ignore[arg-type]
    )


def _persisted(manifest: CompletionManifest) -> CompletionManifest:
    return replace(
        manifest,
        stream_id=COMPLETION_MANIFEST_STREAM_ID,
        chain_index=2,
        chain_hash="e" * 64,
    )


def _evaluate(manifest: CompletionManifest | None, **kwargs: object):
    return evaluate_completion_manifest(
        manifest=manifest,
        market=MARKET,
        session_date=SESSION,
        universe_symbols=kwargs.get("universe_symbols", SYMBOLS),  # type: ignore[arg-type]
        decision_at=kwargs.get("decision_at", DECISION_AT),  # type: ignore[arg-type]
    )


def test_endpoint_and_tr_match_the_kis_adapter_constants() -> None:
    assert KIS_DAILY_ENDPOINT == kis_constants.DOMESTIC_DAILY_CHART_URL
    assert KIS_DAILY_TR_ID == kis_constants.DOMESTIC_DAILY_CHART_TR


def test_full_reconcile_is_proven() -> None:
    manifest = _persisted(_manifest())
    gate = _evaluate(manifest)

    assert manifest.reconciled_count == len(SYMBOLS)
    assert manifest.mismatch_count == 0
    assert manifest.missing_count == 0
    assert gate.status == "proven"
    assert gate.reason == "local_full_universe_exact_reconcile_proven"
    assert gate.evidence["local_reconcile_is_not_provider_finality"] is True
    assert (
        gate.evidence["row_count_and_ingested_at_do_not_prove_completed_session"]
        is True
    )


def test_missing_raw_response_is_recorded_and_blocks() -> None:
    db_bars = [_db(symbol) for symbol in SYMBOLS]
    manifest = _persisted(_manifest(raw_bars=[_raw(db_bars[0])]))

    assert manifest.missing_count == 1
    assert manifest.reconciled_count == 1
    gate = _evaluate(manifest)
    assert gate.status == "unprovable"
    assert gate.reason == "local_full_universe_exact_reconcile_unproven"
    assert "005930" in gate.evidence["failure_symbols"]


@pytest.mark.parametrize(
    ("overrides", "status"),
    [
        ({"raw_close": "1"}, "raw_stck_clpr_mismatch"),
        ({"raw_open": "1"}, "raw_stck_oprc_mismatch"),
        ({"raw_high": "1"}, "raw_stck_hgpr_mismatch"),
        ({"raw_low": "1"}, "raw_stck_lwpr_mismatch"),
        ({"raw_volume": "1"}, "raw_acml_vol_mismatch"),
        ({"raw_value": "1"}, "raw_acml_tr_pbmn_mismatch"),
        ({"raw_close": "70000.0"}, "raw_stck_clpr_not_exact_integer_string"),
        ({"raw_close": None}, "raw_stck_clpr_not_exact_integer_string"),
        ({"raw_business_date": "20260728"}, "raw_business_date_mismatch"),
        ({"endpoint": "/uapi/other"}, "endpoint_mismatch"),
        ({"tr_id": "FHKST00000000"}, "tr_id_mismatch"),
        ({"rt_cd": "1"}, "upstream_error_code"),
        (
            {"observed_at": dt.datetime(2026, 7, 29, 7, 40, tzinfo=KST)},
            "observed_before_daily_completion_cutoff",
        ),
        (
            {"observed_at": dt.datetime(2026, 7, 29, 19, 0, tzinfo=KST)},
            "observed_after_decision_at",
        ),
        (
            {"observed_at": dt.datetime(2026, 7, 29, 16, 30)},
            "observed_at_not_timezone_aware",
        ),
    ],
)
def test_per_symbol_defects_are_named(
    overrides: dict[str, object], status: str
) -> None:
    db = _db(SYMBOLS[0])
    result, _detail = reconcile_symbol(
        raw=_raw(db, **overrides),
        db=db,
        session_date=SESSION,
        decision_at=DECISION_AT,
    )
    assert result == status


def test_matching_symbol_reconciles() -> None:
    db = _db(SYMBOLS[0])
    result, detail = reconcile_symbol(
        raw=_raw(db), db=db, session_date=SESSION, decision_at=DECISION_AT
    )
    assert result == MATCH
    assert detail["raw"]["stck_clpr"] == str(db.close)


def test_extra_symbol_outside_universe_blocks() -> None:
    db_bars = [_db(symbol) for symbol in SYMBOLS] + [_db("999999")]
    manifest = _persisted(
        _manifest(db_bars=db_bars, raw_bars=[_raw(row) for row in db_bars])
    )

    assert manifest.extra_count == 1
    assert _evaluate(manifest).status == "unprovable"


def test_duplicate_evidence_rows_block() -> None:
    db_bars = [_db(symbol) for symbol in SYMBOLS]
    raw_bars = [_raw(row) for row in db_bars] + [_raw(db_bars[0])]
    manifest = _persisted(_manifest(raw_bars=raw_bars))

    assert manifest.mismatch_count >= 1
    assert _evaluate(manifest).status == "unprovable"


def test_partial_sweep_cannot_masquerade_as_full_coverage() -> None:
    """A sweep of one symbol cannot prove a two-symbol universe."""
    db_bars = [_db(symbol) for symbol in SYMBOLS]
    manifest = _persisted(
        _manifest(
            universe_symbols=(SYMBOLS[0],),
            raw_bars=[_raw(db_bars[0])],
            db_bars=[db_bars[0]],
        )
    )

    gate = _evaluate(manifest)
    assert gate.status == "unprovable"
    assert gate.reason == "completion_manifest_universe_hash_mismatch"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"market": "KOSDAQ"}, "completion_manifest_scope_mismatch"),
        (
            {"session_date": dt.date(2026, 7, 28)},
            "completion_manifest_scope_mismatch",
        ),
        ({"endpoint": "/uapi/other"}, "completion_manifest_endpoint_or_tr_mismatch"),
        ({"manifest_hash": "short"}, "completion_manifest_hash_malformed"),
        ({"manifest_hash": "f" * 64}, "completion_manifest_detail_hash_mismatch"),
        (
            {"stream_id": None, "chain_index": None, "chain_hash": None},
            "completion_manifest_append_only_provenance_missing",
        ),
        (
            {"finalized_at": dt.datetime(2026, 7, 29, 23, 0, tzinfo=KST)},
            "completion_manifest_finalized_after_decision_at",
        ),
        (
            {"finalized_at": dt.datetime(2026, 7, 29, 16, 0, tzinfo=KST)},
            "completion_manifest_finalized_before_last_observation",
        ),
    ],
)
def test_manifest_level_defects_are_named(
    overrides: dict[str, object], reason: str
) -> None:
    manifest = replace(_persisted(_manifest()), **overrides)  # type: ignore[arg-type]
    gate = _evaluate(manifest)
    assert gate.status == "unprovable"
    assert gate.reason == reason


def test_missing_manifest_is_unprovable() -> None:
    gate = _evaluate(None)
    assert gate.status == "unprovable"
    assert gate.reason == "completion_manifest_missing"


def test_manifest_reused_for_an_earlier_decision_clock_blocks() -> None:
    """A manifest observed after the decision cannot prove that decision.

    Every row reconciles against the manifest's own (later) decision clock, so the
    only thing that can block reuse is the manifest-level upper bound.
    """
    db_bars = [_db(symbol) for symbol in SYMBOLS]
    late_observation = dt.datetime(2026, 7, 29, 19, 0, tzinfo=KST)
    manifest = _persisted(
        _manifest(
            raw_bars=[_raw(row, observed_at=late_observation) for row in db_bars],
            finalized_at=late_observation,
            decision_at=dt.datetime(2026, 7, 29, 23, 0, tzinfo=KST),
        )
    )

    assert manifest.reconciled_count == len(SYMBOLS)
    gate = _evaluate(manifest, decision_at=DECISION_AT)
    assert gate.status == "unprovable"
    assert gate.reason == "completion_manifest_observed_after_decision_at"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        (
            {"first_observed_at": dt.datetime(2026, 7, 29, 7, 40, tzinfo=KST)},
            "completion_manifest_observed_before_daily_completion_cutoff",
        ),
        (
            {"first_observed_at": None, "last_observed_at": None},
            "completion_manifest_observation_clock_missing",
        ),
        (
            {"last_observed_at": dt.datetime(2026, 7, 29, 16, 30)},
            "completion_manifest_clock_not_timezone_aware",
        ),
    ],
)
def test_manifest_observation_clock_defects_block(
    overrides: dict[str, object], reason: str
) -> None:
    manifest = replace(_persisted(_manifest()), **overrides)  # type: ignore[arg-type]
    gate = _evaluate(manifest)
    assert gate.status == "unprovable"
    assert gate.reason == reason


def test_naive_decision_clock_is_unprovable() -> None:
    gate = _evaluate(
        _persisted(_manifest()), decision_at=dt.datetime(2026, 7, 29, 18, 0)
    )
    assert gate.status == "unprovable"
    assert gate.reason == "completion_manifest_decision_clock_not_timezone_aware"


def test_universe_and_manifest_hashes_are_scope_sensitive() -> None:
    baseline = compute_universe_hash(MARKET, SESSION, SYMBOLS)

    assert compute_universe_hash(MARKET, SESSION, reversed(SYMBOLS)) == baseline
    assert compute_universe_hash("KOSDAQ", SESSION, SYMBOLS) != baseline
    assert compute_universe_hash(MARKET, dt.date(2026, 7, 28), SYMBOLS) != baseline

    manifest = _manifest()
    assert compute_manifest_hash(manifest.details) == manifest.manifest_hash


def test_append_only_manifest_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "completion_manifest.jsonl"

    stored = append_completion_manifest(path, _manifest())
    prefix = path.read_bytes()
    append_completion_manifest(
        path, _manifest(finalized_at=FINALIZED_AT + dt.timedelta(minutes=15))
    )

    assert stored.chain_index == 2
    assert stored.stream_id == COMPLETION_MANIFEST_STREAM_ID
    assert path.read_bytes().startswith(prefix)
    assert (
        verify_stream(path, stream_id=COMPLETION_MANIFEST_STREAM_ID).record_count == 3
    )

    loaded = load_latest_completion_manifest(path, market=MARKET, session_date=SESSION)
    assert loaded is not None
    assert loaded.chain_index == 3
    assert loaded.manifest_hash == stored.manifest_hash
    assert _evaluate(loaded).status == "proven"
    assert (
        load_latest_completion_manifest(
            path, market=MARKET, session_date=dt.date(2026, 7, 28)
        )
        is None
    )


def test_build_rejects_naive_clocks() -> None:
    with pytest.raises(ValueError):
        _manifest(finalized_at=dt.datetime(2026, 7, 29, 17, 0))


# ───── F-INT-02 / N06: provider identity is required at build *and* read time ─────


@pytest.mark.parametrize(
    ("raw_symbol", "expected"),
    [
        (None, "provider_identity_missing"),
        ("000000", "provider_identity_mismatch"),
        ("", "provider_identity_mismatch"),
    ],
)
def test_reconcile_refuses_a_row_without_provider_origin_identity(
    raw_symbol: str | None, expected: str
) -> None:
    """🔴 N06 anchor. The mutant that deleted both of these checks survived.

    ``symbol`` is the symbol we asked for; ``provider_raw_symbol`` is what the
    response said it was. #1729 filled the second from the first, so the evidence
    never confirmed which instrument it described (E1). With no anchor here, that
    substitution can come back silently.
    """
    db = _db(SYMBOLS[0])
    status, detail = reconcile_symbol(
        raw=_raw(db, raw_symbol=raw_symbol),
        db=db,
        session_date=SESSION,
        decision_at=DECISION_AT,
    )
    assert status == expected
    assert detail["raw"]["provider_raw_symbol"] == raw_symbol
    assert detail["raw"]["request_context_symbol_is_not_identity"] is True


def test_identity_less_rows_cannot_reach_a_proven_manifest() -> None:
    db_bars = [_db(symbol) for symbol in SYMBOLS]
    manifest = _persisted(
        _manifest(raw_bars=[_raw(row, raw_symbol=None) for row in db_bars])
    )

    assert manifest.reconciled_count == 0
    gate = _evaluate(manifest)
    assert gate.status == "unprovable"
    assert gate.reason == "local_full_universe_exact_reconcile_unproven"


def test_the_gate_re_establishes_identity_from_the_persisted_details() -> None:
    """🔴 F-INT-02: the reader must not trust that the writer checked.

    A manifest reaches the gate as an append-only row. If only
    :func:`reconcile_symbol` enforces identity, every record written before that
    check existed stays ``proven`` forever — which is exactly what the verifier
    demonstrated with a pre-fix-shape manifest.
    """
    manifest = _persisted(_manifest())
    details = tuple(
        {**detail, "raw": {**detail["raw"], "provider_raw_symbol": None}}
        for detail in manifest.details
    )
    forged = replace(
        manifest, details=details, manifest_hash=compute_manifest_hash(details)
    )

    gate = _evaluate(forged)

    assert gate.status == "unprovable"
    assert gate.reason == "completion_manifest_provider_identity_unproven"
    assert gate.evidence["identity_defect_count"] == len(SYMBOLS)
    assert {item["defect"] for item in gate.evidence["identity_defects"]} == {
        "provider_identity_missing"
    }


def test_a_detail_whose_identity_disagrees_with_its_symbol_blocks() -> None:
    manifest = _persisted(_manifest())
    details = tuple(
        {**detail, "raw": {**detail["raw"], "provider_raw_symbol": "999999"}}
        for detail in manifest.details
    )
    forged = replace(
        manifest, details=details, manifest_hash=compute_manifest_hash(details)
    )

    gate = _evaluate(forged)

    assert gate.reason == "completion_manifest_provider_identity_unproven"
    assert {item["defect"] for item in gate.evidence["identity_defects"]} == {
        "provider_identity_mismatch"
    }


def test_stripping_the_details_does_not_vacuously_prove_the_manifest() -> None:
    """Details are the evidence; a row without them cannot be re-checked."""
    manifest = replace(_persisted(_manifest()), details=(), failures=())

    gate = _evaluate(manifest)

    assert gate.status == "unprovable"
    assert gate.reason == "completion_manifest_details_missing"


def test_schema_version_is_v2_and_v1_is_retired() -> None:
    """🔴 The canonical regression: the detail shape changed under a v1 label.

    ROB-1172 added ``provider_raw_symbol`` to the detail shape while leaving
    ``SCHEMA_VERSION`` at v1, which retroactively promoted every identity-less
    record already in the chain. The operator condition was "bump the schema version
    and do not retro-promote existing v1 evidence".
    """
    assert SCHEMA_VERSION == "krb1.p0_3.completion_manifest.v2"
    assert RETIRED_SCHEMA_VERSIONS == frozenset({"krb1.p0_3.completion_manifest.v1"})


@pytest.mark.parametrize(
    "label",
    ["krb1.p0_3.completion_manifest.v1", "krb1.p0_3.completion_manifest.v0", "", None],
)
def test_a_legacy_labelled_row_is_refused_on_rehydrate(label: str | None) -> None:
    row = manifest_row(_manifest())
    row["schema_version"] = label

    with pytest.raises(ValueError, match="schema_version"):
        manifest_from_row(
            row,
            stream_id=COMPLETION_MANIFEST_STREAM_ID,
            chain_index=2,
            chain_hash="e" * 64,
        )


def test_a_v2_labelled_row_with_the_v1_detail_shape_is_refused() -> None:
    """The label is not the contract: identity keys must actually be there."""
    row = manifest_row(_manifest())
    for detail in row["details"]:
        raw = detail.get("raw")
        if isinstance(raw, dict):
            for key in sorted(REQUIRED_DETAIL_IDENTITY_FIELDS):
                raw.pop(key, None)
    row["manifest_hash"] = compute_manifest_hash(row["details"])

    with pytest.raises(ValueError, match="provider identity"):
        manifest_from_row(
            row,
            stream_id=COMPLETION_MANIFEST_STREAM_ID,
            chain_index=2,
            chain_hash="e" * 64,
        )


def test_a_well_formed_v2_row_still_round_trips_to_proven() -> None:
    """Satisfiability, so the refusals above are attributable rather than blanket."""
    row = manifest_row(_manifest())
    loaded = manifest_from_row(
        row,
        stream_id=COMPLETION_MANIFEST_STREAM_ID,
        chain_index=2,
        chain_hash="e" * 64,
    )

    assert row["schema_version"] == SCHEMA_VERSION
    assert _evaluate(loaded).status == "proven"
