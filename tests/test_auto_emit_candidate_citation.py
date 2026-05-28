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


def test_buy_candidates_follow_candidate_rank_and_limit():
    snaps = [
        _Snap("portfolio", {"primary_source": "kis", "holdings": []}),
        # Symbol snapshots arrive in a different order from the screener rank.
        _Snap("symbol", {"symbol": "005930", "quote": _OK_QUOTE}, symbol="005930"),
        _Snap("symbol", {"symbol": "035720", "quote": _OK_QUOTE}, symbol="035720"),
        _Snap("symbol", {"symbol": "000660", "quote": _OK_QUOTE}, symbol="000660"),
        _Snap(
            "candidate_universe",
            {
                "usefulness": "useful",
                "candidate_limit": 2,
                "candidates": [
                    {
                        "symbol": "000660",
                        "score": 9.0,
                        "reasons": ["1순위"],
                        "source": "kis",
                    },
                    {
                        "symbol": "005930",
                        "score": 8.0,
                        "reasons": ["2순위"],
                        "source": "kis",
                    },
                    {
                        "symbol": "035720",
                        "score": 7.0,
                        "reasons": ["3순위"],
                        "source": "kis",
                    },
                ],
            },
        ),
    ]

    items = EvidenceAutoEmitter(max_buy_candidates=2).propose(
        snapshots=snaps, request_market="kr", account_scope=None
    )

    buys = [i for i in items if i.side == "buy"]
    assert [buy.symbol for buy in buys] == ["000660", "005930"]
    assert [buy.priority for buy in buys] == [1, 2]
    assert [buy.evidence_snapshot["candidate_rank"] for buy in buys] == [1, 2]


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


_DEAD_QUOTE = {
    "status": "ok",
    "best_bid": 0,
    "best_ask": 0,
    "bid_depth": 0,
    "ask_depth": 0,
    "spread_bps": 0,
}


def _verdict_of(item):
    return item.evidence_snapshot.get("action_verdict")


def test_candidate_without_quote_snapshot_is_data_gap_not_dropped():
    snaps = [
        _Snap("portfolio", {"primary_source": "kis", "holdings": []}),
        # No symbol snapshot for 000660 at all.
        _Snap(
            "candidate_universe",
            {
                "usefulness": "useful",
                "candidates": [{"symbol": "000660", "score": 9.0, "rank": 1}],
            },
        ),
    ]
    items = EvidenceAutoEmitter().propose(
        snapshots=snaps, request_market="kr", account_scope=None
    )
    cand = [i for i in items if i.symbol == "000660"]
    assert len(cand) == 1
    assert _verdict_of(cand[0]) == "data_gap"
    assert cand[0].evidence_snapshot["reject_or_wait_reason"] == "quote_missing"
    assert cand[0].operation == "review"
    assert cand[0].apply_policy == "requires_user_approval"


def test_low_liquidity_candidate_is_watch_only():
    snaps = [
        _Snap("portfolio", {"primary_source": "kis", "holdings": []}),
        _Snap("symbol", {"symbol": "000660", "quote": _DEAD_QUOTE}, symbol="000660"),
        _Snap(
            "candidate_universe",
            {
                "usefulness": "useful",
                "candidates": [{"symbol": "000660", "score": 9.0, "rank": 1}],
            },
        ),
    ]
    items = EvidenceAutoEmitter().propose(
        snapshots=snaps, request_market="kr", account_scope=None
    )
    cand = [i for i in items if i.symbol == "000660"]
    assert len(cand) == 1
    assert _verdict_of(cand[0]) == "watch_only"
    assert cand[0].evidence_snapshot["reject_or_wait_reason"] == "low_liquidity"


def test_stale_universe_candidates_are_watch_only_not_buy():
    snaps = [
        _Snap("portfolio", {"primary_source": "kis", "holdings": []}),
        _Snap("symbol", {"symbol": "000660", "quote": _OK_QUOTE}, symbol="000660"),
        _Snap(
            "candidate_universe",
            {
                "usefulness": "stale",  # not "useful"
                "candidates": [{"symbol": "000660", "score": 9.0, "rank": 1}],
            },
        ),
    ]
    items = EvidenceAutoEmitter().propose(
        snapshots=snaps, request_market="kr", account_scope=None
    )
    assert [i for i in items if i.side == "buy"] == []
    cand = [i for i in items if i.symbol == "000660"]
    assert len(cand) == 1
    assert _verdict_of(cand[0]) == "watch_only"
    assert cand[0].evidence_snapshot["reject_or_wait_reason"] == "screener_stale"


def test_overflow_beyond_cap_downgrades_to_watch_only():
    snaps = [
        _Snap("portfolio", {"primary_source": "kis", "holdings": []}),
        _Snap("symbol", {"symbol": "000660", "quote": _OK_QUOTE}, symbol="000660"),
        _Snap("symbol", {"symbol": "005930", "quote": _OK_QUOTE}, symbol="005930"),
        _Snap(
            "candidate_universe",
            {
                "usefulness": "useful",
                "candidates": [
                    {"symbol": "000660", "score": 9.0, "rank": 1},
                    {"symbol": "005930", "score": 8.0, "rank": 2},
                ],
            },
        ),
    ]
    items = EvidenceAutoEmitter(max_buy_candidates=1).propose(
        snapshots=snaps, request_market="kr", account_scope=None
    )
    buys = [i for i in items if i.side == "buy"]
    assert [i.symbol for i in buys] == ["000660"]
    overflow = [i for i in items if i.symbol == "005930"]
    assert len(overflow) == 1
    assert _verdict_of(overflow[0]) == "watch_only"
    assert overflow[0].evidence_snapshot["reject_or_wait_reason"] == "beyond_candidate_budget"


def test_held_candidate_not_double_emitted():
    snaps = [
        _Snap(
            "portfolio",
            {
                "primary_source": "kis",
                "holdings": [{"ticker": "000660", "sellable_quantity": 0}],
            },
        ),
        _Snap("symbol", {"symbol": "000660", "quote": _OK_QUOTE}, symbol="000660"),
        _Snap(
            "candidate_universe",
            {
                "usefulness": "useful",
                "candidates": [{"symbol": "000660", "score": 9.0, "rank": 1}],
            },
        ),
    ]
    items = EvidenceAutoEmitter().propose(
        snapshots=snaps, request_market="kr", account_scope=None
    )
    keys = [i.client_item_key for i in items if i.symbol == "000660"]
    # Held name routes through held_and_trending only — no candidate buy/watch row.
    assert all(not k.startswith("auto-cand-") for k in keys)
    assert all(not k.startswith("auto-buy-") for k in keys)

