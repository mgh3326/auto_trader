"""ROB-429 follow-up: the build CLI must exit non-zero when a requested --commit
is blocked by the coverage guard, so operator automation / CI detects the block
(it is NOT a successful no-op). dry-run and successful/overridden commits exit 0.
"""

from __future__ import annotations

import argparse

import pytest

from scripts import build_invest_kr_fundamentals_snapshots as cli


def _args(**overrides: object) -> argparse.Namespace:
    base = {"limit": 200, "all": False, "commit": False, "allow_partial": False}
    base.update(overrides)
    return argparse.Namespace(**base)


def _result(**overrides: object) -> dict[str, object]:
    base = {
        "snapshot_date": "2026-06-04",
        "fetched": 0,
        "would_upsert": 0,
        "upserted": 0,
        "committed": False,
        "active_universe_count": 3909,
        "coverage_ratio": 0.0,
        "commit_allowed": False,
        "block_reason": None,
        "samples": [],
    }
    base.update(overrides)
    return base


def _patch(monkeypatch: pytest.MonkeyPatch, result: dict[str, object]) -> None:
    async def _fake_run(_request: object) -> dict[str, object]:
        return result

    monkeypatch.setattr(cli, "run_kr_fundamentals_snapshot_build", _fake_run)


@pytest.mark.asyncio
async def test_dry_run_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, _result(committed=False, would_upsert=10))
    assert await cli.run(_args(commit=False)) == 0


@pytest.mark.asyncio
async def test_commit_blocked_exits_two(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(
        monkeypatch,
        _result(
            committed=False,
            would_upsert=200,
            coverage_ratio=0.0511,
            commit_allowed=False,
            block_reason="kr commit blocked: built 200 < floor 3128",
        ),
    )
    assert await cli.run(_args(commit=True)) == 2


@pytest.mark.asyncio
async def test_commit_success_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(
        monkeypatch,
        _result(
            committed=True,
            would_upsert=3800,
            upserted=3800,
            coverage_ratio=0.9721,
            commit_allowed=True,
        ),
    )
    assert await cli.run(_args(commit=True)) == 0


@pytest.mark.asyncio
async def test_commit_allow_partial_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # --allow-partial bypasses the guard → committed True → exit 0 even when thin.
    _patch(
        monkeypatch,
        _result(
            committed=True,
            would_upsert=200,
            upserted=200,
            coverage_ratio=0.0511,
            commit_allowed=True,
        ),
    )
    assert await cli.run(_args(commit=True, allow_partial=True)) == 0
