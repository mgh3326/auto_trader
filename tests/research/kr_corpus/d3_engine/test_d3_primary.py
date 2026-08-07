from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from decimal import Decimal, localcontext
from pathlib import Path, PurePosixPath

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from research.kr_corpus.d3_engine.acceptance import _contract_signal_bars
from research.kr_corpus.d3_engine.canonical import canonical_bytes
from research.kr_corpus.d3_engine.constants import DECIMAL_PRECISION, DECIMAL_ROUNDING
from research.kr_corpus.d3_engine.engine import PortfolioEngine
from research.kr_corpus.d3_engine.guards import (
    SealedAccessBlocked,
    SealedAccessGuard,
    SealedAccessSpy,
)
from research.kr_corpus.d3_engine.indicators import rsi_wilder
from research.kr_corpus.d3_engine.models import Arm, CashflowView, DataView
from research.kr_corpus.d3_engine.primary import (
    PhysicalRun,
    PrimaryRunInvalid,
    _funding_p05,
    _seal_determinism,
    measure_primary_run_executed,
    primary_matrix,
)
from research.kr_corpus.d3_engine.primary_corpus import (
    CORPUS_RUN_ID,
    CorpusBar,
    PrimaryCorpusLoader,
    PrimaryCorpusPaths,
    _prepare_signal_tape,
    _WilderState,
)
from research.kr_corpus.d3_engine.tick import TickTable


def _ticks() -> TickTable:
    return TickTable.from_mapping(
        {
            "schema_version": "d3.krx_tick_table.v1",
            "bands": [
                {"lower_inclusive": 0, "upper_exclusive": 2000, "tick": 1},
                {"lower_inclusive": 2000, "upper_exclusive": 5000, "tick": 5},
                {"lower_inclusive": 5000, "upper_exclusive": 20000, "tick": 10},
                {"lower_inclusive": 20000, "upper_exclusive": 50000, "tick": 50},
                {
                    "lower_inclusive": 50000,
                    "upper_exclusive": 200000,
                    "tick": 100,
                },
                {
                    "lower_inclusive": 200000,
                    "upper_exclusive": 500000,
                    "tick": 500,
                },
                {"lower_inclusive": 500000, "upper_exclusive": None, "tick": 1000},
            ],
        }
    )


def _corpus_bar(raw: object, *, market: str = "KOSPI") -> CorpusBar:
    return CorpusBar(
        session=raw.session,  # type: ignore[attr-defined]
        symbol=raw.symbol,  # type: ignore[attr-defined]
        market=market,
        open_int=int(raw.open),  # type: ignore[attr-defined]
        high_int=int(raw.high),  # type: ignore[attr-defined]
        low_int=int(raw.low),  # type: ignore[attr-defined]
        close_int=int(raw.close),  # type: ignore[attr-defined]
    )


def test_primary_matrix_is_exactly_four_by_two_by_two() -> None:
    matrix = primary_matrix()

    assert len(matrix) == 16
    assert len({run.run_id for run in matrix}) == 16
    assert {run.arm for run in matrix} == set(Arm)
    assert {run.cashflow_view for run in matrix} == set(CashflowView)
    assert {run.data_view for run in matrix} == {
        DataView.ORIGINAL_VALID_BAR,
        DataView.CLAMP_ADMIT_V1,
    }
    assert len({(run.arm, run.cashflow_view) for run in matrix}) == 8


def test_corpus_partition_accepts_frozen_krx_alphanumeric_issue_codes() -> None:
    assert PrimaryCorpusLoader._partition(
        PurePosixPath("dataset/market=KOSPI/year=2024/ticker=08537M.parquet")
    ) == ("KOSPI", 2024, "08537M")


def test_funding_p05_replays_without_future_contribution() -> None:
    daily = [
        {
            "session": date(2015, 1, 31),
            "session_index": 0,
            "settled_orderable_cash": Decimal(100),
            "reserved_orders": [],
            "buy_payables": [],
            "sell_receivables": [],
        },
        {
            "session": date(2015, 3, 1),
            "session_index": 1,
            "settled_orderable_cash": Decimal(600),
            "reserved_orders": [],
            "buy_payables": [],
            "sell_receivables": [],
        },
        {
            "session": date(2015, 5, 1),
            "session_index": 2,
            "settled_orderable_cash": Decimal(600),
            "reserved_orders": [],
            "buy_payables": [],
            "sell_receivables": [],
        },
    ]
    events = [
        {
            "event": "monthly_contribution_pre_open",
            "session": date(2015, 3, 1),
            "amount": Decimal(1000),
        },
        {
            "event": "order_submitted",
            "session": date(2015, 3, 1),
            "order_id": "BUY-1",
            "symbol": "005930",
            "side": "buy",
            "class": "new",
            "rung": "L1",
            "limit": Decimal(500),
            "quantity": 1,
        },
    ]

    result = _funding_p05(daily, events)

    assert result["p05_days"] == 29
    assert result["complete_anchor_count"] == 1
    assert result["right_censored_anchor_count"] == 2
    assert result["anchors"][0]["first_cash_reject"] == date(2015, 3, 1)
    assert result["anchors"][0]["missed_notional_at_first_reject"] == Decimal(500)


def test_precomputed_signal_tape_is_exact_engine_equivalent() -> None:
    sessions = tuple(date(2015, 1, 1) + timedelta(days=index) for index in range(121))
    raw_bars = _contract_signal_bars(list(sessions), symbols=("005930",))
    bars = [_corpus_bar(raw) for raw in raw_bars]
    positions = {session: index for index, session in enumerate(sessions)}

    tape = _prepare_signal_tape({"005930": bars}, positions)
    engine = PortfolioEngine(_ticks())

    for index, bar in enumerate(bars):
        expected = engine._signal_for_session(bars, index)  # type: ignore[arg-type]
        snapshot = tape.get((bar.session, bar.symbol))
        actual = (
            None
            if snapshot is None
            else (
                snapshot.rsi,
                snapshot.l2_price,
                snapshot.fib_high,
                snapshot.fib_low,
            )
        )
        assert actual == expected
    assert len(tape) == 1


def test_signal_tape_resets_at_missing_xkrx_session() -> None:
    sessions = tuple(date(2015, 1, 1) + timedelta(days=index) for index in range(260))
    present = sessions[:121] + sessions[122:]
    bars = [
        CorpusBar(
            session=session,
            symbol="005930",
            market="KOSPI",
            open_int=10_000,
            high_int=10_100,
            low_int=9_900,
            close_int=10_000 - (index % 50),
        )
        for index, session in enumerate(present)
    ]
    positions = {session: index for index, session in enumerate(sessions)}

    tape = _prepare_signal_tape({"005930": bars}, positions)

    assert not any(sessions[122] <= session <= sessions[241] for session, _ in tape)


def test_incremental_wilder_state_is_exact_for_long_variable_history() -> None:
    closes = [10_000 + ((index * 97) % 701) - index for index in range(400)]
    state = _WilderState()
    seen: list[Decimal] = []

    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        context.rounding = DECIMAL_ROUNDING
        for close in closes:
            expected = rsi_wilder(seen)[-1] if seen else None
            assert state.value == expected
            state.add(close)
            seen.append(Decimal(close))


def _write_corpus_view(
    root: Path,
    *,
    data_view: DataView,
    session: str = "2015-01-02",
) -> tuple[str, str]:
    relative = Path("dataset/market=KOSPI/year=2015/ticker=005930.parquet")
    path = root / relative
    path.parent.mkdir(parents=True)
    row: dict[str, object] = {
        "session": session,
        "market": "KOSPI",
        "ticker": "005930",
        "open": 100,
        "high": 103,
        "low": 98,
        "close": 101,
    }
    if data_view is DataView.CLAMP_ADMIT_V1:
        row.update(
            {
                "source_high": 105,
                "source_low": 96,
                "clamped": True,
                "clamp_delta_high": 2,
                "clamp_delta_low": 2,
                "clamp_classification": "both",
                "admitted": True,
            }
        )
    pq.write_table(pa.Table.from_pylist([row]), path)
    parquet_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    checksums_raw = f"{parquet_sha}  {relative.as_posix()}\n".encode()
    (root / "checksums.sha256").write_bytes(checksums_raw)
    checksums_sha = hashlib.sha256(checksums_raw).hexdigest()
    manifest: dict[str, object] = {"scope": "main_only"}
    if data_view is DataView.CLAMP_ADMIT_V1:
        manifest.update(
            {
                "source_corpus_id": "kr-corpus-v1",
                "source_run_id": CORPUS_RUN_ID,
                "source_manifest_sha256": "ORIGINAL_MANIFEST_SHA",
                "checksums_sha256": checksums_sha,
                "source_valid_bar_view_unchanged": True,
            }
        )
    manifest_raw = canonical_bytes(manifest)
    (root / "manifest.json").write_bytes(manifest_raw)
    return hashlib.sha256(manifest_raw).hexdigest(), checksums_sha


def test_loader_measures_actual_safe_manifest_parquet_bar_and_metadata_reads(
    tmp_path: Path,
) -> None:
    original_root = tmp_path / "kr-corpus-main"
    derived_root = tmp_path / "derived-views" / "clamp-admit-v1"
    original_manifest, original_checksums = _write_corpus_view(
        original_root,
        data_view=DataView.ORIGINAL_VALID_BAR,
    )
    derived_manifest, derived_checksums = _write_corpus_view(
        derived_root,
        data_view=DataView.CLAMP_ADMIT_V1,
    )
    derived_payload = json.loads((derived_root / "manifest.json").read_bytes())
    derived_payload["source_manifest_sha256"] = original_manifest
    derived_raw = canonical_bytes(derived_payload)
    (derived_root / "manifest.json").write_bytes(derived_raw)
    derived_manifest = hashlib.sha256(derived_raw).hexdigest()
    spy = SealedAccessSpy()
    loader = PrimaryCorpusLoader(
        paths=PrimaryCorpusPaths(original_root, derived_root),
        guard=SealedAccessGuard(spy),
        bindings={
            "original_manifest_sha256": original_manifest,
            "original_checksums_sha256": original_checksums,
            "derived_manifest_sha256": derived_manifest,
            "derived_checksums_sha256": derived_checksums,
        },
        expected_rows={
            DataView.ORIGINAL_VALID_BAR: 1,
            DataView.CLAMP_ADMIT_V1: 1,
        },
    )
    sessions = (date(2015, 1, 2),)

    original = loader.load(DataView.ORIGINAL_VALID_BAR, market_sessions=sessions)
    clamp = loader.load(DataView.CLAMP_ADMIT_V1, market_sessions=sessions)

    assert original.row_count == clamp.row_count == 1
    assert len(clamp.clamp_rows) == 1
    assert spy.evidence() == {
        "sealed_access_spy": 0,
        "sealed_access_blocked_attempts": 0,
        "sealed_access_path_checks": 8,
        "sealed_access_date_checks": 2,
        "sealed_access_metadata_key_checks": 6,
        "measured_file_reads": 8,
        "measured_manifest_reads": 2,
        "measured_parquet_reads": 2,
        "measured_bar_rows_read": 2,
        "measured_metadata_key_reads": 6,
        "sealed_file_reads": 0,
        "sealed_manifest_reads": 0,
        "sealed_parquet_reads": 0,
        "sealed_bar_rows_read": 0,
        "sealed_metadata_key_reads": 0,
    }


def test_loader_blocks_sealed_root_before_manifest_read(tmp_path: Path) -> None:
    sealed = tmp_path / "holdout" / "corpus"
    (sealed / "dataset").mkdir(parents=True)
    (sealed / "manifest.json").write_bytes(b"{}")
    (sealed / "checksums.sha256").write_bytes(b"")
    spy = SealedAccessSpy()
    loader = PrimaryCorpusLoader(
        paths=PrimaryCorpusPaths(sealed, sealed),
        guard=SealedAccessGuard(spy),
        bindings={
            "original_manifest_sha256": "0" * 64,
            "original_checksums_sha256": "0" * 64,
            "derived_manifest_sha256": "0" * 64,
            "derived_checksums_sha256": "0" * 64,
        },
        expected_rows={
            DataView.ORIGINAL_VALID_BAR: 0,
            DataView.CLAMP_ADMIT_V1: 0,
        },
    )

    with pytest.raises(SealedAccessBlocked):
        loader.load(
            DataView.ORIGINAL_VALID_BAR,
            market_sessions=(date(2015, 1, 2),),
        )

    assert spy.sealed_reads == 0
    assert spy.blocked_attempts == 1
    assert spy.actual_file_reads == 0


def test_loader_counts_a_decoded_sealed_bar_as_actual_access(tmp_path: Path) -> None:
    root = tmp_path / "safe-named-main"
    manifest, checksums = _write_corpus_view(
        root,
        data_view=DataView.ORIGINAL_VALID_BAR,
        session="2025-01-02",
    )
    spy = SealedAccessSpy()
    loader = PrimaryCorpusLoader(
        paths=PrimaryCorpusPaths(root, root),
        guard=SealedAccessGuard(spy),
        bindings={
            "original_manifest_sha256": manifest,
            "original_checksums_sha256": checksums,
            "derived_manifest_sha256": manifest,
            "derived_checksums_sha256": checksums,
        },
        expected_rows={
            DataView.ORIGINAL_VALID_BAR: 1,
            DataView.CLAMP_ADMIT_V1: 1,
        },
    )

    with pytest.raises(SealedAccessBlocked, match="sealed bar date observed"):
        loader.load(
            DataView.ORIGINAL_VALID_BAR,
            market_sessions=(date(2025, 1, 2),),
        )

    assert spy.sealed_reads == 1
    assert spy.sealed_bar_rows_read == 1
    assert spy.parquet_reads == 1


def test_primary_run_measurement_is_derived_from_all_verified_bundles(
    tmp_path: Path,
) -> None:
    completed: list[dict[str, object]] = []
    for physical in primary_matrix():
        base = {
            "payload.json": canonical_bytes(
                {
                    "schema_version": "d3.primary_run.v1",
                    "artifact_kind": "payload",
                    "run_id": physical.run_id,
                    "stamps": {},
                    "rows": [],
                }
            )
        }
        base["run.json"] = canonical_bytes(
            {
                "schema_version": "d3.primary_run.v1",
                "artifact_kind": "physical_run",
                "run_id": physical.run_id,
                "stamps": {},
                "content_checksums": {
                    "payload.json": hashlib.sha256(base["payload.json"]).hexdigest()
                },
                "engine_invocations": 1,
                "physical_run_completed": True,
            }
        )
        bundle = _seal_determinism(
            physical=physical,
            first=base,
            second=dict(base),
            stamps={},
        )
        run_root = tmp_path / "runs" / physical.run_id
        run_root.mkdir(parents=True)
        for name, raw in bundle.items():
            (run_root / name).write_bytes(raw)
        run_raw = bundle["run.json"]
        completed.append(
            {
                "run_id": physical.run_id,
                "arm": physical.arm.value,
                "cashflow_view": physical.cashflow_view.value,
                "data_view": physical.data_view.value,
                "deterministic_2runs": True,
                "run_json_sha256": hashlib.sha256(run_raw).hexdigest(),
                "bundle_sha256": _bundle_sha_for_test(bundle),
            }
        )
    manifest = {
        "matrix": {
            "physical_runs": 16,
            "deterministic_2runs": 16,
            "runs": completed,
        }
    }
    (tmp_path / "manifest.json").write_bytes(canonical_bytes(manifest))

    measured = measure_primary_run_executed(tmp_path)

    assert measured == {
        "primary_run_executed": True,
        "reason": "artifact_derived",
        "physical_runs": 16,
        "verified_engine_runs": 16,
        "expected_matrix_match": True,
    }

    corrupted = tmp_path / "runs" / primary_matrix()[0].run_id / "payload.json"
    corrupted.write_bytes(b"corrupt")
    assert measure_primary_run_executed(tmp_path)["primary_run_executed"] is False


def _bundle_sha_for_test(bundle: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name, raw in sorted(bundle.items()):
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(raw).digest())
    return digest.hexdigest()


def test_determinism_seal_rejects_mismatched_attempts() -> None:
    physical = PhysicalRun(
        Arm.B0,
        CashflowView.NO_CONTRIBUTION,
        DataView.ORIGINAL_VALID_BAR,
    )

    with pytest.raises(PrimaryRunInvalid, match="non-deterministic bundle"):
        _seal_determinism(
            physical=physical,
            first={"run.json": b"{}"},
            second={"run.json": b'{"different":true}'},
            stamps={},
        )
