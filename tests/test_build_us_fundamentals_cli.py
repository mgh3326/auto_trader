"""ROB-441 PR2: US fundamentals builder CLI arg parsing (dry-run default)."""

from __future__ import annotations

import pytest

from scripts.build_us_fundamentals_snapshots import parse_args


@pytest.mark.unit
def test_defaults_to_dry_run() -> None:
    args = parse_args(["--symbol", "AAPL"])
    assert args.commit is False
    assert args.dry_run is True
    assert args.symbol == ["AAPL"]
    assert args.limit == 20  # default when not --all


@pytest.mark.unit
def test_commit_flag() -> None:
    args = parse_args(["--all", "--commit"])
    assert args.commit is True
    assert args.dry_run is False
    assert args.all is True


@pytest.mark.unit
def test_all_excludes_symbol_and_limit() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--all", "--symbol", "AAPL"])
    with pytest.raises(SystemExit):
        parse_args(["--all", "--limit", "50"])


@pytest.mark.unit
def test_concurrency_must_be_positive() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--symbol", "AAPL", "--concurrency", "0"])


@pytest.mark.unit
def test_with_quarterly_flag() -> None:
    # ROB-441 PR4: --with-quarterly opts into quarterly periods (annual-only default).
    assert parse_args(["--symbol", "AAPL"]).include_quarterly is False
    assert (
        parse_args(["--symbol", "AAPL", "--with-quarterly"]).include_quarterly is True
    )
