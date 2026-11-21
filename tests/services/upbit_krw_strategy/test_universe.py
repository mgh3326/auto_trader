from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.upbit_krw_strategy import universe as u
from app.services.upbit_krw_strategy.hashing import decimal_text
from app.services.upbit_krw_strategy.types import DailyBar

from .support import ASOF, LAST, bars, market

GOLDEN = json.loads((Path(__file__).parent / "golden_candidate_set.json").read_text())


def judge(item):
    return u.judge_market(item, asof=ASOF)


def test_last_closed_day_is_the_day_before_the_issue_date() -> None:
    assert u.last_closed_day(ASOF) == LAST


def test_u1_krw_active_market_passes_all_rules() -> None:
    e = judge(market())
    assert (e.eligible, e.reason_code) == (True, "ok")
    assert e.turnover30_median_krw == Decimal("10000000000")
    assert e.turnover30_sum_krw == Decimal("300000000000")
    assert e.last_close_krw == Decimal("1000")


@pytest.mark.parametrize(
    ("name", "kwargs", "reason"),
    [
        ("BTC-ETH", {}, "not_krw_market"),
        ("KRW-", {}, "not_krw_market"),
        ("KRW-ETH", {"is_active": False}, "inactive"),
        ("KRW-USDT", {}, "excluded_stable_list"),  # F10: not in the USD list
        ("KRW-USDC", {}, "excluded_stable_list"),
        ("KRW-WBTC", {}, "excluded_wrapped_duplicate"),
        ("KRW-BTCUP", {}, "excluded_leveraged_token"),
        ("KRW-ETH", {"warning": "CAUTION"}, "market_warning_caution"),
        ("KRW-ETH", {"warning": " caution "}, "market_warning_caution"),
        ("KRW-ETH", {"warning": ""}, "market_warning_unknown"),
        ("KRW-ETH", {"warning": "   "}, "market_warning_unknown"),
        ("KRW-ETH", {"warning": "UNKNOWN"}, "market_warning_unknown"),
        ("KRW-ETH", {"warning": "WARNING"}, "market_warning_unknown"),
        ("KRW-ETH", {"warning": None}, "market_warning_unknown"),
    ],
)
def test_first_failing_rule_wins(name, kwargs, reason) -> None:
    assert judge(market(name, **kwargs)).reason_code == reason


def test_only_none_warning_is_eligible_and_case_is_normalised() -> None:
    assert judge(market(warning="NONE")).eligible
    assert judge(market(warning=" none ")).eligible
    for bad in ("", "UNKNOWN", "null", "CAUTIONS", None, 0):
        e = judge(market(warning=bad))  # type: ignore[arg-type]
        assert (e.eligible, e.reason_code) == (False, "market_warning_unknown")


def test_paxg_is_kept_like_the_research_function() -> None:
    assert u.exclusion_reason("PAXG") is None
    assert judge(market("KRW-PAXG")).eligible


def test_peg_rule_is_not_applied_to_krw_closes() -> None:
    # ~1,000 KRW closes for 90 days would "prove a peg" only if the USD 0.98-1.02
    # rule were applied; it must not be.
    assert u.exclusion_reason("XYZ") is None
    assert judge(
        market("KRW-XYZ", bar_set=bars(count=90, close="1.0"))
    ).reason_code == ("price_below_min")


def test_u3_needs_90_closed_bars_and_ignores_open_candles() -> None:
    assert judge(market(bar_set=bars(count=89))).reason_code == "insufficient_history"
    assert judge(market(bar_set=bars(count=90))).eligible
    open_only = bars(count=90, is_closed=False)
    assert judge(market(bar_set=open_only)).reason_code == "insufficient_history"


def test_future_bars_are_ignored() -> None:
    future = DailyBar(ASOF, Decimal("5"), Decimal("1"), True)
    item = market(bar_set=bars(count=90) + (future,))
    e = judge(item)
    assert e.eligible and e.last_close_krw == Decimal("1000")


def test_duplicate_closed_bar_is_a_data_defect() -> None:
    dup = bars(count=90) + (DailyBar(LAST, Decimal("1"), Decimal("1"), True),)
    with pytest.raises(ValueError, match="duplicate"):
        judge(market(bar_set=dup))


def test_u4_last_close_floor_is_inclusive_100() -> None:
    assert judge(market(bar_set=bars(count=90, close="100"))).eligible
    below = judge(market(bar_set=bars(count=90, close="99.9")))
    assert below.reason_code == "price_below_min"


def test_u4_missing_newest_bar() -> None:
    older = bars(count=91, last=LAST - timedelta(days=1))
    assert judge(market(bar_set=older)).reason_code == "last_close_missing"


def test_u5_null_quote_volume_is_liquidity_unknown_not_zero() -> None:
    rows = list(bars(count=90))
    rows[5] = DailyBar(rows[5].day, rows[5].close, None, True)
    assert judge(market(bar_set=tuple(rows))).reason_code == "liquidity_unknown"


def test_u5_gap_inside_the_30_day_window_is_liquidity_unknown() -> None:
    rows = [b for b in bars(count=91) if b.day != LAST - timedelta(days=10)]
    assert judge(market(bar_set=tuple(rows))).reason_code == "liquidity_unknown"


def test_u5_median_not_mean_and_floor_inclusive() -> None:
    def with_volumes(volumes):
        rows = list(bars(count=90, quote_volume="0"))
        for k, vol in enumerate(volumes):
            rows[k] = DailyBar(rows[k].day, rows[k].close, Decimal(vol), True)
        return tuple(rows)

    # 30 values: 15 x 5e9 and 15 x 1e12 -> median = (5e9 + 1e12)/2 >= 5e9 (mean huge)
    high = judge(
        market(bar_set=with_volumes(["1000000000000"] * 15 + ["5000000000"] * 15))
    )
    assert high.eligible
    # one outlier cannot lift a low median: 29 x 4.9e9 + one 1e15
    low = judge(
        market(bar_set=with_volumes(["4900000000"] * 29 + ["1000000000000000"]))
    )
    assert low.reason_code == "liquidity_below_min"
    assert low.turnover30_median_krw == Decimal("4900000000")
    exact = judge(market(bar_set=with_volumes(["5000000000"] * 30)))
    assert exact.eligible


def test_u0_takes_top_20_by_sum_with_market_code_ties() -> None:
    items = []
    for i in range(25):
        qv = "10000000000" if i < 3 else str(11_000_000_000 + i)
        items.append(
            judge(market(f"KRW-C{i:02d}", bar_set=bars(count=90, quote_volume=qv)))
        )
    u0 = u.select_u0(items)
    assert len(u0) == 20
    # C03..C24 have strictly larger turnover than C00..C02, so those three drop out
    assert {"KRW-C00", "KRW-C01", "KRW-C02"}.isdisjoint(u0)
    assert u0[0] == "KRW-C24"
    tied = [judge(market(m)) for m in ("KRW-B", "KRW-A")]
    assert u.select_u0(tied) == ("KRW-A", "KRW-B")


def test_u0_skips_ineligible_markets() -> None:
    items = [judge(market("KRW-ETH")), judge(market("KRW-USDT"))]
    assert u.select_u0(items) == ("KRW-ETH",)


def test_u0_hash_is_membership_only() -> None:
    assert u.u0_sha256(["KRW-B", "KRW-A"]) == u.u0_sha256(["KRW-A", "KRW-B", "KRW-A"])
    assert u.u0_sha256(["KRW-A"]) != u.u0_sha256(["KRW-A", "KRW-B"])
    expected = hashlib.sha256(b'["KRW-A","KRW-B"]').hexdigest()
    assert u.u0_sha256(["KRW-B", "KRW-A"]) == expected


def test_daily_eligibility_is_u0_only_and_new_markets_never_enter() -> None:
    inputs = {m.market: m for m in (market("KRW-ETH"), market("KRW-NEW"))}
    result = u.daily_eligibility(["KRW-ETH", "KRW-GONE"], inputs, asof=ASOF)
    assert set(result) == {"KRW-ETH", "KRW-GONE"}
    assert result["KRW-GONE"].reason_code == "no_market_data"
    assert not result["KRW-GONE"].eligible


def test_decimal_text_is_plain_and_exponent_free() -> None:
    assert decimal_text(Decimal("5E+9")) == "5000000000"
    assert decimal_text(Decimal("1000.50")) == "1000.5"
    assert decimal_text(Decimal("0.000")) == "0"
    assert decimal_text(None) is None


def test_candidate_set_golden() -> None:
    asof = ASOF
    u0 = GOLDEN["u0"]
    inputs = {
        "KRW-BTC": market(
            "KRW-BTC",
            bar_set=bars(count=90, close="150000000", quote_volume="8000000000"),
        ),
        "KRW-ETH": market(
            "KRW-ETH",
            bar_set=bars(count=90, close="5000000", quote_volume="4000000000"),
        ),
        "KRW-XRP": market("KRW-XRP", warning="CAUTION"),
    }
    result = u.daily_eligibility(u0, inputs, asof=asof)
    assert u.u0_sha256(u0) == GOLDEN["u0_sha256"]
    assert (
        u.candidate_set_payload(result, asof=asof, u0_hash=GOLDEN["u0_sha256"])
        == GOLDEN["payload"]
    )
    assert (
        u.candidate_set_sha256(result, asof=asof, u0_hash=GOLDEN["u0_sha256"])
        == GOLDEN["sha256"]
    )
    # the literal canonical text hashes to the recorded digest (independent of the code)
    assert (
        hashlib.sha256(GOLDEN["canonical_json"].encode()).hexdigest()
        == GOLDEN["sha256"]
    )


def _hash_of(volumes: dict[str, str]) -> tuple[str, dict[str, object]]:
    inputs = {
        m: market(m, bar_set=bars(count=90, quote_volume=qv))
        for m, qv in volumes.items()
    }
    result = u.daily_eligibility(volumes, inputs, asof=ASOF)
    h = u.u0_sha256(volumes)
    return (
        u.candidate_set_sha256(result, asof=ASOF, u0_hash=h),
        u.candidate_set_payload(result, asof=ASOF, u0_hash=h)["markets"],  # type: ignore[return-value]
    )


def test_hash_seals_the_30_day_sum_rank_and_tie_break_key() -> None:
    base_h, base = _hash_of({"KRW-A": "10000000000", "KRW-B": "11000000000"})
    assert base["KRW-B"]["liquidity_rank"] == 1  # type: ignore[index]
    assert base["KRW-A"]["liquidity_rank"] == 2  # type: ignore[index]
    assert base["KRW-A"]["turnover30_sum_krw"] == "300000000000"  # type: ignore[index]
    assert base["KRW-A"]["tie_break_key"] == "KRW-A"  # type: ignore[index]

    # only the sum changes (median stays the same): hash must change
    rows = list(bars(count=90, quote_volume="10000000000"))
    rows[0] = DailyBar(rows[0].day, rows[0].close, Decimal("10000000001"), True)
    bumped = market("KRW-A", bar_set=tuple(rows))
    assert u.judge_market(bumped, asof=ASOF).turnover30_median_krw == Decimal(
        "10000000000"
    )
    inputs = {
        "KRW-A": bumped,
        "KRW-B": market("KRW-B", bar_set=bars(count=90, quote_volume="11000000000")),
    }
    result = u.daily_eligibility(["KRW-A", "KRW-B"], inputs, asof=ASOF)
    h = u.u0_sha256(["KRW-A", "KRW-B"])
    assert u.candidate_set_sha256(result, asof=ASOF, u0_hash=h) != base_h

    # the sum flips the rank: A overtakes B, so the hash and the ranks differ
    flipped_h, flipped = _hash_of({"KRW-A": "12000000000", "KRW-B": "11000000000"})
    assert flipped["KRW-A"]["liquidity_rank"] == 1  # type: ignore[index]
    assert flipped_h != base_h


def test_equal_sums_are_ranked_by_market_code_and_ineligible_has_no_rank() -> None:
    _, tied = _hash_of({"KRW-B": "10000000000", "KRW-A": "10000000000"})
    assert tied["KRW-A"]["liquidity_rank"] == 1  # type: ignore[index]
    assert tied["KRW-B"]["liquidity_rank"] == 2  # type: ignore[index]
    result = u.daily_eligibility(
        ["KRW-ETH", "KRW-X"],
        {"KRW-ETH": market(), "KRW-X": market("KRW-X", warning="CAUTION")},
        asof=ASOF,
    )
    markets = u.candidate_set_payload(result, asof=ASOF, u0_hash="h")["markets"]
    assert markets["KRW-X"]["liquidity_rank"] is None  # type: ignore[index]
    assert markets["KRW-X"]["turnover30_sum_krw"] is None  # type: ignore[index]


def test_u0_ordered_hash_pins_the_cut_order() -> None:
    a = u.u0_ordered_sha256(["KRW-A", "KRW-B"])
    assert a != u.u0_ordered_sha256(["KRW-B", "KRW-A"])
    assert u.u0_sha256(["KRW-A", "KRW-B"]) == u.u0_sha256(["KRW-B", "KRW-A"])
    assert a == hashlib.sha256(b'["KRW-A","KRW-B"]').hexdigest()
    with pytest.raises(ValueError, match="duplicate"):
        u.u0_ordered_sha256(["KRW-A", "KRW-A"])


def test_candidate_set_hash_changes_with_any_field() -> None:
    base = u.daily_eligibility(["KRW-ETH"], {"KRW-ETH": market()}, asof=ASOF)
    other = u.daily_eligibility(
        ["KRW-ETH"],
        {"KRW-ETH": market(bar_set=bars(count=90, close="1001"))},
        asof=ASOF,
    )
    h = u.u0_sha256(["KRW-ETH"])
    assert u.candidate_set_sha256(base, asof=ASOF, u0_hash=h) != u.candidate_set_sha256(
        other, asof=ASOF, u0_hash=h
    )
    assert u.candidate_set_sha256(base, asof=ASOF, u0_hash=h) != u.candidate_set_sha256(
        base, asof=ASOF + timedelta(days=1), u0_hash=h
    )
