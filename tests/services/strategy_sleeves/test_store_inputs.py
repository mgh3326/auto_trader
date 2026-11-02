"""ROB-1470 store input validation and mark selection (no database)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services.strategy_sleeves import marks, store
from app.services.strategy_sleeves.vocabulary import SYSTEM_LOSS_GATE_ACTOR
from tests.services.strategy_sleeves.support import D

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("value", ["1e30", "1e16", "9" * 17, "-0.00001", "1.00001"])
def test_amounts_out_of_column_range_are_sleeve_errors(value) -> None:
    with pytest.raises(store.SleeveError):
        store._amount("budget_cap", value, positive=False)


def test_an_in_range_amount_passes() -> None:
    assert store._amount("budget_cap", "9999999999999999.9999", positive=False) == D(
        "9999999999999999.9999"
    )


@pytest.mark.asyncio
async def test_create_refuses_the_reserved_system_actor_before_any_write() -> None:
    with pytest.raises(store.SleeveError, match="reserved"):
        await store.create_sleeve(
            None,  # never reached: refused before the first query
            sleeve_key="k",
            rule_id="rob1458.kr_cut_minus20",
            account_mode="kis_live",
            market="equity_kr",
            state="shadow",
            budget_cap="1",
            loss_limit="1",
            reason="r",
            actor=SYSTEM_LOSS_GATE_ACTOR,
        )


@pytest.mark.asyncio
async def test_the_mark_is_the_newest_completed_close_whatever_the_row_order(
    monkeypatch,
) -> None:
    from app.services.daily_candles import repository

    now = datetime(2026, 11, 2, 6, 0, tzinfo=UTC)
    yesterday = datetime(2026, 11, 1, tzinfo=UTC)

    def bar(day: datetime, close: str):
        return SimpleNamespace(time_utc=day, close=float(close))

    class _Repo:
        def __init__(self, session) -> None:
            pass

        async def fetch_recent_batch(self, **_kw):
            # newest first: the order must not matter
            return {
                "KRW-BTC": [
                    bar(yesterday, "120"),
                    bar(yesterday - timedelta(days=1), "110"),
                ]
            }

    monkeypatch.setattr(repository, "DailyCandlesRepository", _Repo)
    result = await marks.latest_close_marks(None, "crypto", ["KRW-BTC"], now)
    assert result == {"KRW-BTC": D("120")}


@pytest.mark.asyncio
async def test_marks_are_keyed_by_the_requested_symbols(monkeypatch) -> None:
    """The repository normalizes keys and may add some; marks follow the caller."""
    from app.services.daily_candles import repository

    now = datetime(2026, 11, 2, 6, 0, tzinfo=UTC)
    yesterday = datetime(2026, 11, 1, tzinfo=UTC)

    class _Repo:
        def __init__(self, session) -> None:
            pass

        async def fetch_recent_batch(self, **_kw):
            return {
                "KRW-BTC": [SimpleNamespace(time_utc=yesterday, close=120.0)],
                "KRW-ETH": [SimpleNamespace(time_utc=yesterday, close=5.0)],
            }

    monkeypatch.setattr(repository, "DailyCandlesRepository", _Repo)
    result = await marks.latest_close_marks(None, "crypto", ["krw-btc "], now)
    assert result == {"krw-btc ": D("120")}
