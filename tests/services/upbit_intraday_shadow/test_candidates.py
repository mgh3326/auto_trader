"""Which closed bars become signal or excluded rows (hk 6036)."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

from app.services.upbit_intraday_shadow import contracts as K
from app.services.upbit_intraday_shadow.candidates import build_panel, evaluate_market
from app.services.upbit_intraday_shadow.recorder import run_once
from research.upbit_intraday import rules as R
from tests.services.upbit_intraday_shadow import support as S

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
MARKET = "KRW-AAA"
A_CONFIGS = [f"A_breakout:N{n}_X{x}" for n in (16, 32) for x in (3, 5)]


def flat(count: int = 240, q: str = "40000000") -> dict[int, object]:
    return {i: S.bar(i, q=q) for i in range(count)}


def breakout(index: int, *, level: int = 105, q: str = "200000000"):
    return S.bar(index, o="100", h=str(level + 1), low="99", c=str(level), q=q)


def evaluate(
    rows: dict,
    index: int,
    *,
    market: str = MARKET,
    event: object = S.QUIET,
    observed_offset: timedelta = timedelta(0),
    catchup: int = 1,
):
    now = S.run_after(index)
    fake = S.FakeUpbit({market: list(rows.values())})
    page = S.data_at(fake, now).candles(market, S.at(index + 1))
    return evaluate_market(
        page,
        end_slot=S.end_slot_after(index),
        catchup_bars=catchup,
        market_event=event,
        market_event_observed_at=now + observed_offset,
        selected=K.selected_config_ids(),
    )


def test_a_quiet_bar_is_not_a_candidate() -> None:
    assert evaluate(flat(), 200) == []


def test_a_frozen_breakout_is_a_signal_for_every_matching_configuration() -> None:
    rows = flat() | {200: breakout(200)}
    found = evaluate(rows, 200)
    assert [c.config_id for c in found] == A_CONFIGS
    first = found[0]
    assert (first.disposition, first.exclusion_reasons) == ("signal", ())
    assert first.signal_bar_open_at == S.at(200)
    assert first.signal_time == S.at(201)
    assert first.first_received_at == S.run_after(200)
    assert first.ref_level == Decimal("101")
    assert first.median_quote_volume == Decimal("40000000")
    assert first.volume_ratio == Decimal("5")
    assert first.quote_volume_24h == Decimal("40000000") * 95 + Decimal("200000000")
    assert first.liquidity_bucket == "L1"
    assert first.roundtrip_cost_rate == Decimal("0.005")
    assert first.signal_tick_krw == Decimal("1")
    assert [c.selected_config for c in found] == [True, False, False, False]


def test_held_and_stable_markets_follow_the_frozen_partition() -> None:
    rows = flat() | {200: breakout(200)}
    held = {c.family for c in evaluate(rows, 200, market="KRW-BTC")}
    assert held == {"B_held_rebound"}
    assert evaluate(rows, 200, market="KRW-USDT") == []


def test_the_frozen_cooldown_suppresses_without_a_row() -> None:
    rows = flat() | {200: breakout(200), 210: breakout(210, level=115)}
    assert len(evaluate(rows, 200)) == 4
    assert evaluate(rows, 210) == []
    rows[217] = breakout(217, level=125)
    assert {c.disposition for c in evaluate(rows, 217)} == {"signal"}


def test_a_chain_that_may_depend_on_unseen_bars_is_held_back() -> None:
    # A breakout every 10 bars: no gap longer than the cooldown anywhere in the
    # panel, so which of them the frozen rule would keep cannot be known.
    rows = {}
    level = 100
    for i in range(240):
        if i >= 20 and i % 10 == 0:
            level += 5
            rows[i] = S.bar(
                i, o=str(level), h=str(level + 1), low=str(level - 1), c=str(level),
                q="200000000",
            )  # fmt: skip
        else:
            rows[i] = S.bar(
                i, o=str(level), h=str(level + 1), low=str(level - 1), c=str(level)
            )
    found = [c for c in evaluate(rows, 200) if c.family == "A_breakout"]
    assert found
    assert {c.exclusion_reasons for c in found} == {("cooldown_unverified",)}


def test_a_breakout_under_the_floor_is_held_back_for_liquidity() -> None:
    rows = flat(q="1000000") | {200: breakout(200, q="10000000")}
    found = evaluate(rows, 200)
    assert [c.config_id for c in found] == A_CONFIGS
    assert {c.exclusion_reasons for c in found} == {("liquidity_below_floor",)}
    # Without the relative-volume condition it is not a candidate at all.
    rows[200] = breakout(200, q="2000000")
    assert evaluate(rows, 200) == []


def test_a_thin_candidate_inside_a_signal_cooldown_has_no_row() -> None:
    rows = flat(q="1000000") | {
        200: breakout(200, q="30000000"),
        205: breakout(205, level=115, q="10000000"),
        220: breakout(220, level=125, q="10000000"),
    }
    assert {c.disposition for c in evaluate(rows, 200)} == {"signal"}
    assert evaluate(rows, 205) == []
    assert {c.exclusion_reasons for c in evaluate(rows, 220)} == {
        ("liquidity_below_floor",)
    }


@pytest.mark.parametrize(
    ("event", "offset", "state", "reason"),
    [
        (
            {"warning": True, "caution": {"X": True}},
            timedelta(0),
            "warning",
            "market_event_warning",
        ),
        (
            {"warning": False, "caution": {"X": True}},
            timedelta(0),
            "caution:X",
            "market_event_caution",
        ),
        (None, timedelta(0), "unknown", "market_event_unknown"),
        ({"warning": False}, timedelta(0), "unknown", "market_event_unknown"),
        (S.QUIET, -timedelta(hours=37), "stale", "market_event_stale"),
        # Observed 31 minutes after the signal time: nothing about that time.
        (S.QUIET, timedelta(minutes=30, seconds=1), "unknown", "market_event_unknown"),
    ],
)
def test_market_alert_states_hold_a_signal_back(event, offset, state, reason) -> None:
    rows = flat() | {200: breakout(200)}
    found = evaluate(rows, 200, event=event, observed_offset=offset)
    assert len(found) == 4
    assert {(c.market_event_state, c.exclusion_reasons) for c in found} == {
        (state, (reason,))
    }
    assert {c.disposition for c in found} == {"excluded"}


def test_every_applicable_reason_is_listed_in_vocabulary_order() -> None:
    rows = flat(q="1000000") | {200: breakout(200, q="10000000")}
    found = evaluate(rows, 200, event={"warning": True, "caution": {}})
    assert {c.exclusion_reasons for c in found} == {
        ("market_event_warning", "liquidity_below_floor")
    }


@pytest.mark.parametrize(
    ("seconds_after_close", "expected_rows"), [(-300, 0), (5, 0), (10, 4)]
)
def test_only_closed_and_settled_bars_are_evaluated(
    seconds_after_close, expected_rows
) -> None:
    rows = flat(201) | {200: breakout(200)}
    now = S.at(201) + timedelta(seconds=seconds_after_close)
    report = asyncio.run(
        run_once(
            S.data_at(S.FakeUpbit({MARKET: list(rows.values())}), now),
            run_id="t",
            now=now,
            catchup_bars=1,
        )
    )
    assert report["errors"] == []
    assert len(report["rows"]) == expected_rows


def test_catchup_cannot_outrun_the_24h_window() -> None:
    with pytest.raises(ValueError, match="catchup_bars"):
        evaluate(flat(), 200, catchup=K.MAX_CATCHUP_BARS + 1)
    assert K.MAX_CATCHUP_BARS == 96


def test_decimal_features_match_the_frozen_context() -> None:
    rng = np.random.default_rng(7)
    rows = {i: S.bar(i, q=f"{rng.lognormal(17, 1):.8f}") for i in range(200)} | {
        200: breakout(200, q="900000000.12345678")
    }
    found = evaluate(rows, 200)
    assert found
    start = S.end_slot_after(200) - K.PANEL_SLOTS + 1
    base = S.end_slot_after(0)
    panel = build_panel(
        MARKET, {base + i: r for i, r in rows.items()}, start, K.PANEL_SLOTS
    )
    ctx = R.build_context(panel)
    q = panel.q[0]
    i = K.PANEL_SLOTS - 1
    for candidate in found:
        assert float(candidate.median_quote_volume) == pytest.approx(
            np.median(q[i - 16 : i]), rel=1e-12
        )
        assert float(candidate.quote_volume_24h) == pytest.approx(
            q[i - 95 : i + 1].sum(), rel=1e-12
        )
        assert candidate.liquidity_bucket == R.BUCKET_NAMES[ctx.bucket[0, i]]
        assert ctx.raw(1, candidate.n_bars, float(candidate.x_multiple))[0, i]


def test_the_frozen_files_still_match_the_hk_6035_manifest() -> None:
    manifest = json.loads(
        (
            REPO / "research/experiments/manifests/hk-6035-upbit-intraday-signals.json"
        ).read_text()
    )
    for name in ("rules", "evaluate"):
        data = (REPO / f"research/upbit_intraday/{name}.py").read_bytes()
        assert (
            hashlib.sha256(data).hexdigest()
            == manifest["hashes"][f"code_{name}_sha256"]
        )
    assert K.FROZEN_RULES_SHA256 == manifest["hashes"]["code_rules_sha256"]
    K.verify_frozen_rules()


def test_a_changed_rules_file_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(K, "FROZEN_RULES_SHA256", "0" * 64)
    with pytest.raises(K.FrozenRulesChanged):
        K.verify_frozen_rules()


def test_selected_configurations_come_from_the_frozen_selection() -> None:
    assert K.selected_config_ids() == {
        "A_breakout:N16_X3",
        "B_held_rebound:N32_X5",
        "C_downside:N32_X5",
    }
