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
