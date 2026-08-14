"""Offline US-cycle safety tests: lab reads, RTH/table gates, and no mutation."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from app.mcp_server.tooling.market_session import US_SESSION_REGULAR
from scripts.b0x.labels import SHARED_ACCOUNT_HISTORY, TRUST_LABELS
from scripts.b0x.us import alpaca
from scripts.b0x.us import cycle as us_cycle
from scripts.policy_table.core.schema import compute_policy_table_hash
from tests.scripts.b0x._table_fixtures import make_payload, make_row, write_table

pytestmark = pytest.mark.unit

# Monday 11:00 EDT.  The cycle's session function is monkeypatched anyway so
# these tests remain completely calendar/network independent.
NOW = dt.datetime(2026, 8, 10, 15, 0, tzinfo=dt.UTC)


def _table_dir(
    tmp_path: Path,
    *,
    age: dt.timedelta = dt.timedelta(hours=1),
    selected_usd: str | None = "300",
) -> Path:
    payload = make_payload(
        market="us",
        rows=[
            make_row(
                symbol="AAPL",
                previous_close="100",
                buy_l1="97",
                sell_r1="105",
                sell_r2="110",
            )
        ],
        generated_at=NOW - age,
    )
    payload["config"] = {
        **payload["config"],
        "quote_currency": "USD",
        "new_entry_notional_usd_min": "150",
        "new_entry_notional_usd_max": "450",
    }
    if selected_usd is not None:
        payload["config"]["new_entry_notional_usd"] = selected_usd
    # The US builder stamps its selected amount in config, not the legacy KRW
    # sizing field.  This fixture must exercise that real schema shape.
    payload["sizing"] = {}
    payload["stamps"]["policy_table_hash"] = compute_policy_table_hash(
        {key: value for key, value in payload.items() if key != "stamps"}
    )
    directory = tmp_path / "policy-tables"
    write_table(directory, payload, market="us")
    return directory


def _readers(
    *,
    portfolio_value: str = "5000",
    positions: list[dict[str, Any]] | None = None,
    orders: list[dict[str, Any]] | None = None,
    ledger: list[dict[str, Any]] | None = None,
) -> tuple[alpaca.LabReaders, list[str]]:
    calls: list[str] = []

    async def account(**kwargs: Any) -> dict[str, Any]:
        calls.append("account")
        assert kwargs == {"account_mode": alpaca.LANE}
        return {
            "success": True,
            "account_mode": alpaca.LANE,
            "account": {"cash": "5000", "portfolio_value": portfolio_value},
        }

    async def list_positions(**kwargs: Any) -> dict[str, Any]:
        calls.append("positions")
        assert kwargs == {"account_mode": alpaca.LANE}
        values = positions or []
        return {
            "success": True,
            "account_mode": alpaca.LANE,
            "count": len(values),
            "positions": values,
        }

    async def list_orders(**kwargs: Any) -> dict[str, Any]:
        calls.append("orders")
        assert kwargs == {"status": "open", "limit": 500, "account_mode": alpaca.LANE}
        values = orders or []
        return {
            "success": True,
            "account_mode": alpaca.LANE,
            "count": len(values),
            "orders": values,
        }

    async def list_ledger(**kwargs: Any) -> dict[str, Any]:
        calls.append("ledger")
        assert kwargs == {"limit": 200, "account_mode": alpaca.LANE}
        values = ledger or []
        return {
            "success": True,
            "account_mode": alpaca.LANE,
            "count": len(values),
            "items": values,
        }

    return (
        alpaca.LabReaders(
            get_account=account,
            list_positions=list_positions,
            list_orders=list_orders,
            list_recent_ledger=list_ledger,
        ),
        calls,
    )


@pytest.mark.asyncio
async def test_outside_rth_stops_before_table_or_lab_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    readers, calls = _readers()
    monkeypatch.setattr(us_cycle, "us_market_session", lambda now: "closed")

    outcome = await us_cycle.run_us_cycle(
        now=NOW,
        table_dir=_table_dir(tmp_path),
        out_dir=tmp_path / "observations",
        readers=readers,
    )

    assert outcome.zero_order_reason == us_cycle.OUTSIDE_RTH_REASON
    assert calls == []
    assert "policy_table_hash" not in outcome.record


@pytest.mark.asyncio
async def test_lab_dry_run_plans_without_preview_submit_or_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    readers, read_calls = _readers()
    submit_calls: list[dict[str, Any]] = []
    cancel_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(us_cycle, "us_market_session", lambda now: US_SESSION_REGULAR)

    async def fake_submit(*args: Any, **kwargs: Any) -> dict[str, Any]:
        submit_calls.append(kwargs)
        return {"submitted": True}

    async def fake_cancel(*args: Any, **kwargs: Any) -> dict[str, Any]:
        cancel_calls.append(kwargs)
        return {"cancelled": True}

    outcome = await us_cycle.run_us_cycle(
        now=NOW,
        table_dir=_table_dir(tmp_path),
        out_dir=tmp_path / "observations",
        confirm=False,
        readers=readers,
        submitter=fake_submit,
        canceler=fake_cancel,
    )

    assert read_calls == ["account", "positions", "orders", "ledger"]
    assert outcome.zero_order_reason is None
    assert outcome.record["contract"]["version"] == "v1.7"
    assert outcome.record["submitted"] == []
    assert outcome.record["submission_skipped"].startswith("confirm=False")
    assert submit_calls == []
    assert cancel_calls == []
    assert outcome.record["broker_truth"]["own_pending_readable"] is True
    assert outcome.record["broker_truth"]["own_pending"] == []
    assert outcome.record["planned"]
    assert SHARED_ACCOUNT_HISTORY not in outcome.record["labels"]
    for label in TRUST_LABELS:
        assert label in outcome.record["labels"]
    assert any(
        "CROSS_MARKET_TRANSFER_UNVALIDATED" in label
        for label in outcome.record["labels"]
    )


@pytest.mark.asyncio
async def test_confirm_without_approved_lab_mutation_seam_still_makes_no_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    readers, read_calls = _readers()
    monkeypatch.setattr(us_cycle, "us_market_session", lambda now: US_SESSION_REGULAR)

    outcome = await us_cycle.run_us_cycle(
        now=NOW,
        table_dir=_table_dir(tmp_path),
        out_dir=tmp_path / "observations",
        confirm=True,
        readers=readers,
    )

    assert read_calls == ["account", "positions", "orders", "ledger"]
    assert outcome.record["submitted"] == []
    assert (
        "no approved injected alpaca_paper_lab submitter"
        in outcome.record["submission_skipped"]
    )


@pytest.mark.asyncio
async def test_missing_nav_for_ratio_kill_is_zero_order_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    readers, _ = _readers(portfolio_value="0")
    monkeypatch.setattr(us_cycle, "us_market_session", lambda now: US_SESSION_REGULAR)

    outcome = await us_cycle.run_us_cycle(
        now=NOW,
        table_dir=_table_dir(tmp_path),
        out_dir=tmp_path / "observations",
        readers=readers,
    )

    assert outcome.zero_order_reason == us_cycle.MISSING_NAV_FOR_RATIO_KILL_REASON
    assert "NAV-ratio kill fails closed" in outcome.record["zero_order_detail"]
    assert outcome.record["fresh_truth"]["nav_present"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("selected_usd", [None, "149", "451"])
async def test_invalid_us_table_selection_stops_before_lab_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    selected_usd: str | None,
) -> None:
    readers, calls = _readers()
    monkeypatch.setattr(us_cycle, "us_market_session", lambda now: US_SESSION_REGULAR)

    outcome = await us_cycle.run_us_cycle(
        now=NOW,
        table_dir=_table_dir(tmp_path, selected_usd=selected_usd),
        out_dir=tmp_path / "observations",
        readers=readers,
    )

    assert outcome.zero_order_reason == us_cycle.INVALID_US_TABLE_SIZING_REASON
    assert calls == []


@pytest.mark.asyncio
async def test_foreign_residual_blocks_confirmed_submit_but_is_not_relabelled_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    readers, _ = _readers(
        positions=[
            {
                "symbol": "UBER",
                "qty": "1",
                "qty_available": "1",
                "avg_entry_price": "20",
            }
        ]
    )
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(us_cycle, "us_market_session", lambda now: US_SESSION_REGULAR)

    async def fake_submit(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"submitted": True}

    outcome = await us_cycle.run_us_cycle(
        now=NOW,
        table_dir=_table_dir(tmp_path),
        out_dir=tmp_path / "observations",
        confirm=True,
        readers=readers,
        submitter=fake_submit,
    )

    assert outcome.zero_order_reason is None
    assert outcome.record["contaminated"] is True
    assert outcome.record["fresh_truth"]["foreign_position_symbols"] == ["UBER"]
    assert outcome.record["fresh_truth"]["own_position_symbols"] == []
    assert "contaminated lab account state" in outcome.record["submission_skipped"]
    assert calls == []
