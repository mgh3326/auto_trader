"""Tests for scripts/sync_us_common_stock_flags.py CLI argument parsing (ROB-204)."""
from scripts.sync_us_common_stock_flags import parse_args


def test_default_is_dry_run() -> None:
    args = parse_args([])
    assert args.commit is False
    assert args.dry_run is True


def test_commit_flag() -> None:
    args = parse_args(["--commit"])
    assert args.commit is True
    assert args.dry_run is False
