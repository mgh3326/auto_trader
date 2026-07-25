# US Pre-Open Gap Scan — Reps Log (ROB-924)

Operator-appended log for the manual gate defined in
`docs/runbooks/us-preopen-gap-scan-gate.md`. This is a **manual scaffold** —
no automation writes to this file. Append new rows per session; never
backfill or edit past rows except to fill an `outcome`/`손익%` column once it
becomes known.

**Sizing rule for this rep series** (fill in once, keep fixed across reps —
see runbook Step 4.3): `<operator fills in fixed per-candidate notional here,
subject to the alpaca_paper us_equity cap of qty<=5 / notional<=$1000>`

## Entry log

One row per **entered** rep (Step 3-approved candidate that reached Step 4).

| # | 날짜(KST) | 심볼 | 촉매(출처) | 프리마켓 갭%(quote_asof) | U3 판정 | 진입가/시각(ledger id) | 청산가/시각(ledger id) | 손익% | 청산 사유(손절/종가) | 비고(배제 적중 등) |
|---|---|---|---|---|---|---|---|---|---|---|
| | | | | | | | | | | |

Column notes:

- **촉매(출처)** — e.g. `실적 BMO (get_earnings_calendar)`, cite the source
  call from Step 1/3.
- **프리마켓 갭%(quote_asof)** — the Step 1 gap % *and* the `quote_asof`
  label it was computed from, e.g. `+11.7% (2026-07-16T21:04:00-04:00)`. If
  the quote's `price_source` was not `yahoo_prepost_last`, write `판정 불가`
  instead of a number — never fabricate a gap from a non-prepost quote.
- **U3 판정** — `PASS` (with the three sub-checks noted, e.g.
  `gap✓/capOK/volOK`) since only PASS rows reach the entry log; exclusions go
  in the table below instead.
- **진입가/시각(ledger id)** — price, KST timestamp, and the
  `alpaca_paper_ledger` row id (from `alpaca_paper_ledger_get` /
  `_list_recent`) confirming the entry fill. A rep isn't "done" until this id
  is filled in (runbook Step 6).
- **청산가/시각(ledger id)** — same, for the closing leg.
- **청산 사유** — `stop_loss_3pct` or `close_out` (see runbook Step 5).
- **비고** — free text; note if this rep is also a "MAN형" (실적 드리프트
  지속) vs "갭페이드" (premarket gap faded into/after the open) case for the
  performance report's classification below.

## Exclusion log

One row per symbol that **failed** Step 2 (U3) or was rejected at Step 3 —
i.e. never entered. This table is the source data for the performance
report's "U3 필터 유효성" and "갭다운 배제 적중률" sections — record every
exclusion, not just the interesting ones.

| # | 날짜(KST) | 심볼 | 배제 사유 | 프리마켓 갭%(quote_asof) | 이후 실제 등락(당일 종가 / 익일 시가) | 비고 |
|---|---|---|---|---|---|---|
| | | | | | | |

배제 사유 values: `gap_out_of_range` (note `>20%`/`<5%`), `gap_down`
(gap-down-exclusion-hit-rate candidate — earnings miss that gapped down and
was correctly never entered), `micro_cap_excluded`, `low_volume_excluded`,
`ma_pin_rejected`, `sympathy_recorded_only`, or free text for anything not
covered above (e.g. thin-거래대금 despite nominal cap > $1B).

"이후 실제 등락" is filled in retrospectively (next session or later) using
`get_quote` / `get_ohlcv` — this is what proves (or disproves) that the
exclusion was correct.

## Performance report (fill in only after ≥10 confirmed reps — AC #3)

**Do not fill in placeholder numbers before ≥10 reps exist with confirmed
ledger ids on both legs.** Everything below is a template; leave it as
`TBD` until there is real data to report.

### 1. Return distribution

- Basis: 당일 종가 청산 기준 / 익일 시가 청산 기준 (report both if the log
  has both).
- Distribution: mean, median, min, max, win rate (rows with 손익% > 0 ÷ total
  entered reps).

`TBD — fill in after ≥10 reps`

### 2. U3 필터 유효성

Compare post-hoc returns of **entered** (U3-pass) rows vs **excluded**
(U3-fail) rows' "이후 실제 등락":

- Entered rows: mean/median return.
- Excluded rows: mean/median subsequent move (did they underperform entered
  rows, confirming the filter added value?).

`TBD — fill in after ≥10 reps`

### 3. MAN형 (실적 드리프트) vs 갭페이드 비율

Using the entry log's 비고 classification: what fraction of entered reps
continued drifting in the gap direction (MAN형) vs faded back toward/through
the previous close (갭페이드) by close-out?

`TBD — fill in after ≥10 reps`

### 4. 갭다운 배제 적중률

Of all `gap_down` rows in the exclusion log, what fraction's "이후 실제
등락" confirms the exclusion was correct (i.e. the stock kept falling or
stayed down, rather than recovering)?

`TBD — fill in after ≥10 reps`

---

Promotion beyond `alpaca_paper` reps (any live or proposal-based path) is
explicitly **out of scope** for this log and this gate — see the Boundaries
section of `docs/runbooks/us-preopen-gap-scan-gate.md`. A promotion decision
requires this report plus a separate issue and explicit user sign-off.
