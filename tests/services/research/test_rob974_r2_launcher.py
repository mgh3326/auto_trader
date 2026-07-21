"""ROB-974 R2 launcher default-off and full-fold launch-mode contracts."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
from types import ModuleType

import pytest
from funding_oi_archive import FundingRow
from rob941_funding_sidecar import FundingSidecar
from rob974_features import MINUTE_MS, SYMBOLS, MinuteBar
from rob974_h2_dtos import S3SignalIntent
from rob974_h4_contracts import exact_h4_folds

from app.services.rob974_h6b_materializer import (
    ActualH4InputData,
    ActualMergedH4Runner,
    H6BPlanError,
)

_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _ROOT / "scripts" / "run_rob974_r2_campaign.py"


def _launcher() -> ModuleType:
    spec = importlib.util.spec_from_file_location("rob974_r2_launcher", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _one_bar_input() -> ActualH4InputData:
    start = 1_751_328_000_000
    rows = {symbol: (MinuteBar(start, 1.0, 1.0, 1.0, 1.0, 1.0),) for symbol in SYMBOLS}
    return ActualH4InputData.from_mapping(
        rows,
        corpus_end_ts=start + MINUTE_MS,
        persisted_corpus_hash="a" * 64,
        persisted_feature_hash="b" * 64,
    )


def _funded_one_bar_input(rate: float) -> ActualH4InputData:
    start = 1_751_328_000_000
    rows = {symbol: (MinuteBar(start, 1.0, 1.0, 1.0, 1.0, 1.0),) for symbol in SYMBOLS}
    sidecars = {
        symbol: FundingSidecar.from_rows(symbol, (FundingRow(start, 8, rate),))
        for symbol in SYMBOLS
    }
    return ActualH4InputData.from_mapping(
        rows,
        corpus_end_ts=start + MINUTE_MS,
        persisted_corpus_hash="a" * 64,
        persisted_feature_hash="b" * 64,
        funding_sidecars=sidecars,
    )


def _sealed_arguments(launcher: ModuleType) -> list[str]:
    return [
        "--run",
        "--write-opt-in",
        launcher.WRITE_OPT_IN,
        "--confirm-full-corpus-pit",
        launcher.PIT_CONFIRMATION,
        "--expected-full-campaign-hash",
        launcher.FULL_CAMPAIGN_HASH,
        "--campaign-run-id",
        launcher.CAMPAIGN_RUN_ID,
        "--expected-mapping-hash",
        launcher.EXACT_48_MAPPING_HASH,
        "--integration-head-sha",
        "1" * 40,
        "--integration-tree-sha",
        "2" * 40,
        "--launcher-sha256",
        hashlib.sha256(_SCRIPT.read_bytes()).hexdigest(),
        "--feature-source-sha256",
        launcher.FEATURE_SOURCE_SHA256,
        "--engine-source-sha256",
        launcher.ENGINE_SOURCE_SHA256,
        "--runner-source-sha256",
        launcher.RUNNER_SOURCE_SHA256,
        "--pbo-implementation-sha256",
        launcher.PBO_IMPLEMENTATION_SHA256,
        "--approved-db-host",
        "localhost",
        "--approved-db-port",
        "5432",
        "--approved-db-name",
        "rob974_db",
        "--approved-db-user",
        "postgres",
        "--database-url-env",
        launcher.DATABASE_URL_ENV,
        "--manifest",
        str(launcher.EXPECTED_MANIFEST),
        "--corpus-root",
        str(launcher.EXPECTED_CORPUS_ROOT),
        "--output-root",
        str(launcher.EXPECTED_OUTPUT_ROOT),
        "--one-shot-approval",
        launcher.ONE_SHOT_APPROVAL,
    ]


def test_no_arguments_are_dry_run_only(monkeypatch: pytest.MonkeyPatch) -> None:
    launcher = _launcher()
    stdout = io.StringIO()
    stderr = io.StringIO()

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("no-argument path reached an effectful boundary")

    monkeypatch.setattr(launcher, "_require_exact_static_gates", forbidden)
    monkeypatch.setattr(launcher, "_install_runtime_paths", forbidden)
    assert launcher.run_cli((), stdout=stdout, stderr=stderr, environ={}) == 0
    payload = json.loads(stdout.getvalue())
    assert payload["default_state"] == "DISABLED"
    assert payload["run_requested"] is False
    assert payload["identity"]["attempts"] == 48
    assert payload["target"]["required_runner_width"] == 64
    assert payload["target"]["required_alembic_head"] == (
        "20260722_rob1023_widen_runner"
    )
    assert str(payload["target"]["output_root"]).endswith("-v3")
    assert all(value == 0 for value in payload["effects"].values())
    assert stderr.getvalue() == ""


def test_partial_run_arguments_fail_before_runtime() -> None:
    launcher = _launcher()
    stdout = io.StringIO()
    stderr = io.StringIO()
    assert (
        launcher.run_cli(("--run",), stdout=stdout, stderr=stderr, environ={})
        == launcher.CLI_USAGE_OR_PLAN_ERROR
    )
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == "CLI_USAGE_OR_PLAN_ERROR\n"


def test_schema_guard_only_requires_database_url_without_other_effects() -> None:
    launcher = _launcher()
    stdout = io.StringIO()
    stderr = io.StringIO()
    assert (
        launcher.run_cli(
            (launcher.SCHEMA_GUARD_ONLY_ARGUMENT,),
            stdout=stdout,
            stderr=stderr,
            environ={},
        )
        == launcher.AUTHORITY_OR_PREFLIGHT_REFUSED
    )
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == (
        "AUTHORITY_OR_PREFLIGHT_REFUSED DATABASE_URL_ENV_ABSENT\n"
    )


@pytest.mark.asyncio
async def test_live_schema_guard_pins_runner_width_and_alembic_head() -> None:
    launcher = _launcher()

    class _Result:
        def __init__(self, row: tuple[object, ...] | None) -> None:
            self._row = row

        def one_or_none(self) -> tuple[object, ...] | None:
            return self._row

    class _Session:
        def __init__(self, row: tuple[object, ...] | None) -> None:
            self.row = row
            self.statements: list[str] = []

        async def execute(self, statement: object) -> _Result:
            self.statements.append(str(statement))
            return _Result(self.row)

    expected = (
        "rob974_db",
        "postgres",
        launcher.EXPECTED_BACKTEST_RUNNER_WIDTH,
        launcher.EXPECTED_ALEMBIC_HEAD,
    )
    good = _Session(expected)
    await launcher._fetch_and_validate_live_schema(good)
    assert len(good.statements) == 1
    assert "information_schema.columns" in good.statements[0]
    assert "column_info.column_name = 'runner'" in good.statements[0]

    for row, reason in (
        (None, "LIVE_DATABASE_SCHEMA_RUNNER_COLUMN_MISSING"),
        ((*expected[:2], 16, expected[3]), "RUNNER_WIDTH_MISMATCH"),
        ((*expected[:3], "stale_head"), "ALEMBIC_HEAD_MISMATCH"),
    ):
        with pytest.raises(launcher.LaunchRefused, match=reason):
            await launcher._fetch_and_validate_live_schema(_Session(row))


def test_sealed_arguments_without_secret_dsn_fail_before_runtime_imports() -> None:
    launcher = _launcher()
    stdout = io.StringIO()
    stderr = io.StringIO()
    assert (
        launcher.run_cli(
            _sealed_arguments(launcher),
            stdout=stdout,
            stderr=stderr,
            environ={},
        )
        == launcher.AUTHORITY_OR_PREFLIGHT_REFUSED
    )
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == (
        "AUTHORITY_OR_PREFLIGHT_REFUSED DATABASE_URL_ENV_ABSENT\n"
    )


def test_actual_h4_runner_defaults_bounded_and_launcher_can_enable_all_folds() -> None:
    folds = exact_h4_folds()
    bounded = ActualMergedH4Runner(_one_bar_input())
    full = ActualMergedH4Runner(_one_bar_input(), execute_all_folds=True)
    assert bounded._fold_is_active(folds[0]) is True
    assert all(not bounded._fold_is_active(fold) for fold in folds[1:])
    assert all(full._fold_is_active(fold) for fold in folds)


def test_actual_h4_runner_rejects_truthy_non_bool_full_fold_gate() -> None:
    with pytest.raises(H6BPlanError, match="execute_all_folds must be exact bool"):
        ActualMergedH4Runner(_one_bar_input(), execute_all_folds=1)  # type: ignore[arg-type]


def test_production_runner_requires_and_applies_pit_funding() -> None:
    with pytest.raises(H6BPlanError, match="required PIT funding sidecars are absent"):
        ActualMergedH4Runner(_one_bar_input(), require_pit_funding=True)

    start = 1_751_328_000_000
    runner = ActualMergedH4Runner(
        _funded_one_bar_input(0.0004), require_pit_funding=True
    )
    intent = S3SignalIntent(
        symbol="XRPUSDT",
        side="long",
        signal_ts=start,
        entry_sl_distance=0.01,
        entry_tp_distance=0.02,
        config_id="S3-00",
        fold_id="fold-00",
        volatility_percentile=0.5,
    )
    terminal = runner._invoke_s3(
        intents=(intent,),
        minute_index={("XRPUSDT", start): object()},
        close_feature_index={},
        horizon_end_ts=start + 48 * 3_600_000,
        strategy="S3",
        config_id="S3-00",
        fold_id="fold-00",
    )
    assert terminal.result.trades == ()
    assert tuple(row.reason for row in terminal.result.no_trades) == (
        "expected_funding_cost_above_3bps",
    )
    assert runner._funding_lookup is not None
    crossings = runner._funding_lookup("XRPUSDT", "long", start, start + 1)
    assert tuple(item.rate_bps for item in crossings) == (4.0,)


def test_launcher_reuses_materializer_and_has_no_broker_surface() -> None:
    source = _SCRIPT.read_text(encoding="utf-8")
    assert "materialize_production" in source
    assert "build_production_execution_plan" in source
    assert "DirectoryAtomicArtifactPort" in source
    assert "execute_all_folds=True" in source
    assert "require_pit_funding=True" in source
    assert "taskiq" not in source.lower()
    assert "broker" not in "\n".join(
        line
        for line in source.lower().splitlines()
        if line.startswith(("import ", "from "))
    )
