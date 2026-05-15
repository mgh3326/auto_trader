def test_cli_argument_parser_accepts_required_args():
    from scripts.backfill_daily_candles import _build_parser

    parser = _build_parser()
    ns = parser.parse_args(
        ["--market", "us", "--symbols", "AAPL,MSFT", "--horizon-bars", "500"]
    )
    assert ns.market == "us"
    assert ns.symbols == "AAPL,MSFT"
    assert ns.horizon_bars == 500
    assert ns.dry_run is False


def test_cli_dry_run_flag():
    from scripts.backfill_daily_candles import _build_parser

    parser = _build_parser()
    ns = parser.parse_args(["--market", "kr", "--symbols", "005930", "--dry-run"])
    assert ns.dry_run is True
