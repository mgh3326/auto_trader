# #715 research — KIS live unreconciled phantom rungs (the KIS version of #691?)

- lane: b715-kis-phantom · builder-devin-medium (SWE-2 medium) · T1 read-only research
- worktree: /home/mgh3326/work/auto_trader.t715 · branch: task715-kis-phantom (from origin/main)
- input: handoffkeep doc research/2026-09-25/715-rung-sweep-extract (operator-desk dry-run of scripts/rob1284_resting_rung_sweep.py on NCP, post-#691-backfill)
- NO broker calls, NO production DB, NO NCP access were used. Everything below is repo code + the masked extract.

## Headline answer

Yes — there is a KIS version of #691, but it is **two** mechanisms, not one:

1. **KR leg (review.kis_live_order_ledger, kis_live_reconcile_orders)** — the expiry classifier already exists and is evidence-based (rjct_qty == ord_qty, cncl_yn, 취소-confirm rows). The remaining skips are: (a) the TTTC8001R evidence probe is clamped to the order's exact date with a **90-day cap**, so rows older than ~90 days can never match and fall to FillVerdict.NONE → noop_no_evidence forever; (b) the reconcile kernel is **never run unless a human invokes it** (KIS_LIVE_AUTO_RECONCILE_ENABLED default False, task registered but scheduleless). Stale-accepted rows then leave proposal rungs resting → ledger_still_open.
2. **US leg (review.live_order_ledger broker=kis, live_reconcile_orders)** — closer to the true #691 shape: UsOverseasEvidenceAdapter looks the order up in a **hardcoded 7-day history window** (_find_us_order_in_recent_history), and "not found" is returned as **PENDING** (reason_code="not_found") — which the reconcile maps to a silent noop_pending with **no requires_manual_review flag**. A DAY order that expired more than 7 days ago becomes permanently unreconcilable and invisible. All three kis_live sample rows in the extract are US tickers (BAC, JPM, UBER).

## Q1 — Does the KIS reconcile skip unmatched/expired orders like pre-#691 Toss?

Pre-#691 Toss: toss_reconcile_orders silently skipped orders it could not match in the broker evidence batch, so DAY-expired orders stayed accepted forever.

KIS KR path (measured — code trace):

- kis_live_reconcile_orders_impl scans open rows: status IN (accepted, pending, partial), ordered created_at ASC, limit default 100 — app/mcp_server/tooling/kis_live_ledger.py:609-625, entry at :912-918.
- Per row it calls _fetch_live_daily_rows → kis.inquire_daily_order_domestic (TTTC8001R) — kis_live_ledger.py:458-484, 477. The window is computed by _live_daily_order_window (:421-438): for a known order date it queries **exactly that date** (start==end==order_date), bounded by _LIVE_DAILY_ORDER_LOOKBACK_DAYS = **90** (:387). An order older than 90 days gets start=end=today-89 — i.e. it queries a date the order was never placed on, guaranteeing zero rows.
- classify_fill_evidence (app/services/brokers/kis/mock_scalping_exec/fill_evidence.py:118-197): no matched odno → FillVerdict.NONE "no_matching_order" (:138-146); matched but zero fill → PENDING (:160-168).
- _reconcile_one_ledger_row (kis_live_ledger.py:732-909):
  - NONE → action="noop_no_evidence", requires_manual_review=True, ledger left open (:786-797). **This is the named KR skip** — analogous outcome to pre-#691 Toss (row stays accepted forever), but it IS flagged for manual review rather than silently skipped.
  - PENDING → classify_day_order_expiry (live_order_expiry.py:240-286) can mark expired/cancelled **from broker evidence** (:765-784). So the expiry path exists for KR.
  - What never happens: a NONE row can never become expired/cancelled. Fail-closed by design (absence is not evidence) — correct, but permanent.

KIS US path (measured — code trace):

- live_reconcile_orders_impl (app/mcp_server/tooling/live_order_ledger.py:548-607) scans review.live_order_ledger open rows (status accepted/pending/partial), :183-207.
- Per row → get_evidence_adapter(row.broker) → UsOverseasEvidenceAdapter for broker="kis" (app/mcp_server/tooling/live_order_evidence.py:191-194, adapter :56-112).
- The adapter calls _find_us_order_in_recent_history which hardcodes **start_date = now - 7 days** (app/mcp_server/tooling/orders_modify_cancel.py:372-373) across exchange candidates NASD/NYSE/AMEX (+DB lookup) via inquire_daily_order_overseas (TTTS3035R).
- If not found → returns FillVerdict.**PENDING**, reason_code="not_found" (live_order_evidence.py:65-74). The reconcile then records action="noop_pending" (live_order_ledger.py:327-329). **No requires_manual_review, no anomaly — the row looks identical to a genuinely live order.** This is the sharpest analogue to the #691 skip: an expired order simply ages out of the 7-day evidence window and the ledger stays accepted forever, silently.
- (The same PENDING-not_found also fires for any KIS hiccup in the overseas inquiry, since _find_us_order_in_recent_history swallows per-exchange exceptions and continues — orders_modify_cancel.py:386-394.)

## Q2 — What broker evidence does KIS provide for an expired unfilled DAY order?

KR (measured — code + live-verified comments):

- TR: TTTC8001R via inquire_daily_order_domestic (app/services/brokers/kis/constants.py:87; app/services/brokers/kis/domestic_orders.py:785+). Real response keys confirmed by read-only live probe 2026-06-10 (domestic_orders.py:819-841; live_order_expiry.py:10-16): odno, orgn_odno, ord_qty, tot_ccld_qty, **rjct_qty**, rmn_qty, **cncl_yn**, sll_buy_dvsn_cd_name, excg_id_dvsn_cd.
- EOD expiry of an unfilled day order is expressed as a **full reject: rjct_qty == ord_qty > 0** (live-verified on all 15 expired/cancelled 6/8-6/9 orders — live_order_expiry.py:21-24), gated on nxt_session_closed (order_date's 20:00 KST passed; live_order_expiry.py:82-91) because intraday rjct_qty population is unconfirmed.
- Cancels: cncl_yn truthy on the matched row, or a cancel-confirm row whose orgn_odno points at the order with '취소' in sll_buy_dvsn_cd_name (매수취소/매도취소) (live_order_expiry.py:267-270).
- So: **yes, KR expiry is resolvable without guessing — the machinery already exists** — provided the order's date is inside the ~90-day inquiry depth (comment at kis_live_ledger.py:385-387 says TTTC8001R supports ~3 months) AND reconcile actually runs.

US (measured code, partly inferred broker semantics):

- TRs: TTTS3035R inquire_daily_order_overseas (constants.py:139-141; overseas_orders.py:702+) — supports arbitrary ORD_STRT_DT/ORD_END_DT range, all-exchanges scan with client-side odno+symbol filter (orders_modify_cancel.py:355-408); TTTS3018R inquire_overseas_orders for still-open orders (constants.py:132).
- Evidence fields: ft_ord_qty, ft_ccld_qty, **nccs_qty** (미체결수량 — 0 on a dead order; preferred over ordered-filled per ROB-665 item 4, orders_modify_cancel.py:421-428), rvse_cncl_dvsn_name containing '취소' (cancel evidence, live_order_expiry.py:73-74, 235).
- _map_kis_status: ordered>0 AND filled==0 AND remaining<=0 → "expired" (orders_modify_cancel.py:155-160); the adapter turns that into FillVerdict.EXPIRED (live_order_evidence.py:85-94) → ledger marked_expired (live_order_ledger.py:331-343).
- So the evidence exists and is already wired — **but only inside the 7-day window**. Whether TTTS3035R returns dead orders older than 7 days is **inferred** (the API takes an arbitrary date range; the docstring gives no documented depth cap — needs one operator read-only probe to confirm how far back expired unfilled orders remain visible).

## Q3 — Are the 219 kis_live rows consistent with this mechanism?

Measured from the extract:

- All 219 are account_mode=kis_live, reason_code=ledger_still_open, rung_state=resting — i.e. every one has a matched ledger row still sitting in an open status (accepted). Split: sell 210 / buy 9.
- The 3 sample rows (max 3 per account_mode×reason) are all US tickers — BAC, JPM, UBER, all sell, rung 1, ledger status accepted, masked broker_order ids beginning 003.

Consistent readings:

- account_mode "kis_live" covers BOTH equity_kr (→ kis_live_order_ledger) and equity_us (→ live_order_ledger, account_scope="kis_live") — broker_gateway.py:16-23 and order_execution.py:1415 vs :1442. The extract does NOT show which ledger table the rows came from, so the KR/US split inside the 219 is **not provable from the extract**.
- If the bulk are US (plausible given all 3 samples + sell-ladder skew): consistent with the 7-day-window mechanism — orders older than 7 days return not_found→PENDING→noop_pending forever, even if reconcile runs daily.
- If any are KR: consistent with either (a) reconcile simply never being run (gates default False; remembered note in the extract — "미정산 705건 = arm 안 된 게이트·정산 flow 부재"), or (b) rows older than 90 days that are permanently outside the TTTC8001R window.
- The extract contains no dates/sessions, so age distribution cannot be checked. To close this, the read-only query needed is (for operator-desk): per-ledger-table count, min/max trade_date, min/max created_at, and reconciled_at null-ratio of the open rows — e.g. SELECT 'kis' src, count(*), min(trade_date), max(trade_date), count(*) filter (where reconciled_at is null) FROM review.kis_live_order_ledger WHERE status IN ('accepted','pending','partial') — plus the same shape on review.live_order_ledger WHERE broker='kis' AND account_scope='kis_live'.

## Q4 — upbit 20 and toss_live 12 (inference)

- **upbit 20** (INFERENCE): splits into 14 no_evidence_keys (unverified rungs with no broker_order_id/idempotency_key/correlation_id — the ROB-1277 shape: the rung never bound to a broker order, e.g. submission outcome unknown or pre-key-era rows; there is nothing to match, so they are not phantom orders in the ledger sense) and 6 ledger_still_open (accepted rows in review.live_order_ledger broker=upbit). Upbit limit orders do not have DAY expiry — they genuinely rest until cancelled/filled — so the 5 sell + 1 buy ledger_still_open rows may be real live orders, not phantoms (e.g. KRW-BTC sell 0.00166776 at an unreachable price). UpbitEvidenceAdapter (live_order_evidence.py:124-188) would classify a cancelled one correctly; "not found" again returns PENDING, not NONE.
- **toss_live 12 no_ledger_row** (INFERENCE): the rung carries at least one evidence key but no row in review.toss_live_order_ledger matched on broker_order_id / client_order_id / correlation_id (resting_sweep_service.py:117-161). Likely candidates: (a) a send-path where the rung bound broker_order_id but the ledger insert failed fail-open or never ran (the ledger writers log-and-swallow failures); (b) orders placed before the toss ledger existed or via a path that bypasses it; (c) key divergence (client_order_id vs idempotency_key mismatch). Cannot be resolved from masked identifiers — needs the unmasked broker_order_id list to check whether the orders exist in Toss history at all.

## Q5 — Recommendation

**Yes, a fix task is warranted — but scoped to the US leg, and only after a dry-run measurement confirms the split.**

1. First (no code): operator dry-run measurement per Q6. If the 219 are overwhelmingly US live_order_ledger rows returning not_found→noop_pending, the bug is confirmed measured, not inferred.
2. Fix shape (proposal, mirroring #691):
   - In UsOverseasEvidenceAdapter.fetch_evidence / _find_us_order_in_recent_history: window the TTTS3035R inquiry on the **ledger row's order/trade date** (as the KR path already does in _live_daily_order_window) instead of a hardcoded now-7d, with a bounded lookback matching the broker's documented depth. Not-found inside a proven-complete window stays PENDING but should additionally flag requires_manual_review so it stops masquerading as a live order.
   - Keep evidence-first: expired only via nccs_qty==0/filled==0/expired status or cancel evidence — never absence-as-expiry.
   - KR leg: likely no code change needed for rows ≤90 days (reconcile run resolves them); for rows >90 days the broker inquiry itself cannot see them, so decide an operator procedure (manual review path) rather than a classifier change. Widening beyond the broker's inquiry depth is impossible — flag, don't fake.
   - Tests: fixture-level tests for the US adapter (order found expired inside a date-anchored window; not-found → manual-review flag; KR unchanged), plus a ledger→rung convergence test mirroring the #691 test set.
   - Backfill: operator dry-run first (Q6), then dry_run=False with the existing double gates; the resting-rung sweep then transitions the rungs from committed terminal evidence.
3. NOT recommended: converting not-found to expired (absence-as-evidence is exactly what #691 refused), or auto-arming the reconcile gates (hard-rule: default-disabled).

## Q6 — Exact dry-run commands for operator-desk (NCP, deployed image)

KR leg (writes nothing; makes read-only TTTC8001R GET calls):

    uv run python -m scripts.kis_live_auto_reconcile            # dry_run default, limit=100

    # for the full 219+ backlog, call the kernel with a bigger limit:
    uv run python -c "import asyncio,json; from app.mcp_server.tooling.kis_live_ledger import kis_live_reconcile_orders_impl; print(json.dumps(asyncio.run(kis_live_reconcile_orders_impl(dry_run=True, limit=400)), ensure_ascii=False, default=str))"

US leg (no CLI exists; the kernel takes market/broker filters; read-only TTTS3035R/TTTS3018R GET calls):

    uv run python -c "import asyncio,json; from app.mcp_server.tooling.live_order_ledger import live_reconcile_orders_impl; print(json.dumps(asyncio.run(live_reconcile_orders_impl(market='us', broker='kis', dry_run=True, limit=400)), ensure_ascii=False, default=str))"

What to look for:

- counts{} — the verdict histogram. Today every stale row shows "pending" (US not_found) or "none" (KR outside the 90-day window / unmatched).
- reconciled[].action — would_mark_expired / would_mark_cancelled / would_book_filled are the resolvable population; noop_pending (US) vs noop_no_evidence + requires_manual_review (KR) is the permanent-skip population.
- reconciled[].reason_code / reason — "not_found" pins the 7-day-window bug.
- candidate_scan{scanned, open_total, limit} — proves whether the 400-limit covered the whole backlog.
- proposal_rung_sweep.summary — should still show all 219 as NO_EVIDENCE/ledger_still_open in dry-run (sweep only transitions on committed terminal ledger rows; dry-run reconcile writes nothing, so the ledger stays open).
- Anomaly rows with "error" — would indicate the inquiry itself failing (rate limits, exchange scan errors), which also produces silent noop_pending.

## Commands run + rc (this session, all read-only, repo-local)

- git status / git log / git show e9477c7 --stat — rc 0
- handoffkeep doc get research/2026-09-25/715-rung-sweep-extract — rc 0
- file reads/greps listed inline above — no mutations made

## Tester

Spawned separately (opus, cross-family) against this head; verdict recorded below after its report lands.
