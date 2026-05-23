from app.services.action_report.snapshot_backed.auto_emit import EvidenceAutoEmitter


class _Snap:
    def __init__(self, kind, payload, symbol=None):
        self.snapshot_kind = kind
        self.payload_json = payload
        self.symbol = symbol
        self.snapshot_uuid = "11111111-1111-1111-1111-111111111111"


_OK_QUOTE = {
    "status": "ok",
    "best_bid": 100,
    "best_ask": 101,
    "bid_depth": 5,
    "ask_depth": 5,
    "spread_bps": 10,
}


def test_buy_item_cites_candidate_evidence():
    snaps = [
        _Snap("portfolio", {"primary_source": "kis", "holdings": []}),
        _Snap("symbol", {"symbol": "005930", "quote": _OK_QUOTE}, symbol="005930"),
        _Snap(
            "candidate_universe",
            {
                "usefulness": "useful",
                "candidates": [
                    {
                        "symbol": "005930",
                        "score": 8.0,
                        "reasons": ["단기 상승 모멘텀 후보"],
                        "source": "kis",
                    },
                ],
            },
        ),
    ]
    items = EvidenceAutoEmitter().propose(
        snapshots=snaps, request_market="kr", account_scope=None
    )
    buys = [i for i in items if i.side == "buy"]
    assert buys, "expected a buy candidate"
    ev = buys[0].evidence_snapshot
    assert ev["candidate_score"] == 8.0
    assert ev["candidate_source"] == "kis"
    assert ev["candidate_reasons"] == ["단기 상승 모멘텀 후보"]


def test_held_symbol_in_screener_surfaces_watch():
    snaps = [
        _Snap(
            "portfolio", {"primary_source": "kis", "holdings": [{"ticker": "005930"}]}
        ),
        _Snap(
            "candidate_universe",
            {
                "usefulness": "useful",
                "candidates": [
                    {
                        "symbol": "005930",
                        "score": 8.0,
                        "reasons": ["단기 상승 모멘텀 후보"],
                        "source": "kis",
                    },
                ],
            },
        ),
    ]
    items = EvidenceAutoEmitter().propose(
        snapshots=snaps, request_market="kr", account_scope="kis_live"
    )
    holds = [
        i
        for i in items
        if i.evidence_snapshot.get("proposer") == "auto_emit/held_and_trending"
    ]
    assert len(holds) == 1
    item = holds[0]
    assert item.symbol == "005930"
    assert item.item_kind == "watch"
    assert item.operation == "review"
    assert item.apply_policy == "requires_user_approval"
    assert item.evidence_snapshot["candidate_score"] == 8.0
    # Held symbol must NOT also be proposed as a buy.
    assert not [i for i in items if i.side == "buy" and i.symbol == "005930"]
