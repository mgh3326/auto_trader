"""hk 6036 acceptance 1: the recorded 11-25 day (hk 6036 comment 32758).

Candles are Upbit's public 15-minute bars for BTC, XRP and SAHARA, fetched
2026-11-25 18:40 KST. The market alert flags were read at that same moment and
stand in for each replayed run's observation (Upbit serves no flag history).
The day is replayed on the recorder's cadence: one run a minute after each bar
close, first write wins.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.services.upbit_intraday_shadow.recorder import run_once
from tests.services.upbit_intraday_shadow import support as S

pytestmark = pytest.mark.unit

MARKETS = ("KRW-BTC", "KRW-XRP", "KRW-SAHARA")
FIRST_RUN = S.kst("11-25 09:16")
OBSERVED = S.kst("11-25 10:25")  # when comment 32758 was looking
LAST_RUN = S.kst("11-25 18:31")
CAUTION = "caution:DEPOSIT_AMOUNT_SOARING,TRADING_VOLUME_SOARING"
HELD_BACK = ("market_event_caution",)

#: (market, configuration, signal bar open KST) -> (disposition, reasons)
EXPECTED = {
    # SAHARA: the crash bars are held back by the caution flags; the 09:00 spike to
    # 8.02 never becomes an A_breakout (the bar closed below the prior range).
    ("KRW-SAHARA", "C_downside:N16_X3", "11-25 09:00"): ("excluded", HELD_BACK),
    ("KRW-SAHARA", "C_downside:N16_X5", "11-25 09:00"): ("excluded", HELD_BACK),
    ("KRW-SAHARA", "C_downside:N32_X3", "11-25 09:15"): ("excluded", HELD_BACK),
    ("KRW-SAHARA", "C_downside:N32_X5", "11-25 09:15"): ("excluded", HELD_BACK),
    # XRP: the bounce fails and the low breaks at the 10:30 bar.
    ("KRW-XRP", "C_downside:N16_X3", "11-25 10:30"): ("signal", ()),
    ("KRW-XRP", "C_downside:N32_X3", "11-25 10:30"): ("signal", ()),
    ("KRW-XRP", "B_held_rebound:N16_X3", "11-25 17:30"): ("signal", ()),
    # BTC: no recovery signal at all; two downside breaks later in the day.
    **{
        ("KRW-BTC", f"C_downside:N{n}_X{x}", bar): ("signal", ())
        for n in (16, 32)
        for x in (3, 5)
        for bar in ("11-25 10:30", "11-25 16:30")
    },
}


def _replay(until: datetime) -> dict[tuple[str, str, str], dict[str, Any]]:
    candles = {m: S.recorded_rows(m) for m in MARKETS}
    events = {i["market"]: i["market_event"] for i in S.recorded_market_all()}
    rows: dict[tuple[str, str, str], dict[str, Any]] = {}

    async def run() -> None:
        now = FIRST_RUN
        while now <= until:
            report = await run_once(
                S.data_at(S.FakeUpbit(candles, events), now),
                run_id="replay",
                now=now,
                catchup_bars=1,
            )
            assert report["errors"] == []
            for row in report["rows"]:
                key = (
                    row["market"],
                    row["config_id"],
                    S.kst_label(row["signal_bar_open_at"]),
                )
                rows.setdefault(key, row)
            now += timedelta(minutes=15)

    asyncio.run(run())
    return rows


@pytest.fixture(scope="module")
def day() -> dict[tuple[str, str, str], dict[str, Any]]:
    return _replay(LAST_RUN)


def test_recorded_bars_are_the_ones_the_comment_describes() -> None:
    def bar(market: str, label: str):
        stamp = S.kst(label).strftime("%Y-%m-%dT%H:%M:%S")
        (found,) = [r for r in S.recorded_rows(market) if r.time_utc == stamp]
        return found

    assert Decimal(bar("KRW-BTC", "11-25 09:00").high) == Decimal("81465000")
    assert Decimal(bar("KRW-XRP", "11-25 09:00").high) == Decimal("1260")
    assert Decimal(bar("KRW-XRP", "11-25 10:00").low) == Decimal("1250")
    assert Decimal(bar("KRW-SAHARA", "11-25 09:00").high) == Decimal("8.02")
    assert Decimal(bar("KRW-SAHARA", "11-25 09:30").low) == Decimal("7.12")


def test_the_day_produces_exactly_these_rows(day) -> None:
    actual = {
        key: (row["disposition"], row["exclusion_reasons"]) for key, row in day.items()
    }
    assert actual == EXPECTED


def test_btc_has_not_recovered_when_the_comment_looked() -> None:
    # 10:25 KST: the 10:15 bar is still forming. BTC has no row of any family
    # (no recovery over the 09:00 high), XRP has not broken down yet, and only the
    # SAHARA crash bars are on record, held back.
    seen = _replay(OBSERVED)
    assert {key[0] for key in seen} == {"KRW-SAHARA"}
    assert all(row["disposition"] == "excluded" for row in seen.values())
    assert all(
        row["signal_bar_open_at"] <= S.kst("11-25 10:00") for row in seen.values()
    )


def test_sahara_row_carries_the_held_back_evidence(day) -> None:
    row = day[("KRW-SAHARA", "C_downside:N16_X3", "11-25 09:00")]
    assert row["signal_time"] == S.kst("11-25 09:15")
    assert row["first_received_at"] == S.kst("11-25 09:16")
    assert row["market_event_state"] == CAUTION
    assert row["market_event_caution_types"] == (
        "DEPOSIT_AMOUNT_SOARING",
        "TRADING_VOLUME_SOARING",
    )
    assert row["market_event_observed_at"] == S.kst("11-25 09:16")
    assert row["close_price"] == Decimal("7.60000000")
    assert row["ref_level"] == Decimal("7.69000000")
    assert row["signal_tick_krw"] == Decimal("0.01")
    assert row["fee_rate_per_side"] == Decimal("0.0005")
    assert row["liquidity_bucket"] == "L3"
    assert row["roundtrip_cost_rate"] == Decimal("0.002")


def test_xrp_breakdown_row(day) -> None:
    row = day[("KRW-XRP", "C_downside:N32_X3", "11-25 10:30")]
    assert row["direction"] == -1
    assert row["close_price"] == Decimal("1245.00000000")
    assert row["ref_level"] == Decimal("1248.00000000")
    assert row["market_event_state"] == "none"
    assert row["selected_config"] is False
    assert day[("KRW-BTC", "C_downside:N32_X5", "11-25 10:30")]["selected_config"]


def test_a_late_run_does_not_claim_the_alert_state_of_the_signal_time() -> None:
    candles = {m: S.recorded_rows(m) for m in MARKETS}
    events = {i["market"]: i["market_event"] for i in S.recorded_market_all()}
    report = asyncio.run(
        run_once(
            S.data_at(S.FakeUpbit(candles, events), LAST_RUN),
            run_id="late",
            now=LAST_RUN,
            catchup_bars=40,
        )
    )
    late = [r for r in report["rows"] if r["signal_time"] < S.kst("11-25 18:00")]
    assert len(late) == len(EXPECTED)
    assert {r["exclusion_reasons"] for r in late} == {("market_event_unknown",)}
    assert {r["market_event_state"] for r in late} == {"unknown"}
    # Their virtual round trips use the real next-bar open.
    first = [
        t
        for t in report["virtual_rows"]
        if t["market"] == "KRW-SAHARA"
        and t["config_id"] == "C_downside:N16_X3"
        and t["entry_bar_open_at"] == S.kst("11-25 09:15")
    ]
    assert [t["horizon_bars"] for t in first] == [4, 16]  # 24h not reached yet
    one_hour = first[0]
    assert one_hour["virtual_entry_price"] == Decimal("7.62000000")
    assert one_hour["virtual_exit_price"] == Decimal("7.23000000")
    assert one_hour["gross_return"] == Decimal("0.051181102362")
    assert one_hour["net_return"] == Decimal("0.049181102362")
    assert one_hour["mae"] == Decimal("0.010498687664")
    assert one_hour["mfe"] == Decimal("0.065616797900")
