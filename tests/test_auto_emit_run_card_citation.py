"""ROB-332 — auto_emit cites a validated_run_card snapshot when symbols match."""

import json

from app.services.action_report.snapshot_backed.auto_emit import EvidenceAutoEmitter


class _Snap:
    def __init__(self, kind, payload, symbol=None, uuid="rc-uuid-0001"):
        self.snapshot_kind = kind
        self.payload_json = payload
        self.symbol = symbol
        self.snapshot_uuid = uuid


_OK_QUOTE = {
    "status": "ok",
    "best_bid": 100,
    "best_ask": 101,
    "bid_depth": 5,
    "ask_depth": 5,
    "spread_bps": 10,
}


def _run_card(symbols, verdict="not_validated"):
    return {
        "schema_version": "validated_run_card.v1",
        "verdict": verdict,
        "framing": "audit evidence, not a pass stamp",
        "net_after_cost": {"trades": 12, "profit_factor": float("inf")},
        "validation": {"bootstrap": {"ci_lower": 0.1}, "monte_carlo": {"p_value": 0.4}},
        "gate_report": {"symbols": symbols, "trade_count": 12},
    }


def _buy_universe(symbol):
    return [
        _Snap("portfolio", {"primary_source": "kis", "holdings": []}),
        _Snap("symbol", {"symbol": symbol, "quote": _OK_QUOTE}, symbol=symbol),
        _Snap(
            "candidate_universe",
            {
                "usefulness": "useful",
                "candidates": [
                    {
                        "symbol": symbol,
                        "score": 8.0,
                        "reasons": ["momentum"],
                        "source": "kis",
                    }
                ],
            },
        ),
    ]


def test_buy_item_for_matching_symbol_cites_run_card():
    snaps = _buy_universe("005930") + [
        _Snap("validated_run_card", _run_card(["005930"]), symbol="005930")
    ]
    items = EvidenceAutoEmitter().propose(
        snapshots=snaps, request_market="kr", account_scope=None
    )
    buys = [i for i in items if i.side == "buy"]
    assert buys, "expected a buy candidate"
    rc = buys[0].evidence_snapshot["run_card"]
    assert rc["verdict"] == "not_validated"
    assert rc["is_pass_stamp"] is False
    assert rc["trade_count"] == 12
    assert rc["snapshot_uuid"] == "rc-uuid-0001"
    # Non-finite sanitized; bootstrap/MC nested under validation, not standalone.
    assert rc["net_after_cost"]["profit_factor"] is None
    assert "bootstrap" in rc["validation"]
    json.dumps(buys[0].evidence_snapshot, allow_nan=False)


def test_validated_verdict_flows_is_pass_stamp_true():
    snaps = _buy_universe("005930") + [
        _Snap("validated_run_card", _run_card(["005930"], verdict="validated"))
    ]
    items = EvidenceAutoEmitter().propose(
        snapshots=snaps, request_market="kr", account_scope=None
    )
    buys = [i for i in items if i.side == "buy"]
    assert buys[0].evidence_snapshot["run_card"]["is_pass_stamp"] is True


def test_run_card_present_but_symbol_not_overlapping_is_not_cited():
    snaps = _buy_universe("005930") + [
        _Snap("validated_run_card", _run_card(["XRPUSDT"]))
    ]
    items = EvidenceAutoEmitter().propose(
        snapshots=snaps, request_market="kr", account_scope=None
    )
    assert all("run_card" not in i.evidence_snapshot for i in items)


def test_no_run_card_in_bundle_leaves_items_unchanged():
    items = EvidenceAutoEmitter().propose(
        snapshots=_buy_universe("005930"), request_market="kr", account_scope=None
    )
    assert items
    assert all("run_card" not in i.evidence_snapshot for i in items)


def test_empty_bundle_returns_no_items():
    items = EvidenceAutoEmitter().propose(
        snapshots=[], request_market="kr", account_scope=None
    )
    assert items == []
