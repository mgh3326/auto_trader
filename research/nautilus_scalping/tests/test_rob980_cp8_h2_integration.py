"""ROB-980 CP8: merged ROB-979 DTO/engine integration through one adapter."""

from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
from dataclasses import dataclass
from pathlib import Path

import pytest
import rob941_offline_loader as loader
import rob974_h3_smoke as h3_smoke
from rob941_manifest import CorpusManifest
from rob974_h3_evidence import build_unique_generator_evidence
from rob974_h3_manifest import SYMBOLS, S3Config, S4Config, get_config
from rob974_h3_s3 import generate_s3_global
from rob974_h3_s4 import generate_s4_global

_ADAPTER_NAME = "rob974_h3_h2_adapter"
_ADAPTER_SPEC = importlib.util.find_spec(_ADAPTER_NAME)
adapter = importlib.import_module(_ADAPTER_NAME) if _ADAPTER_SPEC is not None else None

_FOLD_ID = "rob941-shaped-persisted-synthetic"
_MINUTE_MS = 60_000


def test_cp8_adapter_behavior_is_implemented():
    assert adapter is not None, "ROB-980 CP8 merged-H2 integration is not implemented"


@pytest.fixture(scope="module")
def pipeline(tmp_path_factory):
    assert adapter is not None
    root = tmp_path_factory.mktemp("rob980-cp8-persisted")

    # Preserve the CP7 fixture byte-for-byte while extending a CP8-only persisted
    # corpus by two complete 4h bars.  At the first new bar, market=-20% and the
    # residual rises +20%: XRP stays flat while DOGE/SOL fall, making the next
    # synchronized uppercase M negative without stopping the accepted S3 long.
    base_market = h3_smoke._market_returns()
    base_residual = h3_smoke._residual_states()
    original_count = h3_smoke._SMOKE_4H_BARS
    original_market = h3_smoke._market_returns
    original_residual = h3_smoke._residual_states
    try:
        h3_smoke._SMOKE_4H_BARS = original_count + 2
        h3_smoke._market_returns = lambda: base_market + (-0.20, 0.0)
        h3_smoke._residual_states = lambda: base_residual + (0.24, 0.24)
        manifest = h3_smoke._persist_synthetic_corpus(root)
    finally:
        h3_smoke._SMOKE_4H_BARS = original_count
        h3_smoke._market_returns = original_market
        h3_smoke._residual_states = original_residual

    manifest_path = root / "rob980-smoke-manifest.json"
    reloaded = CorpusManifest.load(manifest_path)
    assert manifest.content_hash() == reloaded.content_hash()
    loaded = loader.load_corpus(reloaded, root)
    minutes = h3_smoke._selected_minutes(loaded)
    context, feature_hash = h3_smoke._context(minutes)

    s3_config = get_config("S3-05")
    s4_config = get_config("S4-01")
    assert type(s3_config) is S3Config
    assert type(s4_config) is S4Config
    emit_window = h3_smoke._smoke_emit_window()
    s3_output = generate_s3_global(context, emit_window, s3_config)
    s4_output = generate_s4_global(context, emit_window, s4_config)
    s3_evidence = build_unique_generator_evidence(
        s3_output, fold_or_full_window=_FOLD_ID, phase="offline_smoke"
    )
    s4_evidence = build_unique_generator_evidence(
        s4_output, fold_or_full_window=_FOLD_ID, phase="offline_smoke"
    )
    corpus_end_ts = max(rows[-1].ts for rows in minutes.values()) + _MINUTE_MS
    integration = adapter.run_h2_integration(
        s3_output,
        s4_output,
        minutes,
        context,
        fold_id=_FOLD_ID,
        corpus_end_ts=corpus_end_ts,
    )
    return {
        "root": root,
        "minutes": minutes,
        "context": context,
        "feature_hash": feature_hash,
        "s3_config": s3_config,
        "s4_config": s4_config,
        "s3_output": s3_output,
        "s4_output": s4_output,
        "s3_evidence": s3_evidence,
        "s4_evidence": s4_evidence,
        "corpus_end_ts": corpus_end_ts,
        "integration": integration,
    }


def test_real_persisted_h1_h3_h2_path_is_non_vacuous(pipeline):
    result = pipeline["integration"]
    for strategy in ("s3", "s4"):
        evidence = pipeline[f"{strategy}_evidence"]
        assert evidence.candidate > 0
        assert evidence.generator_rejected > 0
        assert evidence.generator_accepted > 0
        assert len(getattr(result, f"{strategy}_engine_result").trades) > 0

    assert any(
        trade.exit_reason == "THESIS_EXIT" for trade in result.s3_engine_result.trades
    )
    assert result.s4_engine_result.trades
    assert all(len(trade.pair) == 2 for trade in result.s4_engine_result.trades)
    assert result.s3_scenario_rows
    assert result.s4_scenario_rows


def test_missing_minute_remains_h3_no_signal_and_later_closes_trade(pipeline):
    gap_close = (
        h3_smoke.frozen.WINDOW_START_MS
        + (h3_smoke._GAP_BAR_INDEX + 1) * h3_smoke.FOUR_HOUR_MS
    )
    s3_gap = [
        item
        for item in pipeline["s3_output"].decisions
        if item.decision_ts == gap_close and item.symbol == "SOLUSDT"
    ]
    assert len(s3_gap) == 1
    assert s3_gap[0].status == "NO_SIGNAL"
    assert s3_gap[0].no_signal_reason == "missing_required_context"
    assert any(
        trade.signal_ts > gap_close
        for trade in pipeline["integration"].s3_engine_result.trades
    )
    assert any(
        trade.signal_ts > gap_close
        for trade in pipeline["integration"].s4_engine_result.trades
    )


def test_s3_mapping_preserves_distances_percentile_and_timestamps(pipeline):
    output = pipeline["s3_output"]
    intents = pipeline["integration"].s3_intents
    assert len(intents) == len(output.accepted)
    for candidate, intent in zip(output.accepted, intents, strict=True):
        assert type(intent.signal_ts) is int
        assert intent.signal_ts == candidate.decision_ts == candidate.entry_tick_ts
        assert intent.entry_sl_distance.hex() == candidate.d_SL.hex()
        assert intent.entry_tp_distance.hex() == candidate.d_TP.hex()
        assert (
            intent.volatility_percentile.hex() == candidate.volatility_percentile.hex()
        )
        assert intent.fold_id == _FOLD_ID


def test_s4_mapping_uses_effective_mad_and_signed_observed_z(pipeline):
    output = pipeline["s4_output"]
    intents = pipeline["integration"].s4_intents
    assert len(intents) == len(output.accepted)
    for candidate, intent in zip(output.accepted, intents, strict=True):
        assert intent.pair == (candidate.symbol_a, candidate.symbol_b)
        assert intent.side_a == candidate.side_a
        assert intent.side_b == candidate.side_b
        assert intent.weight_a.hex() == candidate.weight_a.hex()
        assert intent.weight_b.hex() == candidate.weight_b.hex()
        assert intent.beta_a.hex() == candidate.beta_a.hex()
        assert intent.beta_b.hex() == candidate.beta_b.hex()
        assert intent.mu.hex() == candidate.mu.hex()
        assert intent.sigma.hex() == candidate.effective_mad_scale.hex()
        assert intent.z_entry.hex() == candidate.observed_z.hex()
        assert intent.gross_notional.hex() == candidate.gross_notional_usd.hex()
        assert intent.entry_sl_distance.hex() == candidate.d_SL.hex()
        assert intent.entry_tp_distance.hex() == candidate.d_TP.hex()
    assert any(
        intent.sigma.hex() != candidate.sigma_pair_risk.hex()
        for candidate, intent in zip(output.accepted, intents, strict=True)
    )
    assert any(
        intent.z_entry.hex() != pipeline["s4_config"].z_entry.hex()
        for intent in intents
    )


def test_real_trade_null_type_timestamp_and_provenance_are_exact(pipeline):
    result = pipeline["integration"]
    thesis = next(
        trade
        for trade in result.s3_engine_result.trades
        if trade.exit_reason == "THESIS_EXIT"
    )
    assert type(thesis.signal_ts) is int
    assert type(thesis.entry_ts) is int
    assert type(thesis.entry_price) is float
    assert type(thesis.volatility_percentile) is float
    assert thesis.entry_ts == thesis.signal_ts

    for trade in result.s4_engine_result.trades:
        assert type(trade.signal_ts) is int
        assert type(trade.entry_ts) is int
        assert type(trade.sigma) is float
        assert type(trade.z_entry) is float
        assert trade.entry_ts == trade.signal_ts
        assert trade.order_id_a is None
        assert trade.order_id_b is None
        assert trade.pair_executor_validated is False
        assert trade.demo_eligible is False
        assert trade.pair_exec_status == "historical_atomic_assumption"
        assert trade.pair_exec_fail == "not_evaluated"
        assert trade.promotion_status == "promotion_blocked_pending_pair_executor"
        assert trade.volatility_percentile is None
        assert trade.volatility_percentile_provenance == "not_defined_for_s4"


def test_same_inputs_and_mapping_permutation_have_identical_bytes_and_hashes(pipeline):
    first = pipeline["integration"]
    kwargs = {
        "fold_id": _FOLD_ID,
        "corpus_end_ts": pipeline["corpus_end_ts"],
    }
    second = adapter.run_h2_integration(
        pipeline["s3_output"],
        pipeline["s4_output"],
        pipeline["minutes"],
        pipeline["context"],
        **kwargs,
    )
    permuted = adapter.run_h2_integration(
        pipeline["s3_output"],
        pipeline["s4_output"],
        {symbol: pipeline["minutes"][symbol] for symbol in reversed(SYMBOLS)},
        pipeline["context"],
        **kwargs,
    )
    assert first.to_json_bytes() == second.to_json_bytes() == permuted.to_json_bytes()
    assert first.content_hash == second.content_hash == permuted.content_hash
    assert first.s3_ledger_hash == second.s3_ledger_hash
    assert first.s4_ledger_hash == second.s4_ledger_hash


def test_actual_integration_bytes_hashes_and_exits_are_pinned(pipeline):
    result = pipeline["integration"]
    assert pipeline["feature_hash"] == (
        "00a382c6fd92ad8c883d5b6290fb78acd0dbf7ee7c2ecc824b91fa023100f7ad"
    )
    assert result.content_hash == (
        "3a82be84136f762598e3b8d0cd2d4c1d18bbfc97d6a224f15ac5b27529670b87"
    )
    assert len(result.to_json_bytes()) == 10_523
    assert result.s3_ledger_hash == (
        "eeb95ba24b5dc9e0c534f22b886ee191810dee395fa96047315205b92094d2c2"
    )
    assert result.s4_ledger_hash == (
        "5c37e9b9156e9ffbaecbc0cdba0457eba3cf10f36e7e47d3e0a55895628791c9"
    )
    assert tuple(
        (trade.symbol, trade.signal_ts, trade.exit_ts, trade.exit_reason)
        for trade in result.s3_engine_result.trades
    ) == (("XRPUSDT", 1_754_236_800_000, 1_754_251_200_000, "THESIS_EXIT"),)
    assert tuple(
        (trade.pair, trade.signal_ts, trade.exit_ts, trade.exit_reason)
        for trade in result.s4_engine_result.trades
    ) == (
        (
            ("XRPUSDT", "SOLUSDT"),
            1_753_358_400_000,
            1_753_373_520_000,
            "TP",
        ),
        (
            ("XRPUSDT", "SOLUSDT"),
            1_753_545_600_000,
            1_753_574_400_000,
            "SL",
        ),
        (
            ("XRPUSDT", "SOLUSDT"),
            1_754_236_800_000,
            1_754_237_220_000,
            "SL",
        ),
    )


def test_contract_drift_is_named_and_fail_closed(monkeypatch):
    assert adapter is not None

    @dataclass(frozen=True)
    class DriftedS4Intent:
        pair: tuple[str, str]
        signal_ts: int

    monkeypatch.setattr(adapter, "S4PairSignalIntent", DriftedS4Intent)
    with pytest.raises(adapter.ContractDriftError, match=r"^CONTRACT_DRIFT:"):
        adapter.verify_h2_contract()


def test_adapter_is_the_only_h3_module_with_concrete_h2_imports():
    source_root = Path(__file__).resolve().parents[1]
    offenders = []
    for path in sorted(source_root.glob("rob974_h3_*.py")):
        if path.name == "rob974_h3_h2_adapter.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names = (node.module or "",)
            else:
                continue
            if any(name.startswith("rob974_h2") for name in names):
                offenders.append((path.name, node.lineno, names))
    assert offenders == []


def test_adapter_import_and_authority_surface_is_pure_and_bridge_owned():
    path = Path(__file__).resolve().parents[1] / "rob974_h3_h2_adapter.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported = []
    forbidden_calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
        elif isinstance(node, ast.Call):
            function = node.func
            if isinstance(function, ast.Name) and function.id in {
                "__import__",
                "eval",
                "exec",
            }:
                forbidden_calls.append((node.lineno, function.id))
            if isinstance(function, ast.Attribute) and function.attr in {
                "import_module",
                "now",
                "today",
                "utcnow",
                "getenv",
            }:
                forbidden_calls.append((node.lineno, function.attr))
    forbidden_roots = {
        "app",
        "sqlalchemy",
        "asyncpg",
        "psycopg",
        "redis",
        "taskiq",
        "celery",
        "httpx",
        "requests",
        "aiohttp",
        "urllib",
        "socket",
        "websockets",
        "boto3",
        "fastapi",
        "uvicorn",
        "random",
        "time",
        "datetime",
        "os",
    }
    assert not {name.split(".")[0] for name in imported} & forbidden_roots
    assert forbidden_calls == []
    assert "rob974_features" not in imported
    assert "rob974_h2_h1_bridge" in imported


def test_integration_seam_exposes_no_behavior_or_funding_callback():
    parameters = inspect.signature(adapter.run_h2_integration).parameters
    assert all("callback" not in name for name in parameters)
    assert "funding_lookup" not in parameters
