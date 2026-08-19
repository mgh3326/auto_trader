from __future__ import annotations

import pytest

from app.services.manual_holdings_leftover import (
    LeftoverManualRow,
    cleanup_toss_leftover_manual_rows,
    detect_manual_broker_conflicts,
    leftover_reasons,
)

pytestmark = pytest.mark.unit


def test_leftover_reasons_are_deterministic() -> None:
    assert leftover_reasons(
        ticker="AMZN",
        is_mock=True,
        filled_sell_symbols={"AMZN"},
    ) == (
        "toss_us_allowlist",
        "filled_sell_in_toss_ledger",
        "account_mode_mock_mismatch",
    )
    assert (
        leftover_reasons(
            ticker="MSFT",
            is_mock=False,
            filled_sell_symbols=set(),
        )
        == ()
    )


def test_conflict_only_when_broker_fill_exists() -> None:
    rows = (
        LeftoverManualRow(
            holding_id=1,
            ticker="AMZN",
            quantity="1",
            broker_account_id=9,
            broker_type="toss",
            is_mock=False,
            reasons=("toss_us_allowlist", "filled_sell_in_toss_ledger"),
        ),
        LeftoverManualRow(
            holding_id=2,
            ticker="GOOGL",
            quantity="1",
            broker_account_id=9,
            broker_type="toss",
            is_mock=False,
            reasons=("toss_us_allowlist",),
        ),
    )
    conflicts = detect_manual_broker_conflicts(rows)
    assert [row.ticker for row in conflicts] == ["AMZN"]
    assert conflicts[0].reason == "manual_row_conflicts_with_broker_fill"


@pytest.mark.asyncio
async def test_commit_without_confirm_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _empty(_session: object) -> tuple[()]:
        return ()

    monkeypatch.setattr(
        "app.services.manual_holdings_leftover.list_toss_leftover_manual_rows",
        _empty,
    )
    with pytest.raises(ValueError, match="confirm"):
        await cleanup_toss_leftover_manual_rows(
            object(),  # type: ignore[arg-type]
            commit=True,
            confirm=False,
        )
