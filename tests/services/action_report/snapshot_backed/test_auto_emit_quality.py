import datetime as dt
from types import SimpleNamespace

from app.services.action_report.snapshot_backed.auto_emit import EvidenceAutoEmitter


def _snap(kind, payload, symbol=None):
    return SimpleNamespace(
        snapshot_uuid=None, snapshot_kind=kind, payload_json=payload, symbol=symbol
    )


def _actionable_quote(sym):
    return {
        "status": "ok",
        "best_bid": 10,
        "best_ask": 10.1,
        "bid_depth": 100,
        "ask_depth": 100,
    }


def test_penny_candidate_demoted_to_watch_with_reason():
    cands = [
        {
            "symbol": "PENNY",
            "rank": 1,
            "candidate_rank": 1,
            "data_state": "fresh",
            "quality_flags": ["penny", "illiquid"],
            "priority_score": 0.1,
            "confidence_cap": None,
        }
    ]
    snaps = [
        _snap("candidate_universe", {"usefulness": "useful", "candidates": cands}),
        _snap(
            "symbol",
            {"symbol": "PENNY", "quote": _actionable_quote("PENNY")},
            symbol="PENNY",
        ),
    ]
    items = EvidenceAutoEmitter().propose(
        snapshots=snaps,
        request_market="us",
        account_scope="kis_live",
        now=dt.datetime(2026, 6, 9),
    )
    item = next(i for i in items if i.symbol == "PENNY")
    assert item.evidence_snapshot["action_verdict"] == "watch_only"
    assert item.evidence_snapshot["reject_or_wait_reason"] == "penny"
    assert "penny" in item.evidence_snapshot["quality_flags"]


def test_non_common_candidate_rejected():
    cands = [
        {
            "symbol": "ETF1",
            "rank": 1,
            "candidate_rank": 1,
            "data_state": "fresh",
            "quality_flags": ["non_common_stock"],
            "priority_score": 0.5,
        }
    ]
    snaps = [
        _snap("candidate_universe", {"usefulness": "useful", "candidates": cands}),
        _snap(
            "symbol",
            {"symbol": "ETF1", "quote": _actionable_quote("ETF1")},
            symbol="ETF1",
        ),
    ]
    items = EvidenceAutoEmitter().propose(
        snapshots=snaps,
        request_market="us",
        account_scope="kis_live",
        now=dt.datetime(2026, 6, 9),
    )
    item = next(i for i in items if i.symbol == "ETF1")
    assert item.evidence_snapshot["action_verdict"] == "rejected"
    assert item.evidence_snapshot["reject_or_wait_reason"] == "non_common_stock"


def test_clean_candidate_stays_buy_review():
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
    snaps = [
        _snap("candidate_universe", {"usefulness": "useful", "candidates": cands}),
        _snap(
            "symbol",
            {"symbol": "GOOD", "quote": _actionable_quote("GOOD")},
            symbol="GOOD",
        ),
    ]
    items = EvidenceAutoEmitter().propose(
        snapshots=snaps,
        request_market="us",
        account_scope="kis_live",
        now=dt.datetime(2026, 6, 9),
    )
    item = next(i for i in items if i.symbol == "GOOD")
    assert item.evidence_snapshot["action_verdict"] == "buy_review"
