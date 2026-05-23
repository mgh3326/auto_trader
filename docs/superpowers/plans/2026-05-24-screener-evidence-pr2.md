# Screener Evidence → `/invest/reports` (PR2: G2 held cross-check) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans or subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Cross-check portfolio holdings against the screener `candidate_universe` so reports separate "held & also trending" from "new candidates," and the auto-emitter surfaces held-and-trending names as review-only watch signals.

**Architecture:** The evidence builder stays held-agnostic (pure). The held join happens where portfolio data is available: the `CandidateUniverseStage` (which sees both `portfolio` and `candidate_universe` snapshots) and `EvidenceAutoEmitter`. Informational awareness only — never sell-eligibility, so the held set is the union of `holdings` + `reference_holdings` (respects "Toss reference never merged into sellable").

**Spec:** `docs/superpowers/specs/2026-05-24-screener-evidence-for-reports-design.md` §6.

**Conventions:** `uv run pytest ... -v`; commit trailer `Co-Authored-By: Paperclip <noreply@paperclip.ing>`; branch `rob-304-pr2` (off merged `main`).

---

## File Structure

**Modify:**
- `app/services/investment_stages/stages/candidate_universe.py` — held normalization + held/new split + portfolio citation.
- `app/services/action_report/snapshot_backed/auto_emit.py` — held-and-trending watch items.

**Test:**
- `tests/services/investment_stages/test_candidate_universe_stage_evidence.py` — add held cross-check cases.
- `tests/test_auto_emit_candidate_citation.py` — add held-and-trending case.

---

## Task 1: Stage held cross-check

Held matching normalizes the crypto `KRW-` prefix so candidate `KRW-BTC` matches held ticker `BTC` or `KRW-BTC`. Held = union of `holdings` + `reference_holdings` tickers across `portfolio` snapshots (awareness only, never sellability). When held names appear in the top candidates, key_points tag them `[보유·추세]` vs `[신규]`, the summary notes held-and-trending symbols, and the portfolio snapshot is cited.

- [ ] **Step 1: Write the failing tests** (append to `tests/services/investment_stages/test_candidate_universe_stage_evidence.py`)

```python
def _ctx_with_portfolio(candidate_payload, portfolio_payload):
    return StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={
            "candidate_universe": [_Snap(candidate_payload)],
            "portfolio": [_Snap(portfolio_payload)],
        },
        bundle_metadata={},
    )


@pytest.mark.asyncio
async def test_stage_tags_held_and_trending_candidate():
    candidate_payload = {
        "freshness_status": "fresh",
        "source_coverage": {"kis": 2},
        "candidates": [
            {"symbol": "005930", "score": 8.0, "reasons": ["단기 상승 모멘텀 후보"],
             "source": "kis"},
            {"symbol": "000660", "score": 7.5, "reasons": ["단기 상승 모멘텀 후보"],
             "source": "kis"},
        ],
        "missing_data": None,
    }
    portfolio_payload = {"primary_source": "kis",
                         "holdings": [{"ticker": "005930"}],
                         "reference_holdings": []}
    out = await CandidateUniverseStage().run(
        _ctx_with_portfolio(candidate_payload, portfolio_payload)
    )
    held_lines = [kp for kp in out.key_points if "보유·추세" in kp]
    assert any("005930" in kp for kp in held_lines)
    assert any("000660" in kp and "신규" in kp for kp in out.key_points)
    assert "005930" in (out.summary or "")
    # Portfolio snapshot is cited when the held cross-check is applied.
    assert any(c.snapshot_kind == "portfolio" for c in out.cited_snapshots)


@pytest.mark.asyncio
async def test_stage_held_crosscheck_normalizes_crypto_prefix():
    candidate_payload = {
        "freshness_status": "fresh",
        "source_coverage": {"tvscreener_upbit": 1},
        "candidates": [
            {"symbol": "KRW-BTC", "score": 9.0, "reasons": ["단기 상승 모멘텀 후보"],
             "source": "tvscreener_upbit"},
        ],
        "missing_data": None,
    }
    portfolio_payload = {"primary_source": "manual",
                         "holdings": [{"ticker": "BTC"}],
                         "reference_holdings": []}
    out = await CandidateUniverseStage().run(
        _ctx_with_portfolio(candidate_payload, portfolio_payload)
    )
    assert any("보유·추세" in kp and "KRW-BTC" in kp for kp in out.key_points)


@pytest.mark.asyncio
async def test_stage_no_portfolio_marks_all_new():
    candidate_payload = {
        "freshness_status": "fresh",
        "source_coverage": {"kis": 1},
        "candidates": [
            {"symbol": "005930", "score": 8.0, "reasons": ["x"], "source": "kis"},
        ],
        "missing_data": None,
    }
    out = await CandidateUniverseStage().run(_ctx(candidate_payload))
    assert all("보유·추세" not in kp for kp in out.key_points)
    assert not any(c.snapshot_kind == "portfolio" for c in out.cited_snapshots)
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/services/investment_stages/test_candidate_universe_stage_evidence.py -v`
Expected: the three new tests FAIL (no held tagging, no portfolio citation).

- [ ] **Step 3: Implement** — edit `app/services/investment_stages/stages/candidate_universe.py`.

Add helpers above the class:

```python
def _norm_symbol(value: str) -> str:
    s = (value or "").strip().upper()
    return s[4:] if s.startswith("KRW-") else s


def _held_symbols(context: StageContext) -> tuple[set[str], object | None]:
    """Union of held + reference holdings (awareness only, never sellability).
    Returns the normalized symbol set and the portfolio snapshot used (for
    citation), or ``(set(), None)`` when no portfolio snapshot is present."""
    portfolio_snaps = context.snapshots_for("portfolio")
    if not portfolio_snaps:
        return set(), None
    snap = portfolio_snaps[0]
    payload = snap.payload_json or {}
    held: set[str] = set()
    for key in ("holdings", "reference_holdings"):
        rows = payload.get(key) or []
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and isinstance(row.get("ticker"), str):
                    held.add(_norm_symbol(row["ticker"]))
    return held, (snap if held else None)
```

In `run`, after computing `top` and before building `key_points`, add the held join and replace the `key_points`/`summary`/citations construction:

```python
        held, portfolio_snap = _held_symbols(context)

        def _is_held(c: dict) -> bool:
            return _norm_symbol(c.get("symbol", "")) in held

        key_points = [
            f"[{'보유·추세' if _is_held(c) else '신규'}] "
            f"{c.get('symbol', '?')} (score={c.get('score', 0):.1f}): "
            f"{', '.join(c.get('reasons', []))} [{c.get('source', '?')}]"
            for c in top
        ]
        held_trending = [c.get("symbol", "?") for c in top if _is_held(c)]
        if held_trending:
            summary = f"{summary} · 보유·추세: {', '.join(held_trending)}"
```

Then build `cited_snapshots` to include the portfolio citation when used:

```python
        cited = [
            StageCitation(
                snapshot_uuid=snap.snapshot_uuid,
                snapshot_kind="candidate_universe",
                payload_path="$.candidates",
            )
        ]
        if portfolio_snap is not None:
            cited.append(
                StageCitation(
                    snapshot_uuid=portfolio_snap.snapshot_uuid,
                    snapshot_kind="portfolio",
                    payload_path="$.holdings",
                )
            )
```

Replace the old inline `key_points = [...]` and `cited_snapshots=[...]` with the new `key_points`/`cited`. Pass `cited_snapshots=cited` in the return.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/services/investment_stages/test_candidate_universe_stage_evidence.py -v`
Expected: all PASS (existing + 3 new).

- [ ] **Step 5: Commit**

```bash
git add app/services/investment_stages/stages/candidate_universe.py tests/services/investment_stages/test_candidate_universe_stage_evidence.py
git commit -m "feat(rob-304): candidate_universe stage held cross-check (G2)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 2: auto_emit held-and-trending watch

Held symbols that also appear in the screener candidates surface as review-only `watch` items (held names are already excluded from buy candidates). Mirrors the news-watch invariants exactly: `item_kind="watch"`, `operation="review"`, `apply_policy="requires_user_approval"`, `intent="trend_recovery_review"`. Uses the KIS-primary `held` set (auto_emit's existing semantic) and the PR1 `candidate_by_symbol` map.

- [ ] **Step 1: Write the failing test** (append to `tests/test_auto_emit_candidate_citation.py`)

```python
def test_held_symbol_in_screener_surfaces_watch():
    snaps = [
        _Snap("portfolio", {"primary_source": "kis",
                            "holdings": [{"ticker": "005930"}]}),
        _Snap("candidate_universe", {
            "usefulness": "useful",
            "candidates": [
                {"symbol": "005930", "score": 8.0,
                 "reasons": ["단기 상승 모멘텀 후보"], "source": "kis"},
            ],
        }),
    ]
    items = EvidenceAutoEmitter().propose(
        snapshots=snaps, request_market="kr", account_scope="kis_live"
    )
    holds = [i for i in items if i.evidence_snapshot.get("proposer")
             == "auto_emit/held_and_trending"]
    assert len(holds) == 1
    item = holds[0]
    assert item.symbol == "005930"
    assert item.item_kind == "watch"
    assert item.operation == "review"
    assert item.apply_policy == "requires_user_approval"
    assert item.evidence_snapshot["candidate_score"] == 8.0
    # Held symbol must NOT also be proposed as a buy.
    assert not [i for i in items if i.side == "buy" and i.symbol == "005930"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_auto_emit_candidate_citation.py::test_held_symbol_in_screener_surfaces_watch -v`
Expected: FAIL — no held-and-trending item emitted.

- [ ] **Step 3: Implement** — in `auto_emit.py`, just before `return items`, add:

```python
        # Held-and-trending — held names that also surface in the screener
        # candidate universe. Review-only awareness signal (held names are
        # excluded from buy candidates above); no broker mutation.
        already_proposed = {item.symbol for item in items if item.symbol}
        for sym, cand in candidate_by_symbol.items():
            if sym not in held or sym in already_proposed:
                continue
            reasons = cand.get("reasons") or []
            items.append(
                IngestReportItem(
                    client_item_key=f"auto-hold-trend-{sym}",
                    item_kind="watch",
                    symbol=sym,
                    intent="trend_recovery_review",
                    rationale=(
                        f"보유 종목 {sym}가 스크리너 추세 상위에 등장 — 관망/추가검토 "
                        f"(score {cand.get('score')}, {', '.join(reasons)})"
                    ),
                    operation="review",
                    apply_policy="requires_user_approval",
                    evidence_snapshot=_make_evidence(
                        candidate_snapshot,
                        extra={
                            "candidate_snapshot_uuid": _snapshot_uuid(candidate_snapshot),
                            "candidate_score": cand.get("score"),
                            "candidate_reasons": reasons,
                            "candidate_source": cand.get("source"),
                            "held": True,
                            "proposer": "auto_emit/held_and_trending",
                        },
                    ),
                )
            )
```

- [ ] **Step 4: Run to verify pass + no regressions**

Run: `uv run pytest tests/test_auto_emit_candidate_citation.py tests/services/action_report/snapshot_backed/test_auto_emit.py -v`
Expected: all PASS (held-and-trending fires only when both portfolio held + candidate present; existing watch tests have neither combo).

- [ ] **Step 5: Commit**

```bash
git add app/services/action_report/snapshot_backed/auto_emit.py tests/test_auto_emit_candidate_citation.py
git commit -m "feat(rob-304): auto_emit held-and-trending watch items (G2)

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

## Task 3: Verify + open PR2

- [ ] **Step 1:** `uv run pytest tests/services/investment_stages/ tests/test_auto_emit_candidate_citation.py tests/services/action_report/ -q` → all pass.
- [ ] **Step 2:** ROB-287 guard: `uv run pytest tests/services/action_report/snapshot_backed/test_no_internal_llm_imports.py -q` → pass.
- [ ] **Step 3:** `make lint` → clean.
- [ ] **Step 4:** broad regression `uv run pytest tests/ -k "candidate_universe or auto_emit or screener" -q` → pass.
- [ ] **Step 5:** push `rob-304-pr2`, open PR against `main`.

## Self-Review (against spec §6)

- Builder stays held-agnostic ✓ (untouched).
- Stage held join via `portfolio` snapshot, held/new split, portfolio citation → Task 1. ✓
- auto_emit reuses held set to surface held overlap → Task 2. ✓
- Awareness only (union of holdings+reference), no sellability math → respects Toss-reference rule. ✓
- No placeholders; types consistent (`_norm_symbol`, `_held_symbols`, `candidate_by_symbol`, `_make_evidence` all defined/existing).
