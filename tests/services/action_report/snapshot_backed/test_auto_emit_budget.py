import datetime as dt
from types import SimpleNamespace

from app.services.action_report.snapshot_backed.auto_emit import EvidenceAutoEmitter


def _snap(kind, payload, symbol=None):
    return SimpleNamespace(
        snapshot_uuid=None, snapshot_kind=kind, payload_json=payload, symbol=symbol
    )


def _q():
    return {
        "status": "ok",
        "best_bid": 10,
        "best_ask": 10.1,
        "bid_depth": 100,
        "ask_depth": 100,
    }


def _snaps(buying_power):
    cands = [
        {
            "symbol": "GOOD",
            "rank": 1,
            "candidate_rank": 1,
            "data_state": "fresh",
            "quality_flags": [],
            "priority_score": 0.9,
        }
    ]
    return [
        _snap(
            "portfolio",
            {"buying_power": buying_power, "primary_source": "kis", "holdings": []},
        ),
        _snap("candidate_universe", {"usefulness": "useful", "candidates": cands}),
        _snap("symbol", {"symbol": "GOOD", "quote": _q()}, symbol="GOOD"),
    ]


def _item(snaps, **budget):
    items = EvidenceAutoEmitter().propose(
        snapshots=snaps,
        request_market="us",
        account_scope="kis_live",
        now=dt.datetime(2026, 6, 9),
        **budget,
    )
    return next(i for i in items if i.symbol == "GOOD")


def test_usd_zero_demotes_with_budget_gap():
    item = _item(_snaps({"usd": 0, "krw": 0}))
    ev = item.evidence_snapshot
    assert ev["action_verdict"] == "watch_only"
    assert "budget_gap" in ev["budget_reasons"]
    assert ev["budget_basis"] == "available_usd"


def test_usd_zero_with_krw_present_not_summed():
    # default basis=available_usd; KRW present must NOT be summed into USD.
    ev = _item(_snaps({"usd": 0, "krw": 500000})).evidence_snapshot
    assert "fx_required" in ev["budget_reasons"]
    assert ev["available_usd"] in (0, 0.0)
    assert ev["krw_orderable_reference"] == 500000  # reference, not summed into USD


def test_krw_orderable_reference_basis_flags_fx_required_only():
    # basis=krw_orderable_reference (no override) → watch_only with fx_required
    # ONLY (no budget_gap / operator_budget_required); KRW never fabricated to USD.
    ev = _item(
        _snaps({"usd": 0, "krw": 500000}),
        budget_basis="krw_orderable_reference",
    ).evidence_snapshot
    assert ev["action_verdict"] == "watch_only"
    assert ev["budget_reasons"] == ["fx_required"]


def test_operator_override_keeps_buy():
    ev = _item(
        _snaps({"usd": 0, "krw": 0}),
        budget_basis="operator_budget_override",
        operator_budget_override_usd=500,
    ).evidence_snapshot
    assert ev["action_verdict"] == "buy_review"


def test_usd_positive_keeps_buy():
    ev = _item(_snaps({"usd": 2000, "krw": 0})).evidence_snapshot
    assert ev["action_verdict"] == "buy_review"
