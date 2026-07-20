"""ROB-976 verify R1: bounded, read-only CLI preview for support_proximity."""

from __future__ import annotations

import datetime as dt

import pytest

from scripts import build_support_proximity_snapshot as cli


class _FakeCM:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *exc: object) -> bool:
        return False


def test_default_market_is_kr():
    args = cli.parse_args([])
    assert args.market == "kr"
    assert args.limit == 30


def test_no_commit_flag_exists():
    """support_proximity has no persisted artifact — a --commit flag would be
    dishonest (nothing to commit). Confirm argparse rejects it."""
    with pytest.raises(SystemExit):
        cli.parse_args(["--market", "kr", "--commit"])


@pytest.mark.asyncio
async def test_run_returns_1_when_no_partition(monkeypatch):
    import app.services.invest_view_model.support_proximity_screener as loader_module

    async def _fake_loader(*args, **kwargs):
        return None

    monkeypatch.setattr(
        loader_module, "load_support_proximity_from_snapshots", _fake_loader
    )
    monkeypatch.setattr(cli, "AsyncSessionLocal", lambda: _FakeCM())

    args = cli.parse_args(["--market", "kr"])
    exit_code = await cli.run(args)
    assert exit_code == 1


@pytest.mark.asyncio
async def test_run_prints_rows_and_returns_0(monkeypatch, capsys):
    import app.services.invest_view_model.support_proximity_screener as loader_module
    from app.services.invest_view_model.screener_service import _SnapshotLoadResult

    async def _fake_loader(*args, **kwargs):
        return _SnapshotLoadResult(
            rows=[
                {
                    "symbol": "005930",
                    "name": "삼성전자",
                    "close": 80000.0,
                    "support_price": 78500.0,
                    "support_kind": "bb_lower",
                    "support_strength": "strong",
                    "dist_to_support_pct": 1.88,
                    "market_cap": 4.0e14,
                }
            ],
            partition_date=dt.date(2026, 7, 20),
            partition_computed_at=None,
            degradation_reason=None,
            coverage_label=None,
        )

    monkeypatch.setattr(
        loader_module, "load_support_proximity_from_snapshots", _fake_loader
    )
    monkeypatch.setattr(cli, "AsyncSessionLocal", lambda: _FakeCM())

    args = cli.parse_args(["--market", "kr"])
    exit_code = await cli.run(args)
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "005930" in out
    assert "dry-run only, no rows written" in out
