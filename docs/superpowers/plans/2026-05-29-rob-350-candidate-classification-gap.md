# ROB-350 Candidate Classification Gap — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop silently dropping ranked new-buy candidates that fail the actionable-quote gate; classify every non-held screener candidate as `buy_review` / `watch_only` / `data_gap` with a reason, surfaced through the ActionPacket and UI.

**Architecture:** Add a pure `classify_candidate_symbol()` helper to `action_verdict.py` (symmetric with `classify_held_symbol`), restructure the `EvidenceAutoEmitter` buy block into a single always-on classification loop, thread a `reject_or_wait_reason` field through the schema/projection, and render it in the ActionPacket UI. No migration (classification lives in `evidence_snapshot` JSON). Every emitted item stays `operation="review"` + `apply_policy="requires_user_approval"`.

**Tech Stack:** Python 3.13 / Pydantic v2 / pytest (`uv run --all-groups`), React + TypeScript / vitest (`--pool=forks`).

Design doc: `docs/superpowers/specs/2026-05-29-rob-350-candidate-classification-gap-design.md`

---

## File Structure

- `app/services/action_report/snapshot_backed/action_verdict.py` — add `classify_candidate_symbol` (pure helper).
- `app/services/action_report/snapshot_backed/auto_emit.py` — replace buy block with one classification loop + a `_candidate_item` factory.
- `app/schemas/investment_reports.py` — add `ActionPacketEntry.reject_or_wait_reason`.
- `app/services/investment_reports/action_packet.py` — read `reject_or_wait_reason`, rank-sort `risk_reviews`.
- `frontend/invest/src/types/investmentReports.ts` — add `rejectOrWaitReason`.
- `frontend/invest/src/api/investmentReports.ts` — normalize `reject_or_wait_reason`.
- `frontend/invest/src/components/investment-reports/ActionPacketView.tsx` — render reason chip.
- Tests: `tests/services/action_report/snapshot_backed/test_action_verdict.py` (new or existing), `tests/test_auto_emit_candidate_citation.py`, `tests/services/investment_reports/test_action_packet.py`, `frontend/invest/src/__tests__/ActionPacketView.test.tsx`, `frontend/invest/src/__tests__/investmentReportsActionPacket.test.ts`.

---

## Task 1: `classify_candidate_symbol` pure helper

**Files:**
- Modify: `app/services/action_report/snapshot_backed/action_verdict.py`
- Test: `tests/services/action_report/snapshot_backed/test_action_verdict.py`

- [ ] **Step 1: Write the failing test**

Create/append `tests/services/action_report/snapshot_backed/test_action_verdict.py`:

```python
import pytest

from app.services.action_report.snapshot_backed.action_verdict import (
    classify_candidate_symbol,
)

_OK_QUOTE = {
    "status": "ok",
    "best_bid": 100,
    "best_ask": 101,
    "bid_depth": 5,
    "ask_depth": 5,
    "spread_bps": 10,
}
_DEAD_QUOTE = {"status": "ok", "best_bid": 0, "best_ask": 0, "bid_depth": 0, "ask_depth": 0}


@pytest.mark.parametrize(
    "quote, present, useful, expected",
    [
        (None, False, True, "data_gap"),            # no snapshot at all
        (_DEAD_QUOTE, True, True, "watch_only"),     # 저유동성
        (_OK_QUOTE, True, False, "watch_only"),      # screener stale
        (_OK_QUOTE, True, True, "buy_review"),       # actionable + useful
    ],
)
def test_classify_candidate_symbol(quote, present, useful, expected):
    assert (
        classify_candidate_symbol(
            quote, universe_useful=useful, quote_snapshot_present=present
        )
        == expected
    )


def test_classify_candidate_symbol_never_rejects():
    # Honest-verdict only: rejected / limit_wait are Hermes-only.
    for present in (True, False):
        for useful in (True, False):
            v = classify_candidate_symbol(
                _DEAD_QUOTE, universe_useful=useful, quote_snapshot_present=present
            )
            assert v in {"data_gap", "watch_only", "buy_review"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --all-groups pytest tests/services/action_report/snapshot_backed/test_action_verdict.py -v`
Expected: FAIL with `ImportError: cannot import name 'classify_candidate_symbol'`

- [ ] **Step 3: Write minimal implementation**

In `app/services/action_report/snapshot_backed/action_verdict.py`, add after `classify_held_symbol` (reuse the existing module-level `_quote_is_actionable`):

```python
def classify_candidate_symbol(
    quote: dict[str, Any] | None,
    *,
    universe_useful: bool,
    quote_snapshot_present: bool,
) -> str:
    """Deterministic verdict for ONE non-held screener candidate.

    Honest-verdict only (mirrors ``classify_held_symbol``): never returns the
    directional ``rejected`` / ``limit_wait`` — those are Hermes-only.

    Order:
      1. no symbol/quote snapshot at all   -> ``data_gap``   (호가 근거 부족)
      2. quote present but not actionable  -> ``watch_only`` (저유동성)
      3. quote actionable, universe stale  -> ``watch_only`` (스크리너 stale)
      4. quote actionable, universe useful -> ``buy_review``
    """
    if not quote_snapshot_present:
        return "data_gap"
    if not _quote_is_actionable(quote):
        return "watch_only"
    if not universe_useful:
        return "watch_only"
    return "buy_review"
```

Note: `_quote_is_actionable` already guards `not isinstance(quote, dict)` → returns `False`, so `quote=None` with `present=True` is safe.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --all-groups pytest tests/services/action_report/snapshot_backed/test_action_verdict.py -v`
Expected: PASS (5 cases)

- [ ] **Step 5: Commit**

```bash
git add app/services/action_report/snapshot_backed/action_verdict.py tests/services/action_report/snapshot_backed/test_action_verdict.py
git commit -m "feat(ROB-350): add classify_candidate_symbol honest-verdict helper

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Restructure auto_emit into a single classification loop

**Files:**
- Modify: `app/services/action_report/snapshot_backed/auto_emit.py:266-329` (the `if candidate_actionable:` buy block)
- Test: `tests/test_auto_emit_candidate_citation.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_auto_emit_candidate_citation.py` (reuses the module's `_Snap` / `_OK_QUOTE`):

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --all-groups pytest tests/test_auto_emit_candidate_citation.py -v`
Expected: the 5 new tests FAIL (candidates dropped → `cand` empty; `KeyError`/assertion failures).

- [ ] **Step 3: Add the `_candidate_item` factory**

In `app/services/action_report/snapshot_backed/auto_emit.py`, add a module-level factory after `_candidate_sort_key` (before the class). It builds one review-only item for any candidate verdict:

```python
def _candidate_item(
    *,
    symbol_snapshot: Any,
    candidate_snapshot: Any,
    sym: str,
    cand: dict[str, Any],
    quote: dict[str, Any] | None,
    verdict: str,
    priority: int,
    reject_or_wait_reason: str | None,
    candidate_usefulness: str | None,
    news_match_count: int,
) -> IngestReportItem:
    is_buy = verdict == "buy_review"
    is_gap = verdict == "data_gap"
    q = quote if isinstance(quote, dict) else {}
    extra: dict[str, Any] = {
        "candidate_snapshot_uuid": _snapshot_uuid(candidate_snapshot),
        "candidate_usefulness": candidate_usefulness,
        "candidate_rank": priority,
        "candidate_score": cand.get("score"),
        "candidate_reasons": cand.get("reasons"),
        "candidate_source": cand.get("source"),
        "news_matches": news_match_count,
        "quote_status": q.get("status") if quote is not None else "no_snapshot",
        "best_bid": q.get("best_bid"),
        "best_ask": q.get("best_ask"),
        "spread_bps": q.get("spread_bps"),
        "proposer": f"auto_emit/candidate_{verdict}",
    }
    if reject_or_wait_reason is not None:
        extra["reject_or_wait_reason"] = reject_or_wait_reason

    if is_buy:
        rationale = (
            f"신규 매수 검토 {priority}순위 — {sym} "
            f"(candidate {candidate_usefulness}, score {cand.get('score')}, "
            f"quote best_bid {q.get('best_bid')}, spread_bps {q.get('spread_bps')})"
        )
    elif is_gap:
        rationale = f"신규 후보 판단 보류 — {sym} (호가 스냅샷 없음)"
    elif reject_or_wait_reason == "low_liquidity":
        rationale = (
            f"신규 후보 관망 {priority}순위 — {sym} "
            f"(저유동성: spread_bps {q.get('spread_bps')})"
        )
    elif reject_or_wait_reason == "beyond_candidate_budget":
        rationale = f"신규 후보 관망 {priority}순위 — {sym} (후보 예산 초과)"
    else:  # screener_stale
        rationale = f"신규 후보 관망 {priority}순위 — {sym} (스크리너 stale)"

    return IngestReportItem(
        client_item_key=(f"auto-buy-{sym}" if is_buy else f"auto-cand-{verdict}-{sym}"),
        item_kind="action" if is_buy else "risk",
        symbol=sym,
        side="buy" if is_buy else None,
        intent=(
            "buy_review"
            if is_buy
            else "risk_review"
            if is_gap
            else "trend_recovery_review"
        ),
        priority=priority,
        rationale=rationale,
        operation="review",
        apply_policy="requires_user_approval",
        evidence_snapshot=_make_evidence(symbol_snapshot, extra=extra),
    )
```

- [ ] **Step 4: Import the helper and replace the buy block**

At the top of `auto_emit.py`, extend the existing import:

```python
from app.services.action_report.snapshot_backed.action_verdict import (
    VERDICT_TO_BUCKET,
    classify_candidate_symbol,
    classify_held_symbol,
)
```

Replace the entire block from the `# Buy candidates —` comment through `buy_emitted += 1` (currently `auto_emit.py:266-329`) with:

```python
        # Candidate classification — every non-held screener candidate gets
        # exactly ONE honest verdict (buy_review / watch_only / data_gap). No
        # candidate is silently dropped (ROB-350). Always-on whenever a
        # candidate_universe snapshot is present, independent of intraday_floor.
        buy_emitted = 0
        for cand in sorted(candidate_order, key=_candidate_sort_key):
            sym = cand.get("symbol")
            if not isinstance(sym, str) or sym in held:
                continue  # held names handled by held_and_trending below
            symbol_pair = symbol_quotes.get(sym)
            quote = symbol_pair[1] if symbol_pair else None
            verdict = classify_candidate_symbol(
                quote,
                universe_useful=candidate_actionable,
                quote_snapshot_present=symbol_pair is not None,
            )
            reject_or_wait_reason: str | None = None
            if verdict == "data_gap":
                reject_or_wait_reason = "quote_missing"
            elif verdict == "watch_only":
                reject_or_wait_reason = (
                    "low_liquidity"
                    if symbol_pair is not None and not _quote_is_actionable(quote)
                    else "screener_stale"
                )
            elif verdict == "buy_review":
                if buy_emitted >= self._max_buy_candidates:
                    verdict = "watch_only"
                    reject_or_wait_reason = "beyond_candidate_budget"
                else:
                    buy_emitted += 1

            candidate_rank = _candidate_rank(cand)
            priority = candidate_rank if candidate_rank is not None else buy_emitted
            items.append(
                _stamp(
                    _candidate_item(
                        symbol_snapshot=(
                            symbol_pair[0] if symbol_pair else candidate_snapshot
                        ),
                        candidate_snapshot=candidate_snapshot,
                        sym=sym,
                        cand=cand,
                        quote=quote,
                        verdict=verdict,
                        priority=priority,
                        reject_or_wait_reason=reject_or_wait_reason,
                        candidate_usefulness=candidate_usefulness,
                        news_match_count=news_matches.get(sym, 0),
                    ),
                    verdict,
                )
            )
```

Note: `_quote_is_actionable(quote)` with `quote=None` returns `False` safely. The old `if candidate_actionable:` gate is removed — `candidate_actionable` is now passed as `universe_useful` so stale universes still iterate and classify.

- [ ] **Step 5: Run the new + existing auto_emit tests**

Run: `uv run --all-groups pytest tests/test_auto_emit_candidate_citation.py -v`
Expected: PASS — the 5 new tests plus the pre-existing `test_buy_item_cites_candidate_evidence` and `test_buy_candidates_follow_candidate_rank_and_limit` (buys still keyed `auto-buy-`, ordered by rank).

- [ ] **Step 6: Run the generator regression test**

Run: `uv run --all-groups pytest tests/services/action_report/snapshot_backed/test_generator.py -v`
Expected: PASS — `test_auto_emit_from_evidence_respects_request_candidate_limit` still sees exactly 2 `auto-buy-` items (the 3rd candidate now becomes a `watch_only` row, which the test's `auto-buy-` filter ignores), and the intraday-floor / no-new-buy tests stay green.

- [ ] **Step 7: Commit**

```bash
git add app/services/action_report/snapshot_backed/auto_emit.py tests/test_auto_emit_candidate_citation.py
git commit -m "feat(ROB-350): classify non-buy candidates instead of dropping them

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Surface `reject_or_wait_reason` through schema + ActionPacket projection

**Files:**
- Modify: `app/schemas/investment_reports.py` (`ActionPacketEntry`)
- Modify: `app/services/investment_reports/action_packet.py`
- Test: `tests/services/investment_reports/test_action_packet.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/services/investment_reports/test_action_packet.py`:

```python
def test_data_gap_candidate_becomes_data_gap_entry_with_symbol() -> None:
    gap = _item(
        verdict="data_gap",
        decision_bucket="deferred_no_action",
        side=None,
        intent="risk_review",
        symbol="000660",
        evidence_extra={"reject_or_wait_reason": "quote_missing"},
    )
    packet = build_action_packet([gap], diagnostics=None)
    sources = [g.source for g in packet.data_gaps_for_next_cycle]
    assert "000660" in sources


def test_watch_only_candidate_exposes_reject_reason_and_rank_sort() -> None:
    low = _item(
        verdict="watch_only",
        decision_bucket="risk_watch",
        side=None,
        intent="trend_recovery_review",
        symbol="005930",
        priority=2,
        evidence_extra={"candidate_rank": 2, "reject_or_wait_reason": "low_liquidity"},
    )
    high = _item(
        verdict="watch_only",
        decision_bucket="risk_watch",
        side=None,
        intent="trend_recovery_review",
        symbol="000660",
        priority=1,
        evidence_extra={"candidate_rank": 1, "reject_or_wait_reason": "screener_stale"},
    )
    packet = build_action_packet([low, high], diagnostics=None)
    assert [e.symbol for e in packet.risk_reviews] == ["000660", "005930"]
    assert packet.risk_reviews[0].reject_or_wait_reason == "screener_stale"
    assert packet.risk_reviews[1].reject_or_wait_reason == "low_liquidity"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --all-groups pytest tests/services/investment_reports/test_action_packet.py -v -k "reject or data_gap_candidate"`
Expected: FAIL — `AttributeError: 'ActionPacketEntry' object has no attribute 'reject_or_wait_reason'` and risk-sort assertion failing (risk_reviews currently unsorted).

- [ ] **Step 3: Add the schema field**

In `app/schemas/investment_reports.py`, add to `ActionPacketEntry` (after `rank`):

```python
    reject_or_wait_reason: str | None = None
```

- [ ] **Step 4: Read the reason and rank-sort risk_reviews**

In `app/services/investment_reports/action_packet.py`, update `_entry` to populate the field:

```python
def _entry(item: InvestmentReportItemResponse, verdict: str) -> ActionPacketEntry:
    rank = _entry_rank(item)
    evidence = item.evidence_snapshot or {}
    reason = (
        evidence.get("reject_or_wait_reason")
        if isinstance(evidence, Mapping)
        else None
    )
    return ActionPacketEntry(
        verdict=verdict,  # type: ignore[arg-type]
        symbol=item.symbol,
        side=item.side,
        rationale=item.rationale,
        item_uuid=item.item_uuid,
        priority=item.priority if item.priority > 0 else None,
        rank=rank,
        reject_or_wait_reason=str(reason) if isinstance(reason, str) else None,
        evidence_snapshot=dict(item.evidence_snapshot or {}),
    )
```

Then, in `build_action_packet`, next to the existing `new_buy.sort(...)` line, add a matching sort for risk reviews:

```python
    new_buy.sort(key=lambda entry: entry.rank or entry.priority or 1_000_000)
    risk.sort(key=lambda entry: entry.rank or entry.priority or 1_000_000)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run --all-groups pytest tests/services/investment_reports/test_action_packet.py -v`
Expected: PASS — new tests plus existing `test_held_and_new_and_risk_are_grouped_by_verdict` / `test_new_buy_candidates_are_sorted_and_expose_rank_priority`.

- [ ] **Step 6: Commit**

```bash
git add app/schemas/investment_reports.py app/services/investment_reports/action_packet.py tests/services/investment_reports/test_action_packet.py
git commit -m "feat(ROB-350): expose reject_or_wait_reason + rank-sort risk reviews

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Frontend — render the reject/wait reason

**Files:**
- Modify: `frontend/invest/src/types/investmentReports.ts` (`ActionPacketEntry`)
- Modify: `frontend/invest/src/api/investmentReports.ts` (`normalizeActionPacketEntry`)
- Modify: `frontend/invest/src/components/investment-reports/ActionPacketView.tsx` (`EntryRow`)
- Test: `frontend/invest/src/__tests__/investmentReportsActionPacket.test.ts`, `frontend/invest/src/__tests__/ActionPacketView.test.tsx`

- [ ] **Step 1: Write the failing tests**

In `frontend/invest/src/__tests__/investmentReportsActionPacket.test.ts`, extend the `risk_reviews` fixture object (the one with `verdict: "watch_only"`) to include `reject_or_wait_reason` and assert it normalizes:

```typescript
      risk_reviews: [{ verdict: "watch_only", symbol: "035720", rationale: "관망",
                       item_uuid: "i3", reject_or_wait_reason: "low_liquidity",
                       evidence_snapshot: {} }],
```

and add an assertion in the same test after the existing `riskReviews[0]!.verdict` check:

```typescript
    expect(packet!.riskReviews[0]!.rejectOrWaitReason).toBe("low_liquidity");
```

In `frontend/invest/src/__tests__/ActionPacketView.test.tsx`, add a test:

```typescript
  it("renders the reject/wait reason on risk rows", () => {
    render(<ActionPacketView packet={makePacket({
      riskReviews: [
        { verdict: "watch_only", symbol: "035720", side: null, rationale: "관망",
          itemUuid: "r1", evidenceSnapshot: {}, rejectOrWaitReason: "low_liquidity" },
      ],
    })} />);
    expect(screen.getByText("low_liquidity")).toBeInTheDocument();
  });
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd frontend/invest && npx vitest run --pool=forks src/__tests__/investmentReportsActionPacket.test.ts src/__tests__/ActionPacketView.test.tsx`
Expected: FAIL — `rejectOrWaitReason` is `undefined` (type/normalizer missing) and the chip text is not in the document.

- [ ] **Step 3: Add the type field**

In `frontend/invest/src/types/investmentReports.ts`, add to `ActionPacketEntry` (after `rank`):

```typescript
  rejectOrWaitReason?: string | null;
```

- [ ] **Step 4: Normalize the snake_case field**

In `frontend/invest/src/api/investmentReports.ts`, inside `normalizeActionPacketEntry`, add (after the `rank:` line):

```typescript
    rejectOrWaitReason: asOptionalString(obj.reject_or_wait_reason),
```

- [ ] **Step 5: Render the chip**

In `frontend/invest/src/components/investment-reports/ActionPacketView.tsx`, inside `EntryRow`, after the `priority` Pill block, add:

```tsx
        {entry.rejectOrWaitReason ? (
          <Pill tone="muted" size="sm">{entry.rejectOrWaitReason}</Pill>
        ) : null}
```

If `tone="muted"` is not a valid `Pill` tone in this codebase, use the same tone already used for neutral chips in this file (check the existing `Pill` usages at the top of `ActionPacketView.tsx`); fall back to a plain `<span>` styled like the `rank` span if no neutral tone exists.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd frontend/invest && npx vitest run --pool=forks src/__tests__/investmentReportsActionPacket.test.ts src/__tests__/ActionPacketView.test.tsx`
Expected: PASS — new assertions plus the existing rank/priority and data-gap rendering tests.

- [ ] **Step 7: Commit**

```bash
git add frontend/invest/src/types/investmentReports.ts frontend/invest/src/api/investmentReports.ts frontend/invest/src/components/investment-reports/ActionPacketView.tsx frontend/invest/src/__tests__/investmentReportsActionPacket.test.ts frontend/invest/src/__tests__/ActionPacketView.test.tsx
git commit -m "feat(ROB-350): render candidate reject/wait reason in ActionPacket UI

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Full-suite verification + no-mutation evidence

**Files:** none (verification only)

- [ ] **Step 1: Ruff**

Run: `uv run ruff check app/ tests/`
Expected: `All checks passed!`

- [ ] **Step 2: Backend touched modules**

Run:
```bash
uv run --all-groups pytest \
  tests/services/action_report/snapshot_backed/test_action_verdict.py \
  tests/test_auto_emit_candidate_citation.py \
  tests/services/action_report/snapshot_backed/test_generator.py \
  tests/services/investment_reports/test_action_packet.py -q
```
Expected: all PASS.

- [ ] **Step 3: Static import guard (no broker/order mutation)**

Run: `uv run --all-groups pytest -k "import_guard or no_mutation" tests/ -q`
Expected: PASS — confirms `app/services/action_report/snapshot_backed/` reaches no broker/order/watch/order-intent mutation path. (If the guard test name differs, find it with `grep -rln "import.*guard" tests/services/action_report/`.)

- [ ] **Step 4: Frontend suite for touched files**

Run: `cd frontend/invest && npx vitest run --pool=forks src/__tests__/ActionPacketView.test.tsx src/__tests__/investmentReportsActionPacket.test.ts`
Expected: PASS.

- [ ] **Step 5: Record handoff evidence**

In the PR description, record (per ROB-350 handoff requirements): branch name, changed files, the exact test commands + results above, "no migration", "no feature flag/env" (additive evidence_snapshot field only, default-safe), and "no broker/order/watch/order-intent mutation — every emitted item is operation=review + requires_user_approval, static import guard green". Note remaining ROB-350 scope still open: budget-generous candidate count tuning (§2) and Toss/Naver supplementary evidence (§3).

---

## Self-Review notes

- **Spec coverage:** Q1 taxonomy → Task 1/2 (`data_gap`/`watch_only`, no `rejected`). Q2 always-on → Task 2 (gate removed, `candidate_actionable` passed as `universe_useful`). Reason surfacing → Task 3/4. No-mutation invariant → Task 5 step 3. Out-of-scope (§2 budget tuning, §3 Toss/Naver) explicitly deferred in Task 5 step 5.
- **Type consistency:** helper name `classify_candidate_symbol` and field name `reject_or_wait_reason` (backend) / `rejectOrWaitReason` (frontend) are used identically across all tasks. `_candidate_item` factory and its keyword args match the call site in Task 2 step 4.
- **Edge case:** `priority = candidate_rank if not None else buy_emitted` — collector always stamps `rank`, so the fallback is effectively dead for the standard path; kept for snapshots lacking rank.
