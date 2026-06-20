from __future__ import annotations

from scripts.binance_demo_scalping_benchmark import _parse_args


def test_cli_parse_args() -> None:
    ns = _parse_args(["--product", "usdm_futures", "--date", "2026-06-20"])
    assert ns.product == "usdm_futures"
    assert ns.date == "2026-06-20"
    assert ns.session_tag == ""
