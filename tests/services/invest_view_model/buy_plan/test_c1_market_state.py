"""task-792 C1 — the crypto recovery gate is a single market-state coefficient.

Live vocabulary is exactly m = {1.0, 0.5, 0.0} for 2/2, 1/2, 0/2 met; a
missing OR stale input resolves to ``hold`` and is never inferred as 0/2.
Breadth is the share of KRW-quoted alts whose 24h change exceeds KRW-BTC's,
counted once inside this gate — there is no second breadth multiplier. The
0.25 arm exists only in the crypto mock paired virtual ledger and cannot be
declared on the live policy.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from app.schemas.trading_policy import PolicyRecoveryGate
from app.services.invest_view_model.buy_plan import service as buy_plan_service
from app.services.invest_view_model.buy_plan.gate_inputs import (
    GateMetricReading,
    evaluate_gate_conditions,
    parse_alt_breadth_reading,
    parse_btc_long_short_reading,
    resolve_market_state_coefficient,
)
from scripts.policy_table.adapters import crypto as crypto_adapter

pytestmark = pytest.mark.unit

NOW_EPOCH = 1_790_000_000.0  # any fixed epoch; freshness is measured off this


def _gate() -> PolicyRecoveryGate:
    """The shipped C1 gate shape, minimal and self-contained."""

    return PolicyRecoveryGate.model_validate(
        {
            "lanes": ["buy"],
            "advisory": True,
            "semantics": "C1 market-state coefficient",
            "min_conditions_met": 2,
            "of": 2,
            "missing_or_null_threshold": "do_not_infer_or_count_as_met",
            "size_coefficient": {
                "applies_to": "crypto_new_entry_notional",
                "by_met_count": {0: 0.0, 1: 0.5, 2: 1.0},
                "on_missing_or_stale_input": "hold",
                "fixed_at": "episode_first_order",
            },
            "conditions": [
                {
                    "id": "alt_breadth_24h",
                    "metric": "upbit_alt_breadth_24h",
                    "sources": ["upbit_open_api_ticker_derived"],
                    "operator": "gt",
                    "threshold": 50,
                    "unit": "percent",
                    "stale_after_seconds": 1800,
                    "semantics": "share of KRW alts outperforming BTC over 24h",
                },
                {
                    "id": "btc_long_short_ratio",
                    "metric": "btc_long_short_ratio",
                    "sources": [
                        "binance_global_account",
                        "binance_top_trader_position",
                    ],
                    "operator": "lte",
                    "threshold": 1.5,
                    "unit": "ratio",
                    "stale_after_seconds": 10800,
                    "semantics": "both report inputs at or below threshold",
                },
            ],
        }
    )


def _breadth(
    value: str | None, *, observed_at: float | None = NOW_EPOCH
) -> GateMetricReading:
    return GateMetricReading(
        metric="upbit_alt_breadth_24h",
        value=None if value is None else Decimal(value),
        source="stub",
        observed_at=observed_at,
    )


def _lsr(
    value: str | None, *, observed_at: float | None = NOW_EPOCH
) -> GateMetricReading:
    return GateMetricReading(
        metric="btc_long_short_ratio",
        value=None if value is None else Decimal(value),
        source="stub",
        observed_at=observed_at,
    )


def _readings(
    breadth: GateMetricReading, lsr: GateMetricReading
) -> dict[str, GateMetricReading]:
    return {breadth.metric: breadth, lsr.metric: lsr}


# ---------------------------------------------------------------------------
# Pure evaluation — the met-count table and the hold rule.
# ---------------------------------------------------------------------------


def test_two_of_two_resolves_full_coefficient() -> None:
    verdicts, met, unavailable, stale = evaluate_gate_conditions(
        _gate(),
        readings=_readings(_breadth("62"), _lsr("1.2")),
        now_epoch=NOW_EPOCH,
    )

    assert (met, unavailable, stale) == (2, 0, 0)
    assert all(v.state == "met" for v in verdicts)
    assert resolve_market_state_coefficient(
        _gate().size_coefficient, met_count=met, unresolved_count=0
    ) == ("resolved", Decimal("1.0"))


def test_one_of_two_resolves_half_coefficient() -> None:
    _, met, unavailable, stale = evaluate_gate_conditions(
        _gate(),
        readings=_readings(_breadth("30"), _lsr("1.2")),
        now_epoch=NOW_EPOCH,
    )

    assert (met, unavailable, stale) == (1, 0, 0)
    assert resolve_market_state_coefficient(
        _gate().size_coefficient, met_count=met, unresolved_count=0
    ) == ("resolved", Decimal("0.5"))


def test_zero_of_two_resolves_zero_coefficient() -> None:
    _, met, unavailable, stale = evaluate_gate_conditions(
        _gate(),
        readings=_readings(_breadth("10"), _lsr("1.9")),
        now_epoch=NOW_EPOCH,
    )

    assert (met, unavailable, stale) == (0, 0, 0)
    assert resolve_market_state_coefficient(
        _gate().size_coefficient, met_count=met, unresolved_count=0
    ) == ("resolved", Decimal("0"))


def test_stale_input_holds_even_when_the_other_leg_passes() -> None:
    verdicts, met, unavailable, stale = evaluate_gate_conditions(
        _gate(),
        readings=_readings(_breadth("62"), _lsr("1.2", observed_at=NOW_EPOCH - 10_801)),
        now_epoch=NOW_EPOCH,
    )

    assert (met, unavailable, stale) == (1, 0, 1)
    lsr_verdict = next(v for v in verdicts if v.metric == "btc_long_short_ratio")
    assert lsr_verdict.state == "stale"
    # The stale leg is not trusted AND not counted as an inferred miss —
    # the whole decision is hold.
    assert resolve_market_state_coefficient(
        _gate().size_coefficient, met_count=met, unresolved_count=unavailable + stale
    ) == ("hold", None)


def test_missing_input_holds_never_inferred_zero_of_two() -> None:
    verdicts, met, unavailable, stale = evaluate_gate_conditions(
        _gate(),
        readings=_readings(_breadth(None), _lsr("1.2")),
        now_epoch=NOW_EPOCH,
    )

    assert (met, unavailable, stale) == (1, 1, 0)
    assert (
        next(v for v in verdicts if v.metric == "upbit_alt_breadth_24h").state
        == "unavailable"
    )
    # NOT ("resolved", Decimal("0")) — missing input cannot become 0/2.
    assert resolve_market_state_coefficient(
        _gate().size_coefficient, met_count=met, unresolved_count=unavailable + stale
    ) == ("hold", None)


def test_undated_reading_under_a_freshness_bound_is_unavailable() -> None:
    """A readable value with no observation timestamp is freshness-unproven."""

    verdicts, met, unavailable, stale = evaluate_gate_conditions(
        _gate(),
        readings=_readings(_breadth("62", observed_at=None), _lsr("1.2")),
        now_epoch=NOW_EPOCH,
    )

    assert (met, unavailable, stale) == (1, 1, 0)
    assert (
        next(v for v in verdicts if v.metric == "upbit_alt_breadth_24h").state
        == "unavailable"
    )
    assert resolve_market_state_coefficient(
        _gate().size_coefficient, met_count=met, unresolved_count=unavailable + stale
    ) == ("hold", None)


def test_future_dated_reading_holds_instead_of_appearing_fresh() -> None:
    verdicts, met, unavailable, stale = evaluate_gate_conditions(
        _gate(),
        readings=_readings(_breadth("62", observed_at=NOW_EPOCH + 60), _lsr("1.2")),
        now_epoch=NOW_EPOCH,
    )

    assert (met, unavailable, stale) == (1, 1, 0)
    assert verdicts[0].state == "unavailable"
    assert resolve_market_state_coefficient(
        _gate().size_coefficient, met_count=met, unresolved_count=unavailable + stale
    ) == ("hold", None)


def test_breadth_is_counted_once_inside_the_gate() -> None:
    """The gate has exactly one breadth leg and no second breadth consumer."""

    gate = _gate()
    breadth_legs = [c for c in gate.conditions if "breadth" in c.metric]
    assert len(breadth_legs) == 1
    # The coefficient comes only from by_met_count — there is no breadth-
    # specific multiplier field anywhere on the spec.
    spec_keys = set(gate.size_coefficient.model_dump())
    assert spec_keys == {
        "applies_to",
        "by_met_count",
        "on_missing_or_stale_input",
        "fixed_at",
    }


def test_coefficient_is_fixed_at_episode_entry() -> None:
    spec = _gate().size_coefficient
    assert spec.fixed_at == "episode_first_order"
    assert spec.on_missing_or_stale_input == "hold"


# ---------------------------------------------------------------------------
# Schema — the live vocabulary excludes 0.25 and must cover every count.
# ---------------------------------------------------------------------------


def test_live_schema_rejects_the_quarter_arm() -> None:
    spec = _gate().model_dump()
    spec["size_coefficient"]["by_met_count"][0] = 0.25
    with pytest.raises(ValidationError):
        PolicyRecoveryGate.model_validate(spec)


def test_live_schema_requires_every_met_count() -> None:
    spec = _gate().model_dump()
    del spec["size_coefficient"]["by_met_count"][0]
    with pytest.raises(ValidationError, match="every met count"):
        PolicyRecoveryGate.model_validate(spec)


def test_live_schema_rejects_decreasing_coefficients() -> None:
    spec = _gate().model_dump()
    spec["size_coefficient"]["by_met_count"] = {0: 1.0, 1: 0.5, 2: 0.0}
    with pytest.raises(ValidationError, match="non-decreasing"):
        PolicyRecoveryGate.model_validate(spec)


def test_live_schema_rejects_a_different_monotonic_table() -> None:
    spec = _gate().model_dump()
    spec["size_coefficient"]["by_met_count"] = {0: 0.5, 1: 0.5, 2: 1.0}
    with pytest.raises(ValidationError, match="exactly"):
        PolicyRecoveryGate.model_validate(spec)


# ---------------------------------------------------------------------------
# Payload parsers — BTC-relative breadth and the fresher-leg rule.
# ---------------------------------------------------------------------------


def test_breadth_parser_is_btc_relative_and_dated() -> None:
    reading = parse_alt_breadth_reading(
        {
            "breadth": {
                "alts_total": 100,
                "alts_beating_btc": 62,
                "alts_beating_btc_pct": 0.62,  # upstream fraction, not percent
                "btc_change_24h": -0.011,
                "latest_trade_at": "2025-11-10T01:02:03+00:00",
            }
        }
    )

    # Fraction converted once to the policy's percent unit.
    assert reading.value == Decimal("62")
    assert reading.observed_at is not None
    assert reading.note == "62/100 alts > KRW-BTC (24h)"


def test_breadth_parser_undated_payload_cannot_prove_freshness() -> None:
    reading = parse_alt_breadth_reading(
        {"breadth": {"alts_beating_btc_pct": 0.62, "alts_total": 100}}
    )

    assert reading.value == Decimal("62")
    assert reading.observed_at is None


def test_lsr_parser_takes_the_worse_ratio_and_the_stalest_leg() -> None:
    reading = parse_btc_long_short_reading(
        {
            "global_account": {
                "ratio": 1.1,
                "history": [{"time": "2025-11-10T01:00:00Z"}],
            },
            "top_position": {
                "ratio": 1.4,
                "history": [{"time": "2025-11-10T00:30:00Z"}],
            },
        }
    )

    assert reading.value == Decimal("1.4")  # the worse (higher) of the pair
    # observed_at is the OLDER leg — the pair is only as fresh as its
    # stalest report. 2025-11-10T00:30:00Z == 1762734600 epoch.
    assert reading.observed_at == pytest.approx(1_762_734_600.0, abs=1)


def test_lsr_parser_refuses_a_half_answer() -> None:
    reading = parse_btc_long_short_reading(
        {
            "global_account": {"ratio": 1.1, "history": []},
            "top_position": None,
        }
    )

    assert reading.value is None


def test_lsr_parser_cannot_date_pair_from_only_one_leg() -> None:
    reading = parse_btc_long_short_reading(
        {
            "global_account": {
                "ratio": 1.1,
                "history": [{"time": "2025-11-10T01:00:00Z"}],
            },
            "top_position": {"ratio": 1.2, "history": []},
        }
    )

    assert reading.value == Decimal("1.2")
    assert reading.observed_at is None


@pytest.mark.asyncio
async def test_buy_plan_stale_leg_holds_its_live_coefficient(monkeypatch) -> None:
    async def breadth() -> GateMetricReading:
        return _breadth("62")

    async def lsr() -> GateMetricReading:
        return _lsr("1.2", observed_at=NOW_EPOCH - 20_000)

    monkeypatch.setattr(buy_plan_service, "read_alt_breadth_24h", breadth)
    monkeypatch.setattr(buy_plan_service, "read_btc_long_short_ratio", lsr)
    policy = SimpleNamespace(
        market_rules={"crypto": SimpleNamespace(recovery_gate=_gate())}
    )
    rows = await buy_plan_service._build_discovery_gates(
        policy=policy, now=dt.datetime.fromtimestamp(NOW_EPOCH, dt.UTC)
    )

    assert len(rows) == 1
    assert rows[0].stale_count == 1
    assert rows[0].coefficient is not None
    assert rows[0].coefficient.state == "hold"
    assert rows[0].coefficient.value is None


# ---------------------------------------------------------------------------
# Adapter — the market_state block is resolved from captured raw inputs only.
# ---------------------------------------------------------------------------


def _raw_with_gate(**overrides: Any) -> crypto_adapter.RawInputs:
    gate = _gate().model_dump()
    spec: dict[str, Any] = {
        "as_of": "2025-11-10T01:30:00+00:00",  # NOW_EPOCH
        "holdings": [],
        "watch_alerts": [],
        "top_traded": [],
        "orderable_krw": "0",
        "candles": {},
        "altseason": {
            "breadth": {
                "alts_total": 100,
                "alts_beating_btc": 62,
                "alts_beating_btc_pct": 0.62,
                "btc_change_24h": -0.011,
                "latest_trade_at": "2025-11-10T01:29:00+00:00",
            }
        },
        "long_short_ratio": {
            "global_account": {
                "ratio": 1.1,
                "history": [{"time": "2025-11-10T01:00:00Z"}],
            },
            "top_position": {
                "ratio": 1.2,
                "history": [{"time": "2025-11-10T01:00:00Z"}],
            },
        },
        "recovery_gate": gate,
    }
    spec.update(overrides)
    return crypto_adapter.RawInputs(**spec)


def test_adapter_market_state_resolves_two_of_two() -> None:
    state = crypto_adapter._build_market_state(_raw_with_gate())

    assert state["decision"] == "resolved"
    assert state["coefficient"] == Decimal("1.0")
    assert state["met_count"] == 2
    assert state["basis_met_count"] == 2
    assert state["fixed_at"] == "episode_first_order"
    assert state["breadth_counted_once"] is True
    assert {leg["state"] for leg in state["legs"]} == {"met"}


def test_adapter_market_state_holds_on_missing_input() -> None:
    state = crypto_adapter._build_market_state(_raw_with_gate(altseason=None))

    assert state["decision"] == "hold"
    assert state["coefficient"] is None
    assert state["unavailable_count"] == 1


def test_adapter_market_state_holds_on_stale_input() -> None:
    stale_lsr = {
        "global_account": {
            "ratio": 1.1,
            # 4h old — beyond the 10800s freshness bound for this leg.
            "history": [{"time": "2025-11-09T21:00:00Z"}],
        },
        "top_position": {
            "ratio": 1.2,
            "history": [{"time": "2025-11-10T01:00:00Z"}],
        },
    }
    state = crypto_adapter._build_market_state(
        _raw_with_gate(long_short_ratio=stale_lsr)
    )

    assert state["decision"] == "hold"
    assert state["coefficient"] is None
    assert state["stale_count"] == 1


def test_adapter_market_state_holds_without_a_captured_spec() -> None:
    """A pre-C1 replay dump has no gate spec — hold, never an inferred 0/2."""

    state = crypto_adapter._build_market_state(_raw_with_gate(recovery_gate=None))

    assert state["decision"] == "hold"
    assert state["coefficient"] is None
    assert state["available"] is False


def test_adapter_market_state_never_emits_the_mock_arm() -> None:
    """Whatever the inputs resolve to, the table can only show 1.0/0.5/0/hold."""

    for breadth_pct, lsr_ratio in (("62", "1.2"), ("30", "1.2"), ("10", "1.9")):
        raw = _raw_with_gate()
        raw.altseason["breadth"]["alts_beating_btc_pct"] = int(breadth_pct) / 100
        raw.long_short_ratio["top_position"]["ratio"] = float(lsr_ratio)
        state = crypto_adapter._build_market_state(raw)
        assert state["coefficient"] in (
            Decimal("1.0"),
            Decimal("0.5"),
            Decimal("0"),
            None,
        )
        assert state["coefficient"] != Decimal("0.25")


def test_raw_inputs_replay_roundtrip_carries_the_gate_spec() -> None:
    """A dumped raw snapshot re-validates into the identical RawInputs."""

    raw = _raw_with_gate()
    replayed = crypto_adapter.RawInputs.from_jsonable(raw.to_jsonable())

    assert replayed == raw
    # ...and a pre-C1 dump without the new keys still replays.
    legacy = raw.to_jsonable()
    for key in ("altseason", "long_short_ratio", "recovery_gate"):
        del legacy[key]
    assert crypto_adapter.RawInputs.from_jsonable(legacy).recovery_gate is None
