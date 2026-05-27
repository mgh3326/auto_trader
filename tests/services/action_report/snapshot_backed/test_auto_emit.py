"""ROB-278 Phase 2 — EvidenceAutoEmitter tests.

The auto-emitter is deterministic and fail-closed:

* Sell candidates require ``portfolio.primary_source='kis'`` + a held row
  with positive ``sellable_quantity`` AND the matching symbol snapshot's
  quote must report ``status='ok'`` with non-zero best bid/ask and at
  least one side of book depth.
* Buy candidates require ``candidate_universe.usefulness='useful'`` AND
  the symbol's quote evidence to be actionable (same gate as sell). The
  symbol must not already be held.
* Watch candidates require news activity (``symbol_matches > 0``) but
  insufficient action grounds (quote unavailable, or candidate evidence
  not useful, etc.).
* Every emitted item is ``operation='review'`` +
  ``apply_policy='requires_user_approval'``.
* Every emitted item carries an ``evidence_snapshot`` dict with the
  source snapshot's uuid + kind + symbol + a ``proposer`` tag.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from app.services.action_report.snapshot_backed.auto_emit import EvidenceAutoEmitter


def _make_snapshot(
    *,
    kind: str,
    payload: dict,
    symbol: str | None = None,
    snapshot_uuid=None,
) -> SimpleNamespace:
    return SimpleNamespace(
        snapshot_kind=kind,
        symbol=symbol,
        snapshot_uuid=snapshot_uuid or uuid4(),
        payload_json=payload,
    )


def _ok_quote_payload(symbol: str, sellable: float = 0.0) -> dict:
    return {
        "symbol": symbol,
        "quote": {
            "status": "ok",
            "last_price": 70_000.0,
            "best_bid": 69_900.0,
            "best_ask": 70_100.0,
            "spread": 200.0,
            "spread_bps": 28.57,
            "bid_depth": 500.0,
            "ask_depth": 600.0,
            "venue": "krx",
            "nxt_eligible": True,
            "session": "regular",
        },
    }


def _kis_portfolio_payload(*, ticker: str, sellable: float) -> dict:
    return {
        "primary_source": "kis",
        "holdings": [
            {
                "ticker": ticker,
                "quantity": sellable + 2.0,
                "sellable_quantity": sellable,
                "source": "kis",
                "market": "KR",
            }
        ],
        "reference_holdings": [],
        "count": 1,
        "market": "kr",
    }


def _candidate_payload(usefulness: str, actionable_count: int = 5) -> dict:
    return {
        "market": "kr",
        "actionable_count": actionable_count,
        "stale_count": 0,
        "usefulness": usefulness,
        "no_data_reason": None if usefulness == "useful" else "no fresh candidates",
    }


def _news_payload(symbol_matches: dict[str, int]) -> dict:
    return {
        "since": "2026-05-19T00:00:00+00:00",
        "count": sum(symbol_matches.values()),
        "citations": [],
        "symbol_matches": symbol_matches,
        "no_data_reason": None if any(symbol_matches.values()) else "no matches",
    }


# ---------------------------------------------------------------------------
# Empty / no-evidence baselines.
# ---------------------------------------------------------------------------
def test_empty_snapshots_emits_nothing():
    emitter = EvidenceAutoEmitter()
    items = emitter.propose(snapshots=[], request_market="kr", account_scope="kis_live")
    assert items == []


def test_no_evidence_combo_emits_nothing():
    emitter = EvidenceAutoEmitter()
    snapshots = [
        _make_snapshot(
            kind="portfolio",
            payload={
                "primary_source": "manual",
                "holdings": [{"ticker": "005930", "quantity": 5}],
                "reference_holdings": [],
            },
        )
    ]
    items = emitter.propose(
        snapshots=snapshots, request_market="kr", account_scope="kis_live"
    )
    assert items == []


# ---------------------------------------------------------------------------
# Sell candidates.
# ---------------------------------------------------------------------------
def test_sell_emitted_when_kis_held_and_quote_actionable():
    """KIS primary + sellable > 0 + quote.status='ok' → sell review item."""
    emitter = EvidenceAutoEmitter()
    snapshots = [
        _make_snapshot(
            kind="portfolio",
            payload=_kis_portfolio_payload(ticker="005930", sellable=8.0),
        ),
        _make_snapshot(
            kind="symbol",
            symbol="005930",
            payload=_ok_quote_payload("005930"),
        ),
    ]
    items = emitter.propose(
        snapshots=snapshots, request_market="kr", account_scope="kis_live"
    )
    sells = [i for i in items if i.item_kind == "action" and i.side == "sell"]
    assert len(sells) == 1
    sell = sells[0]
    assert sell.symbol == "005930"
    assert sell.operation == "review"
    assert sell.apply_policy == "requires_user_approval"
    assert sell.evidence_snapshot["proposer"] == "auto_emit/sell_from_held"
    assert sell.evidence_snapshot["sellable_quantity"] == 8.0
    assert sell.evidence_snapshot["snapshot_kind"] == "symbol"


def test_no_sell_when_portfolio_primary_source_is_manual():
    """Manual primary is never promoted — no sell candidate even if quote ok."""
    emitter = EvidenceAutoEmitter()
    snapshots = [
        _make_snapshot(
            kind="portfolio",
            payload={
                "primary_source": "manual",
                "holdings": [
                    {"ticker": "005930", "sellable_quantity": 10.0, "source": "manual"}
                ],
                "reference_holdings": [],
            },
        ),
        _make_snapshot(
            kind="symbol",
            symbol="005930",
            payload=_ok_quote_payload("005930"),
        ),
    ]
    items = emitter.propose(
        snapshots=snapshots, request_market="kr", account_scope="kis_live"
    )
    sells = [i for i in items if i.item_kind == "action" and i.side == "sell"]
    assert sells == []


def test_no_sell_when_quote_unavailable():
    """KIS held but quote.status='unavailable' → no sell candidate (fail-closed)."""
    emitter = EvidenceAutoEmitter()
    snapshots = [
        _make_snapshot(
            kind="portfolio",
            payload=_kis_portfolio_payload(ticker="005930", sellable=8.0),
        ),
        _make_snapshot(
            kind="symbol",
            symbol="005930",
            payload={
                "symbol": "005930",
                "quote": {
                    "status": "unavailable",
                    "unavailable_reason": "session_closed",
                },
            },
        ),
    ]
    items = emitter.propose(
        snapshots=snapshots, request_market="kr", account_scope="kis_live"
    )
    sells = [i for i in items if i.item_kind == "action" and i.side == "sell"]
    assert sells == []


def test_no_sell_when_sellable_quantity_zero():
    """KIS held but sellable_quantity == 0 → no sell candidate."""
    emitter = EvidenceAutoEmitter()
    snapshots = [
        _make_snapshot(
            kind="portfolio",
            payload=_kis_portfolio_payload(ticker="005930", sellable=0.0),
        ),
        _make_snapshot(
            kind="symbol",
            symbol="005930",
            payload=_ok_quote_payload("005930"),
        ),
    ]
    items = emitter.propose(
        snapshots=snapshots, request_market="kr", account_scope="kis_live"
    )
    sells = [i for i in items if i.item_kind == "action" and i.side == "sell"]
    assert sells == []


# ---------------------------------------------------------------------------
# Buy candidates.
# ---------------------------------------------------------------------------
def test_buy_emitted_when_candidate_useful_and_quote_ok_and_not_held():
    """Useful candidate universe + actionable quote + unheld → buy review item."""
    emitter = EvidenceAutoEmitter()
    snapshots = [
        _make_snapshot(
            kind="portfolio",
            payload=_kis_portfolio_payload(ticker="005930", sellable=8.0),
        ),
        _make_snapshot(
            kind="symbol",
            symbol="000660",
            payload=_ok_quote_payload("000660"),
        ),
        _make_snapshot(
            kind="candidate_universe",
            payload=_candidate_payload("useful", actionable_count=5),
        ),
    ]
    items = emitter.propose(
        snapshots=snapshots, request_market="kr", account_scope="kis_live"
    )
    buys = [i for i in items if i.item_kind == "action" and i.side == "buy"]
    assert len(buys) == 1
    buy = buys[0]
    assert buy.symbol == "000660"
    assert buy.operation == "review"
    assert buy.apply_policy == "requires_user_approval"
    assert buy.evidence_snapshot["proposer"] == "auto_emit/buy_from_candidate"
    assert buy.evidence_snapshot["candidate_usefulness"] == "useful"


def test_no_buy_when_candidate_universe_stale_only():
    """Candidate usefulness != 'useful' → no buy candidate even if quote ok."""
    emitter = EvidenceAutoEmitter()
    snapshots = [
        _make_snapshot(
            kind="symbol",
            symbol="000660",
            payload=_ok_quote_payload("000660"),
        ),
        _make_snapshot(
            kind="candidate_universe",
            payload=_candidate_payload("stale_only", actionable_count=0),
        ),
    ]
    items = emitter.propose(
        snapshots=snapshots, request_market="kr", account_scope="kis_live"
    )
    buys = [i for i in items if i.item_kind == "action" and i.side == "buy"]
    assert buys == []


def test_no_buy_when_already_held():
    """Useful candidate + held symbol → no buy candidate (already in position)."""
    emitter = EvidenceAutoEmitter()
    snapshots = [
        _make_snapshot(
            kind="portfolio",
            payload=_kis_portfolio_payload(ticker="005930", sellable=8.0),
        ),
        _make_snapshot(
            kind="symbol",
            symbol="005930",
            payload=_ok_quote_payload("005930"),
        ),
        _make_snapshot(
            kind="candidate_universe",
            payload=_candidate_payload("useful"),
        ),
    ]
    items = emitter.propose(
        snapshots=snapshots, request_market="kr", account_scope="kis_live"
    )
    buys = [i for i in items if i.item_kind == "action" and i.side == "buy"]
    assert buys == []


def test_buy_respects_cap():
    """Max-buy-candidates bound is honoured."""
    snapshots = [
        _make_snapshot(
            kind="candidate_universe",
            payload=_candidate_payload("useful"),
        ),
    ]
    for i in range(5):
        sym = f"00500{i}"
        snapshots.append(
            _make_snapshot(
                kind="symbol",
                symbol=sym,
                payload=_ok_quote_payload(sym),
            )
        )
    emitter = EvidenceAutoEmitter(max_buy_candidates=3)
    items = emitter.propose(
        snapshots=snapshots, request_market="kr", account_scope="kis_live"
    )
    buys = [i for i in items if i.item_kind == "action" and i.side == "buy"]
    assert len(buys) == 3


# ---------------------------------------------------------------------------
# Watch candidates.
# ---------------------------------------------------------------------------
def test_watch_emitted_when_news_active_but_no_quote_evidence():
    """News matches without quote evidence → watch review item."""
    emitter = EvidenceAutoEmitter()
    snapshots = [
        _make_snapshot(
            kind="news",
            payload=_news_payload({"000660": 3}),
        ),
    ]
    items = emitter.propose(
        snapshots=snapshots, request_market="kr", account_scope="kis_live"
    )
    watches = [i for i in items if i.item_kind == "watch"]
    assert len(watches) == 1
    watch = watches[0]
    assert watch.symbol == "000660"
    assert watch.operation == "review"
    assert watch.apply_policy == "requires_user_approval"
    assert watch.evidence_snapshot["proposer"] == "auto_emit/watch_from_news"
    assert watch.evidence_snapshot["news_match_count"] == 3


def test_no_duplicate_watch_when_already_proposed_as_buy():
    """A symbol already proposed for buy must not also surface as watch."""
    emitter = EvidenceAutoEmitter()
    snapshots = [
        _make_snapshot(
            kind="symbol",
            symbol="000660",
            payload=_ok_quote_payload("000660"),
        ),
        _make_snapshot(
            kind="candidate_universe",
            payload=_candidate_payload("useful"),
        ),
        _make_snapshot(
            kind="news",
            payload=_news_payload({"000660": 4}),
        ),
    ]
    items = emitter.propose(
        snapshots=snapshots, request_market="kr", account_scope="kis_live"
    )
    by_symbol_kind = [(i.symbol, i.item_kind) for i in items]
    # Buy proposal should win; watch on the same symbol must not also fire.
    assert ("000660", "action") in by_symbol_kind
    assert ("000660", "watch") not in by_symbol_kind


# ---------------------------------------------------------------------------
# Mutation safety — static guard.
# ---------------------------------------------------------------------------
def test_auto_emit_module_does_not_import_mutation_paths():
    import importlib
    import sys

    forbidden = (
        "kis_trading_service",
        "investment_reports.watch_activation",
        "alpaca_paper_ledger_service",
        "upbit.client",
        "place_order",
        "submit_order",
        "cancel_order",
        "modify_order",
    )
    module_name = "app.services.action_report.snapshot_backed.auto_emit"
    importlib.import_module(module_name)
    module = sys.modules[module_name]
    source = open(module.__file__, encoding="utf-8").read()  # type: ignore[arg-type]
    for symbol in forbidden:
        assert symbol not in source, (
            f"auto_emit unexpectedly references {symbol!r} — must remain read-only"
        )


# ---------------------------------------------------------------------------
# Apply-policy + evidence provenance — invariant across all proposals.
# ---------------------------------------------------------------------------
def test_all_emitted_items_are_review_and_require_user_approval():
    emitter = EvidenceAutoEmitter()
    snapshots = [
        _make_snapshot(
            kind="portfolio",
            payload=_kis_portfolio_payload(ticker="005930", sellable=8.0),
        ),
        _make_snapshot(
            kind="symbol",
            symbol="005930",
            payload=_ok_quote_payload("005930"),
        ),
        _make_snapshot(
            kind="symbol",
            symbol="000660",
            payload=_ok_quote_payload("000660"),
        ),
        _make_snapshot(
            kind="candidate_universe",
            payload=_candidate_payload("useful"),
        ),
        _make_snapshot(kind="news", payload=_news_payload({"035420": 2})),
    ]
    items = emitter.propose(
        snapshots=snapshots, request_market="kr", account_scope="kis_live"
    )
    assert items, "test setup should produce at least one proposal"
    for item in items:
        assert item.operation == "review", item
        assert item.apply_policy == "requires_user_approval", item
        assert item.evidence_snapshot is not None
        assert item.evidence_snapshot.get("snapshot_uuid")
        assert item.evidence_snapshot.get("proposer", "").startswith("auto_emit/")


def test_existing_sell_item_is_stamped_with_verdict_and_bucket() -> None:
    # Default mode (no intraday_floor): existing sell candidate now carries the
    # ActionPacket sub-verdict + decision_bucket so it projects.
    snapshots = [
        _make_snapshot(kind="portfolio",
                       payload=_kis_portfolio_payload(ticker="005930", sellable=5.0)),
        _make_snapshot(kind="symbol", symbol="005930",
                       payload=_ok_quote_payload("005930")),
    ]
    items = EvidenceAutoEmitter().propose(
        snapshots=snapshots, request_market="kr", account_scope="kis_live"
    )
    sell = next(i for i in items if i.symbol == "005930" and i.side == "sell")
    assert sell.evidence_snapshot["action_verdict"] == "sell_review"
    assert sell.decision_bucket == "open_action"


def test_intraday_floor_classifies_every_held_symbol() -> None:
    # Held symbol with NO actionable quote -> data_gap item (would be skipped
    # entirely in default mode).
    snapshots = [
        _make_snapshot(kind="portfolio",
                       payload=_kis_portfolio_payload(ticker="005930", sellable=0.0)),
    ]
    items = EvidenceAutoEmitter(intraday_floor=True).propose(
        snapshots=snapshots, request_market="kr", account_scope="kis_live"
    )
    held = next(i for i in items if i.symbol == "005930")
    assert held.evidence_snapshot["action_verdict"] == "data_gap"
    assert held.decision_bucket == "deferred_no_action"


def test_intraday_floor_emits_no_new_buy_marker_when_stale_only() -> None:
    snapshots = [
        _make_snapshot(kind="portfolio",
                       payload=_kis_portfolio_payload(ticker="005930", sellable=0.0)),
        _make_snapshot(kind="candidate_universe",
                       payload=_candidate_payload("stale_only")),
    ]
    items = EvidenceAutoEmitter(intraday_floor=True).propose(
        snapshots=snapshots, request_market="kr", account_scope="kis_live"
    )
    marker = next(
        i for i in items
        if i.evidence_snapshot.get("action_verdict") == "no_new_buy_candidates"
    )
    assert marker.symbol is None
    assert marker.decision_bucket == "new_buy_candidate"
    assert marker.item_kind == "risk"


def test_default_mode_emits_no_marker_and_no_keep_items() -> None:
    # Backwards-compat: without intraday_floor, behaviour is unchanged.
    snapshots = [
        _make_snapshot(kind="portfolio",
                       payload=_kis_portfolio_payload(ticker="005930", sellable=0.0)),
        _make_snapshot(kind="candidate_universe",
                       payload=_candidate_payload("stale_only")),
    ]
    items = EvidenceAutoEmitter().propose(
        snapshots=snapshots, request_market="kr", account_scope="kis_live"
    )
    verdicts = {i.evidence_snapshot.get("action_verdict") for i in items}
    assert "no_new_buy_candidates" not in verdicts
    assert "keep" not in verdicts


def test_intraday_floor_never_classifies_reference_holdings() -> None:
    # Toss/manual rows live in reference_holdings; primary_source != 'kis'
    # means _held_kis_symbols returns {} -> no held_actions promoted.
    payload = {
        "primary_source": "manual",
        "holdings": [{"ticker": "AAPL", "sellable_quantity": 3, "source": "manual"}],
        "reference_holdings": [{"ticker": "AAPL", "source": "toss"}],
        "count": 1, "market": "us",
    }
    snapshots = [_make_snapshot(kind="portfolio", payload=payload)]
    items = EvidenceAutoEmitter(intraday_floor=True).propose(
        snapshots=snapshots, request_market="us", account_scope="kis_live"
    )
    assert all(i.symbol != "AAPL" for i in items if i.evidence_snapshot.get("action_verdict") in
               {"sell_review", "keep", "no_add"})


def test_intraday_floor_user_id_missing_portfolio_yields_no_held_items() -> None:
    # primary_source='none' (user_id missing path) -> no holdings to classify;
    # the generator-level floor (Task 5) supplies the data_gap item instead.
    payload = {
        "primary_source": "none", "holdings": [], "reference_holdings": [],
        "count": 0, "market": "kr",
    }
    snapshots = [_make_snapshot(kind="portfolio", payload=payload)]
    items = EvidenceAutoEmitter(intraday_floor=True).propose(
        snapshots=snapshots, request_market="kr", account_scope="kis_live"
    )
    held_verdicts = {"sell_review", "keep", "no_add", "data_gap"}
    assert not [i for i in items if i.symbol and
                i.evidence_snapshot.get("action_verdict") in held_verdicts]

