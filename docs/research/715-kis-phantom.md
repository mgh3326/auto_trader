# #715 research — KIS live unreconciled phantom rungs (the KIS version of #691?)

- lane: b715-kis-phantom · builder-devin-medium (SWE-2 medium) · T1 read-only research
- worktree: /home/mgh3326/work/auto_trader.t715 · branch: task715-kis-phantom
- round 2: incorporates tester (opus xhigh) round-1 BLOCKER findings — B1/B2/B3 corrected below.
- input: handoffkeep doc research/2026-09-25/715-rung-sweep-extract (operator-desk dry-run of scripts/rob1284_resting_rung_sweep.py on NCP, post-#691-backfill)
- NO broker calls, NO production DB, NO NCP access were used. Everything below is repo code + the masked extract.

## Headline answer (reframed)

Not "one KIS version of #691" — there are **four distinct gaps**, and the closest #691 analogue is the one that was almost missed:

1. **Partial-residual gap (KR + US) — the true #691-shaped bug.** A KR order that partially fills and is then swept/cancelled is booked to ledger status `partial` and never again consults expiry/cancel evidence — it stays `partial` forever (open scan), and `partial` never projects onto the proposal rung, so the rung stays `resting` → ledger_still_open. This class exactly matches the extract's shape and cannot be excluded by the extract (only 3 sample rows show ledger status). US has the same gap (expiry checked only when filled_qty==0).
2. **US evidence-window gap.** UsOverseasEvidenceAdapter looks the order up in a hardcoded 7-day history window; not-found returns PENDING("not_found") → silent noop_pending, indistinguishable in the output from a genuinely live order (no reason_code is emitted on the PENDING branch — this also means a dry-run cannot measure it directly; see Q6).
3. **KR evidence-window cap.** TTTC8001R is probed on the exact order date with a 90-day cap; older rows can never match → FillVerdict.NONE → noop_no_evidence + requires_manual_review forever.
4. **Coverage/invocation gap.** All reconcile entry points are off by default or operator-invoked, and every default call uses limit=100 created_at-ASC — permanently unresolvable oldest rows (2/3 above) starve every scan, so even an armed reconcile would never reach newer resolvable rows.

## Q1 — Does the KIS reconcile skip unmatched/expired orders the way pre-#691 Toss did?

Correction to the premise (tester B3): pre-#691 Toss did not merely skip unmatched orders — the broker returned positive evidence (REJECTED + timeInForce=DAY + canceledAt) that no classifier mapped to expiry; broker absence stayed a flagged manual-review anomaly. For zero-fill orders KIS already has the equivalent classifier. The actual skips, traced:

KR path (review.kis_live_order_ledger → kis_live_reconcile_orders_impl):

- Open scan: status IN (accepted, pending, partial), created_at ASC, limit default 100 — app/mcp_server/tooling/kis_live_ledger.py:609-625, entry :912-918.
- Evidence: TTTC8001R via _fetch_live_daily_rows (:458-484, call :477). _live_daily_order_window (:421-438) probes exactly the order's date, capped by _LIVE_DAILY_ORDER_LOOKBACK_DAYS=90 (:387). Rows older than 90 days get start=end=today-89 — they probe a wrong date and almost always return zero rows (the probe also sends ODNO/PDNO so an order-number reuse across days is a theoretical false-match, not verifiable in-repo).
- classify_fill_evidence (app/services/brokers/kis/mock_scalping_exec/fill_evidence.py:118-197): unmatched odno → NONE no_matching_order (:138-146); matched, zero fill → PENDING (:160-168).
- _reconcile_one_ledger_row (:732-909) named skips:
  - NONE → action=noop_no_evidence, requires_manual_review=True, ledger open (:786-797). Flagged, but permanent.
  - PENDING → classify_day_order_expiry → expired/cancelled on broker evidence (:765-784).
  - PARTIAL/FILLED (:799-909) → delta-idempotent booking; **never consults classify_day_order_expiry/cncl_yn/rjct_qty**. Once delta_qty<=0 → noop_already_booked with status=partial forever (:810-819). And _converge_kis_proposal_rung maps only filled/cancelled/expired/rejected (:652-661) — partial returns None, so a KR partial fill never projects to the rung. **Named skip: partial-then-swept KR orders are stuck in both ledger and rung.**
  - Note also: the expired/cancelled branch (:775-782) updates the ledger but does NOT call _converge_kis_proposal_rung — rung convergence for expiry happens only on a later non-dry pass (repair pre-pass :705-729, non-dry only :921-924) or via the rung sweep.

US path (review.live_order_ledger broker=kis → live_reconcile_orders_impl, live_order_ledger.py:548-607; open scan :183-207):

- Evidence: UsOverseasEvidenceAdapter (app/mcp_server/tooling/live_order_evidence.py:56-112) → _find_us_order_in_recent_history → TTTS3035R inquire_daily_order_overseas over a hardcoded now-7d..now window (app/mcp_server/tooling/orders_modify_cancel.py:372-373), all-symbol scan per exchange candidate NASD/NYSE/AMEX + DB lookup (:283, :297-315, :378-394). Per-exchange exceptions are swallowed → an error also looks like not-found (:386-394).
- Not found → FillVerdict.PENDING reason_code="not_found" (live_order_evidence.py:65-74) → noop_pending (live_order_ledger.py:327-329) with no requires_manual_review. **Named skip: a DAY order that expired >7 days ago is silently indistinguishable from a live order, forever.**
- Found: nccs_qty==0 + filled==0 → normalized status expired → FillVerdict.EXPIRED (:85-94, via _normalize_kis_overseas_order orders_modify_cancel.py:411-463 and _map_kis_status :155-160) → marked_expired (:331-343). But the expiry check is gated on filled_qty==0 (:85) — a partial-then-swept US order also never expires (same class as the KR partial gap; the extract shows no such rows, so it is not currently live).
- Supporting: the US cancel tool path never writes live_order_ledger (KR does _mark_ledger_cancelled; US branch does not — orders_modify_cancel.py:1209-1221), so operator-cancelled US orders also go silent-pending once >7 days old.

## Q2 — What broker evidence does KIS provide for an expired unfilled DAY order?

KR (measured — code + live-verified comments):

- TR TTTC8001R inquire_daily_order_domestic (app/services/brokers/kis/constants.py:87; domestic_orders.py:785+). Live-verified keys (probe 2026-06-10; domestic_orders.py:819-841; live_order_expiry.py:10-16): odno, orgn_odno, ord_qty, tot_ccld_qty, rjct_qty, rmn_qty, cncl_yn, sll_buy_dvsn_cd_name, excg_id_dvsn_cd.
- EOD day-order expiry = full reject rjct_qty==ord_qty>0 (live-verified on all 15 expired/cancelled 6/8-6/9 orders; live_order_expiry.py:21-24), gated on nxt_session_closed = order_date's 20:00 KST passed (:82-91).
- Cancels: cncl_yn truthy on matched row, or orgn_odno cancel-confirm row with '취소' in sll_buy_dvsn_cd_name (:267-270).
- Verdict: resolvable without guessing for rows within the ~90-day inquiry depth (comment at kis_live_ledger.py:385-387). For >90 days: tester flagged that KIS publicly documents a separate 3-months-ago daily-order TR (CTSC9115R/CTSC9215R) — not in the repo, unverified; treat "unrecoverable" as a question for operator-desk, not a fact.

US (measured code; broker depth inferred):

- TR TTTS3035R inquire_daily_order_overseas (constants.py:139-141; overseas_orders.py:702+) — caller-chosen ORD_STRT_DT..ORD_END_DT, client-side odno+symbol match; TTTS3018R inquire_overseas_orders for open orders (constants.py:132) — note: the reconcile adapter does NOT call TTTS3018R; that TR belongs to order-history/cancel paths only.
- Evidence: ft_ord_qty, ft_ccld_qty, nccs_qty (미체결수량=0 on dead orders; ROB-665 item 4, orders_modify_cancel.py:421-428), rvse_cncl_dvsn_name '취소' (live_order_expiry.py:73-74, :235).
- Resolvable without guessing — but only inside the adapter's 7-day window. Whether TTTS3035R still returns a dead order older than 7 days is inferred; an existing read-only probe answers it (get_order_history(market="us", days=N) already queries TTTS3035R with a caller-chosen range, default 30d — orders_history.py:292-306).

## Q3 — Are the 219 kis_live rows consistent with this mechanism?

Measured from the extract:

- 219 = sell 210 + buy 9; all reason_code=ledger_still_open, rung_state=resting — every one has a matched ledger row in an open status (the open set is {accepted, pending, partial}, resting_sweep.py:78). Only the 3 samples show status accepted; the other 216 could include partial rows — which matters because partial rows fit gap #1.
- Samples: BAC, JPM, UBER — all US tickers, sell, rung 1, masked broker_order 003…
- Sampling caveat (tester): sweep rows come out in OrderProposalRung.id ascending order, so first-3-per-group samples are likely the oldest rows, not a random draw — weak evidence for the KR/US split.
- The KR/US split is NOT in the masked extract, but it IS in the raw sweep.json on NCP: rows[].market and rows[].ledger_rows[].{table,status,reconciled_at} are in the payload (resting_sweep.py:170-201). One jq on /root/at-run/sweep715/sweep.json answers the split, the accepted/partial mix (tests gap #1), and rough ages — no new query, no broker call.
- Consistent readings: US-heavy → window gap #2; KR rows → gap #1 (partial) and/or #3 (>90d) and/or simply never reconciled (#4); plus the extract's own memory note: 미정산 705건 = arm 안 된 게이트·정산 flow 부재 (09-16).

## Q4 — upbit 20 and toss_live 12 (inference)

- upbit 20 (INFERENCE): 14 no_evidence_keys — unverified rungs carrying no broker_order_id/idempotency_key/correlation_id (ROB-1277 shape: submission outcome never bound; nothing to match, so not ledger phantoms). 6 ledger_still_open — accepted live_order_ledger broker=upbit rows. Upbit limit orders have no DAY expiry and genuinely rest until cancelled/filled, so these may be real live orders, not phantoms; UpbitEvidenceAdapter (live_order_evidence.py:124-188) resolves a cancelled one correctly, while not-found returns PENDING. Also: the operator-void path can only prove absence for rungs with an identifier, so no_evidence_keys rungs return unknown and cannot be voided (broker_gateway.py upbit branch).
- toss_live 12 no_ledger_row (INFERENCE): all 3 samples show rung_state=unverified with blank broker_order — so the surviving match key is idempotency_key (client_order_id) or correlation_id, not broker_order_id. A rung has keys but no row in any of the three ledgers matched (the sweep searches all three — resting_sweep_service.py:50-69, 117-161). Likely: submitted via a path whose ledger write failed fail-open / never ran, or key divergence. Resolution path already exists: order_proposal_void + fetch_operator_void_evidence scans Toss OPEN + bounded CLOSED windows by client_order_id and can void unverified rungs after broker-absence proof (broker_gateway.py:285+). The unmasked idempotency keys are needed to run it.

## Q5 — Recommendation (rescoped)

Measure first, then a fix task covering all of:

- M2 (zero cost): jq the existing /root/at-run/sweep715/sweep.json for the market/ledger-table/status/age split — decides which gaps are live before any code work.
- Fix A (US window): anchor the evidence inquiry on the ledger row's order/trade date instead of now-7d (mirroring _live_daily_order_window), bounded to documented TTTS3035R depth; make not-found distinguishable (requires_manual_review) instead of silent pending.
- Fix B (partial residual — the real #691 port): in the PARTIAL/delta-idempotent branch, consult expiry/cancel evidence (KR: classify_day_order_expiry on the same rows; US: extend the adapter to terminal-check partial rows too), mark the ledger expired/cancelled on evidence, and project the terminal rung state (add partial to the convergence map or pre-project then close, as the Toss fix did with pre-terminal fill projection).
- Fix C (KR expiry convergence): the expired/cancelled branch should converge the rung in the same pass (today it needs a second non-dry pass or sweep --apply).
- Fix D (scan starvation): bounded paging/aging for the open scan so permanently-stuck rows don't occupy every limit=100 slot.
- Explicitly out of scope: converting absence into expiry (absence-as-evidence, refused by #691 too), and arming any default-off gate.
- Tests: fixture-level — KR partial-then-expired books residual + marks expired + converges rung; US not-found flagged; date-anchored window; starvation paging. Backfill: operator dry-run first, then gated non-dry pass(es), then sweep --apply --confirm for rung transition.
- If no fix: acceptable only if M2 shows the 219 are all recent genuinely-live orders — implausible given age, but measurable.

## Q6 — Exact dry-run / measurement commands for operator-desk (NCP, deployed image e9477c7+)

Step 0 — free, no broker calls, answers the KR/US split and partial/accepted mix:

    jq on /root/at-run/sweep715/sweep.json: group rows[] by account_mode+market+ledger_rows[].table+ledger_rows[].status; also census.truncation_proof_by_window for ages.

Step 1 — KR leg (writes nothing; read-only TTTC8001R GETs; CLI has no --limit flag so use the kernel for full coverage):

    uv run python -m scripts.kis_live_auto_reconcile            # dry-run, limit=100
    uv run python -c "import asyncio,json; from app.mcp_server.tooling.kis_live_ledger import kis_live_reconcile_orders_impl; print(json.dumps(asyncio.run(kis_live_reconcile_orders_impl(dry_run=True, limit=800)), ensure_ascii=False, default=str))"

    Look for: counts{expired,cancelled,filled,partial,pending,none}; reconciled[].action would_mark_expired / would_mark_cancelled / would_book_filled / would_book_partial = resolvable; noop_no_evidence + requires_manual_review = permanent-skip; candidate_scan{scanned,open_total,limit} for truncation (705 backlog > any small limit); proposal_rung_sweep.summary stays NO_EVIDENCE in dry-run.

Step 2 — US leg (read-only TTTS3035R GETs; WARNING on load: each row re-scans the full 7-day all-symbol history across up to 4 exchanges with pagination — with limit=800 this can mean thousands of KIS calls; prefer a small limit first, and use market/broker filters):

    uv run python -c "import asyncio,json; from app.mcp_server.tooling.live_order_ledger import live_reconcile_orders_impl; print(json.dumps(asyncio.run(live_reconcile_orders_impl(market='us', broker='kis', dry_run=True, limit=50)), ensure_ascii=False, default=str))"

    Look for: counts{expired,cancelled,filled,partial,pending}; the US kernel emits would_book (not would_book_filled). CRITICAL: noop_pending rows do NOT carry reason_code — not_found is invisible in this output (tester B1). To distinguish aged-out rows from live ones, join reconciled[].ledger_id to created_at/trade_date >7 days on the DB side, or rely on Step 0's ledger_rows[].reconciled_at/status.

Step 3 — probe ">7-day dead-order visibility" (read-only, answers the inferred part of Q2):

    MCP get_order_history(market="us", symbol=<one stuck symbol>, status="all", days=90, is_mock=False) — if the dead order appears with nccs_qty=0/status expired, Fix A's wider window is sufficient; if not, the gap is broker-side depth.

## Commands run + rc (this session, all read-only, repo-local)

- git status / git log / git show e9477c7 --stat / git rev-parse HEAD — rc 0
- handoffkeep doc get research/2026-09-25/715-rung-sweep-extract — rc 0
- file reads/greps cited inline — no repo mutations besides this doc

## Tester

Round 1: opus xhigh, independent re-trace — VERDICT: BLOCKER @d36eaf5fc46224bb44b47bf404c33de2d33ca4e9.
Material findings adopted: B1 (US kernel emits no reason_code on PENDING — Q6 measurement fixed to M2/M3), B2 (partial-residual gap added as the true #691 analogue — verified at kis_live_ledger.py:799-819 and _converge map :652-661), B3 (#691 reframed: positive REJECTED+canceledAt evidence, not mere unmatched-skip), plus corrections on ledger open-status set, sampling bias, US-cancel-no-ledger-write, two-pass KR convergence, and US dry-run load.
Round 2 verdict pending at new head.
