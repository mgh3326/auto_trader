from decimal import Decimal

import pytest

from app.services.shadow_replay.scoring import agree, extract_decision, summarize


@pytest.mark.unit
def test_extract_reads_trade_setup_stringified_decimals():
    item = {
        "side": "buy",
        "max_action": {"notional": "300000", "limit_price": "129600"},
        "evidence_snapshot": {
            "trade_setup": {
                "stop": "125000",
                "target": "150000",
                "headline": {"entry": "129600"},
            }
        },
        "trigger_checklist": ["support_129600_hold", "rsi_below_45"],
    }
    d = extract_decision(item)
    assert d["side"] == "buy"
    assert d["notional"] == Decimal("300000")
    assert d["limit_price"] == Decimal("129600")
    assert d["entry"] == Decimal("129600")
    assert d["stop"] == Decimal("125000")
    assert d["triggers"] == frozenset({"support_129600_hold", "rsi_below_45"})


@pytest.mark.unit
def test_extract_no_action_when_no_side():
    d = extract_decision(
        {
            "side": None,
            "max_action": {},
            "evidence_snapshot": {},
            "trigger_checklist": [],
        }
    )
    assert d["side"] is None


@pytest.mark.unit
def test_agree_limit_tolerance_and_side():
    a = {
        "side": "buy",
        "notional": Decimal("300000"),
        "limit_price": Decimal("129600"),
        "triggers": frozenset({"x"}),
    }
    b = {
        "side": "buy",
        "notional": Decimal("320000"),
        "limit_price": Decimal("129700"),
        "triggers": frozenset({"x"}),
    }
    r = agree(a, b, tick=Decimal("100"))
    assert r["side"] and r["size_band"] and r["limit"] and r["same_decision"]


@pytest.mark.unit
def test_summarize_exposes_no_action_rate():
    hold = {"side": None, "triggers": frozenset()}
    s = summarize([hold, hold], reference=None, tick=Decimal("100"))
    assert s["no_action_rate"] == 1.0
    assert (
        s["self_same_decision_rate"] == 1.0
    )  # degenerate agreement is VISIBLE via no_action_rate
