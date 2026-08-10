"""ROB-1236 — policy-table adapters must not emit rows for inert series.

The B0-X observation tables are built from these payloads, so a symbol under a
trading halt that still gets a row contaminates everything derived from it.
Halt-suspect symbols are dropped from ``rows`` and surfaced under
``universe.halted_suspect`` instead — excluded, but never silently.

Pure-compute path only: every test builds ``RawInputs`` directly, no DB and no
network.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from scripts.policy_table.adapters import crypto as crypto_adapter
from scripts.policy_table.adapters import kr as kr_adapter
from scripts.policy_table.adapters import us as us_adapter

pytestmark = pytest.mark.unit

FROZEN_PRICE = Decimal("83800")


def _bars(
    n: int = 150,
    *,
    start: Decimal = Decimal("80000"),
    step: Decimal = Decimal("100"),
    frozen_sessions: int = 0,
    frozen_price: Decimal = FROZEN_PRICE,
) -> list[list[str]]:
    """Ascending ``[close, high, low, volume]`` bars, optionally frozen at the end."""
    bars: list[list[str]] = []
    price = start
    for i in range(n):
        price = price + (step if i % 5 else -step * 2 // 3)
        bars.append(
            [
                str(price),
                str(price + step * 4),
                str(price - step * 3),
                "120000",
            ]
        )
    if frozen_sessions:
        # Final live bar closes exactly where the halt freezes.
        bars[-1] = [
            str(frozen_price),
            str(frozen_price + step * 4),
            str(frozen_price - step * 3),
            "310000",
        ]
        bars.extend(
            [str(frozen_price), str(frozen_price), str(frozen_price), "0"]
            for _ in range(frozen_sessions)
        )
    return bars


# ---------------------------------------------------------------------------
# KR
# ---------------------------------------------------------------------------


def _kr_raw(candles: dict[str, list[list[str]]]) -> kr_adapter.RawInputs:
    symbols = sorted(candles)
    return kr_adapter.RawInputs(
        as_of="2026-08-10T09:00:00+00:00",
        holdings=[],
        watch_alerts=[],
        universe_pool=[
            {
                "symbol": symbol,
                "latest_close": candles[symbol][-1][0],
                "market_cap": "5000000000000",
                "market_cap_source": "market_valuation_snapshots:naver_finance",
                "daily_turnover": "50000000000",
                "daily_volume": "500000",
                "snapshot_date": "2026-08-09",
            }
            for symbol in symbols
        ],
        snapshot_partition_date="2026-08-09",
        snapshot_breadth=None,
        universe_symbols=symbols,
        candles=candles,
    )


def test_kr_halted_symbol_gets_no_row():
    raw = _kr_raw(
        {
            "000880": _bars(frozen_sessions=8),
            "005930": _bars(),
        }
    )

    payload = kr_adapter.compute_policy_table(raw)

    assert [row["symbol"] for row in payload["rows"]] == ["005930"]
    assert "000880" not in {row["symbol"] for row in payload["rows"]}


def test_kr_halted_symbol_is_recorded_not_silently_dropped():
    raw = _kr_raw({"000880": _bars(frozen_sessions=8), "005930": _bars()})

    payload = kr_adapter.compute_policy_table(raw)

    halted = payload["universe"]["halted_suspect"]
    assert [entry["symbol"] for entry in halted] == ["000880"]
    assert halted[0]["frozen_sessions"] == 8
    assert halted[0]["krx_halt_master"] == "unavailable"
    assert "not a confirmed trading halt" in halted[0]["note"]

    skipped = {
        entry["symbol"]: entry["reason"] for entry in payload["universe"]["skipped"]
    }
    assert skipped["000880"] == "halted_suspect"
    assert payload["universe"]["computed_symbols"] == 1


def test_kr_halted_symbol_named_in_the_rendered_summary():
    raw = _kr_raw({"000880": _bars(frozen_sessions=8), "005930": _bars()})

    summary = kr_adapter.render_summary_md(
        {
            **kr_adapter.compute_policy_table(raw),
            "stamps": {"policy_table_hash": "x", "auto_trader_head": "y"},
        }
    )

    assert "000880" in summary
    assert "NOT a confirmed halt" in summary


def test_kr_actively_traded_symbols_all_keep_their_rows():
    raw = _kr_raw({"005930": _bars(), "000660": _bars(start=Decimal("190000"))})

    payload = kr_adapter.compute_policy_table(raw)

    assert sorted(row["symbol"] for row in payload["rows"]) == ["000660", "005930"]
    assert payload["universe"]["halted_suspect"] == []


def test_kr_two_frozen_sessions_stay_below_the_threshold():
    raw = _kr_raw({"000880": _bars(frozen_sessions=2)})

    payload = kr_adapter.compute_policy_table(raw)

    assert [row["symbol"] for row in payload["rows"]] == ["000880"]
    assert payload["universe"]["halted_suspect"] == []


# ---------------------------------------------------------------------------
# US
# ---------------------------------------------------------------------------


def _us_raw(candles: dict[str, list[list[str]]]) -> us_adapter.RawInputs:
    symbols = sorted(candles)
    return us_adapter.RawInputs(
        as_of="2026-08-10T09:00:00+00:00",
        holdings=[],
        watch_alerts=[],
        universe_pool=[
            {
                "symbol": symbol,
                "latest_close": candles[symbol][-1][0],
                "market_cap": "3000000000000",
                "market_cap_source": "market_valuation_snapshots:yahoo",
                "daily_turnover": "5000000000",
                "daily_volume": "50000000",
                "snapshot_date": "2026-08-09",
                "exchange": "NASD",
                "is_common_stock": True,
            }
            for symbol in symbols
        ],
        snapshot_partition_date="2026-08-09",
        snapshot_breadth=None,
        universe_symbols=symbols,
        candles=candles,
        market_cap_fill_stats={"note": "unit-test fixture"},
    )


def test_us_halted_symbol_gets_no_row_and_is_recorded():
    raw = _us_raw(
        {
            "AAPL": _bars(start=Decimal("200"), step=Decimal("1")),
            "HALT": _bars(
                start=Decimal("50"),
                step=Decimal("1"),
                frozen_sessions=5,
                frozen_price=Decimal("47"),
            ),
        }
    )

    payload = us_adapter.compute_policy_table(raw)

    assert [row["symbol"] for row in payload["rows"]] == ["AAPL"]
    halted = payload["universe"]["halted_suspect"]
    assert [entry["symbol"] for entry in halted] == ["HALT"]
    assert halted[0]["frozen_sessions"] == 5


def test_us_actively_traded_symbol_keeps_its_row():
    raw = _us_raw({"AAPL": _bars(start=Decimal("200"), step=Decimal("1"))})

    payload = us_adapter.compute_policy_table(raw)

    assert [row["symbol"] for row in payload["rows"]] == ["AAPL"]
    assert payload["universe"]["halted_suspect"] == []


# ---------------------------------------------------------------------------
# Crypto
# ---------------------------------------------------------------------------


def _crypto_raw(candles: dict[str, list[list[str]]]) -> crypto_adapter.RawInputs:
    return crypto_adapter.RawInputs(
        as_of="2026-08-10T09:00:00+00:00",
        holdings=[],
        watch_alerts=[],
        top_traded=[
            {
                "market": symbol,
                "trade_price": candles[symbol][-1][0],
                "acc_trade_price_24h": "1000000000",
                "signed_change_rate": "0.01",
            }
            for symbol in sorted(candles)
        ],
        orderable_krw="1000000",
        candles=candles,
    )


def test_crypto_dead_feed_symbol_gets_no_row_and_is_recorded():
    raw = _crypto_raw(
        {
            "KRW-BTC": _bars(start=Decimal("100000000"), step=Decimal("100000")),
            "KRW-DEAD": _bars(
                start=Decimal("1000"),
                step=Decimal("10"),
                frozen_sessions=4,
                frozen_price=Decimal("980"),
            ),
        }
    )

    payload = crypto_adapter.compute_policy_table(raw)

    assert [row["symbol"] for row in payload["rows"]] == ["KRW-BTC"]
    halted = payload["universe"]["halted_suspect"]
    assert [entry["symbol"] for entry in halted] == ["KRW-DEAD"]
    assert halted[0]["frozen_sessions"] == 4


def test_crypto_actively_traded_symbols_all_keep_their_rows():
    raw = _crypto_raw(
        {
            "KRW-BTC": _bars(start=Decimal("100000000"), step=Decimal("100000")),
            "KRW-ETH": _bars(start=Decimal("5000000"), step=Decimal("10000")),
        }
    )

    payload = crypto_adapter.compute_policy_table(raw)

    assert sorted(row["symbol"] for row in payload["rows"]) == ["KRW-BTC", "KRW-ETH"]
    assert payload["universe"]["halted_suspect"] == []


# ---------------------------------------------------------------------------
# Replay compatibility
# ---------------------------------------------------------------------------


def test_legacy_three_element_replay_dumps_still_classify():
    """Pre-ROB-1236 raw dumps carry no volume — the frozen OHLC still shows."""
    legacy = {"000880": [bar[:3] for bar in _bars(frozen_sessions=8)]}

    payload = kr_adapter.compute_policy_table(_kr_raw(legacy))

    assert payload["rows"] == []
    assert payload["universe"]["halted_suspect"][0]["reasons"] == ["zero_variation"]
