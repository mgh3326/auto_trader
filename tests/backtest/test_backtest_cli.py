"""Tests for backtest CLI routing."""

import importlib.util
import sys
from pathlib import Path


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
    captured: dict[str, str] = {}

    def fake_run_report(split: str, bar_interval: str, output: str) -> None:
        captured["split"] = split
        captured["bar_interval"] = bar_interval
        captured["output"] = output

    monkeypatch.setattr(module, "_run_report", fake_run_report)
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
        ],
    )

    module.main()

    assert captured == {
        "split": "test",
        "bar_interval": "4h",
        "output": "json",
    }
