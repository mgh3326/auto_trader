"""Tests for backtest CLI routing."""

import hashlib
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_backtest_module():
    module_path = (
        Path(__file__).resolve().parent.parent.parent / "backtest" / "backtest.py"
    )
    spec = importlib.util.spec_from_file_location("backtest_cli_module", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_main_routes_report_mode_with_output(monkeypatch) -> None:
    module = _load_backtest_module()
    captured: dict[str, object] = {}
    strategy_class = type("VerifiedStrategy", (), {})

    def fake_run_report(
        split: str,
        bar_interval: str,
        output: str,
        execution_cost,
        verified_strategy_class,
    ) -> None:
        captured["split"] = split
        captured["bar_interval"] = bar_interval
        captured["output"] = output
        captured["execution_cost"] = execution_cost
        captured["strategy_class"] = verified_strategy_class

    monkeypatch.setattr(module, "_run_report", fake_run_report)
    monkeypatch.setattr(
        module,
        "load_verified_strategy_class",
        lambda expected_strategy_sha256, expected_params_sha256: strategy_class,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "backtest.py",
            "--mode",
            "report",
            "--split",
            "test",
            "--output",
            "json",
            "--interval",
            "4h",
            "--fee-bps",
            "4",
            "--half-spread-bps",
            "1",
            "--slippage-bps",
            "3",
        ],
    )

    module.main()

    assert captured == {
        "split": "test",
        "bar_interval": "4h",
        "output": "json",
        "execution_cost": module.prepare.ExecutionCost(
            fee_rate=0.0004,
            half_spread_bps=1.0,
            slippage_bps=3.0,
        ),
        "strategy_class": strategy_class,
    }


def test_strategy_hash_mismatch_fails_before_candidate_source_executes(
    tmp_path: Path,
) -> None:
    module = _load_backtest_module()
    marker = tmp_path / "executed"
    strategy_path = tmp_path / "strategy.py"
    strategy_path.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n"
        "PARAMS = {}\nclass Strategy:\n    pass\n",
        encoding="utf-8",
    )

    with pytest.raises(module.StrategySourceMismatch, match="strategy_source_hash"):
        module.load_verified_strategy_class(
            "00" * 32,
            None,
            strategy_path=strategy_path,
        )

    assert not marker.exists()


def test_verified_strategy_loader_executes_the_exact_hashed_bytes(
    tmp_path: Path,
) -> None:
    module = _load_backtest_module()
    strategy_path = tmp_path / "strategy.py"
    source = "PARAMS = {'lookback': 7}\nclass Strategy:\n    pass\n"
    strategy_path.write_text(source, encoding="utf-8")
    expected_hash = hashlib.sha256(source.encode()).hexdigest()

    strategy_class = module.load_verified_strategy_class(
        expected_hash,
        module.canonical_sha256({"lookback": 7}),
        strategy_path=strategy_path,
    )

    assert strategy_class.__name__ == "Strategy"


def test_verified_strategy_loader_rejects_executable_params_drift(
    tmp_path: Path,
) -> None:
    module = _load_backtest_module()
    strategy_path = tmp_path / "strategy.py"
    source = (
        "PARAMS = {'lookback': 7}\nPARAMS['lookback'] = 8\nclass Strategy:\n    pass\n"
    )
    strategy_path.write_text(source, encoding="utf-8")

    with pytest.raises(module.StrategySourceMismatch, match="strategy_params_hash"):
        module.load_verified_strategy_class(
            hashlib.sha256(source.encode()).hexdigest(),
            module.canonical_sha256({"lookback": 7}),
            strategy_path=strategy_path,
        )


def test_cv_prints_canonical_trial_statistics_and_forwards_cost(
    monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_backtest_module()
    observed = []
    fold_results = [
        SimpleNamespace(
            sharpe=value,
            total_return_pct=1.0,
            max_drawdown_pct=1.0,
            num_trades=2,
        )
        for value in (1.0, 2.0, 3.0, 4.0)
    ]
    cv_result = SimpleNamespace(
        fold_scores=[1.0, 2.0, 3.0, 4.0],
        fold_results=fold_results,
        fold_indices=[0, 1, 2, 3],
        cv_score=1.0,
        mean_score=2.5,
        std_score=1.0,
        min_score=1.0,
    )

    def fake_cross_validate(strategy_class, *, bar_interval, execution_cost):
        observed.append(execution_cost)
        return cv_result

    monkeypatch.setattr(module.prepare, "cross_validate", fake_cross_validate)
    cost = module.prepare.ExecutionCost(
        fee_rate=0.0004,
        half_spread_bps=0.0,
        slippage_bps=2.0,
    )

    module._run_cv("1d", cost, object)

    output = capsys.readouterr().out
    assert "trial_sharpe:       2.500000" in output
    assert "trial_p_value:" in output
    assert "trial_sample_size:  4" in output
    assert observed == [cost]
