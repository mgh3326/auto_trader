from __future__ import annotations

import pytest

from scripts.build_financial_fundamentals_snapshots import parse_args


def test_defaults_to_dry_run():
    args = parse_args(["--symbol", "005930"])
    assert args.dry_run is True
    assert args.commit is False
    assert args.include_quarterly is False
    assert args.market == "kr"


def test_commit_flag_disables_dry_run():
    args = parse_args(["--all", "--commit"])
    assert args.dry_run is False
    assert args.commit is True


def test_all_is_mutually_exclusive_with_symbol():
    with pytest.raises(SystemExit):
        parse_args(["--all", "--symbol", "005930"])
