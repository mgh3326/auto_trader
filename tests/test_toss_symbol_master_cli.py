from scripts.sync_toss_symbol_master import parse_args


def test_parse_args_defaults_to_dry_run() -> None:
    args = parse_args(["--market", "kr", "--symbol", "005930"])
    assert args.market == "kr"
    assert args.symbol == ["005930"]
    assert args.commit is False


def test_parse_args_all_excludes_symbol_and_limit() -> None:
    try:
        parse_args(["--market", "kr", "--all", "--symbol", "005930"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("expected parser error")
