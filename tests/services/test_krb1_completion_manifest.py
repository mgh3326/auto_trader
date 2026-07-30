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
    CompletionManifest,
    DbDailyBar,
    RawDailyBar,
    append_completion_manifest,
    build_completion_manifest,
    compute_manifest_hash,
    compute_universe_hash,
    evaluate_completion_manifest,
    load_latest_completion_manifest,
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
