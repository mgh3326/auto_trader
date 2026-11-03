# KRX after-market eligibility list (#925)

## 1. What it is

The KRX after-market opened 2026-09-14: 16:00-20:00 KST, continuous
matching, about 2,000 KOSPI/KOSDAQ names. ETFs, ETNs and other off-regular
products come in a later phase, and there is no KRX pre-market (08:00-08:50
stays NXT-only). Source: hk:doc `strategy-lab/2026-09-29/krx-aftermarket-vs-nxt`
(id 8157), which summarises theguru.co.kr's 2026-09-27 article (no=107383)
and the 375500 evidence (`nxt_tradable=false`, yet it traded on Toss
15-minute bars 16:00-19:45 on 2026-09-28).

`krx_after_market_eligibility` holds the KRX-published list. It feeds:

- `get_quote.krx_after_tradable` for KR quotes;
- the order-proposal approval window, which allows 16:00-20:00 for a
  non-NXT name only when that name is proven eligible for the KRX
  after-market;
- the Toss order-tool `nxt_preflight` (#969: preview, place, modify, and
  `suggest_order_account`), which applies the same rule. Inside 16:00-20:00
  a non-NXT name passes with `reason: krx_after_tradable`. An empty,
  unreadable or stale list blocks with `krx_after_capability_unknown` or
  `krx_after_capability_stale`, not `not_nxt_eligible`, so a missing import
  is visible as such.

An empty table means no list has been imported, so every symbol reads
`false`. That is the state right after the migration.

## 2. Import (operator only, manual)

1. Download the KRX after-market eligibility list (a KRX notice or data
   portal export) as CSV/TSV. It needs a column named `종목코드`, `단축코드`,
   `symbol` or `code`; one code per line also works. The file can be UTF-8 or
   CP949, and `A`-prefixed codes are accepted.
2. Dry-run first:

   ```bash
   uv run python scripts/import_krx_after_market_eligibility.py \
       --file <list.csv> --source "<KRX notice URL or file name>"
   ```

   Check `listed` (it should be about 2,000), `unknown` (codes that are not in
   the active `kr_symbol_universe`) and `listed_non_stock` (ETF/ETN or other
   non-`STOCK` rows; these still read `false`).
3. Commit with `--commit`. Pass `--asof <ISO with offset>` when the list is
   current as of a time other than now.

Each import replaces the whole snapshot in one transaction. A single
unparseable code rejects the whole file. An empty list and a blank
`--source` are both refused.

## 3. Staleness

A list older than 7 days (`KRX_AFTER_LIST_STALE_AFTER`) reads as unknown,
so every symbol is `false` and non-NXT names lose the evening window again.
Re-import at least weekly, or after any KRX notice that changes the list.

### Freshness alert (ROB-1488, hk task 2712 item 1a)

`get_operating_briefing(market="kr")` includes
`staleness.krx_after_market_eligibility`: `status`, `requires_attention`, row count,
oldest/newest list timestamps and an operator warning. Missing, stale, future or
unreadable evidence requires attention. The probe checks the oldest row, so one
fresh row cannot conceal a partially stale snapshot. Other markets do not query it.
This is a read-only alert; it never refreshes eligibility during a quote or approval.

For a bounded, SELECT-only check with an error log captured by the existing CLI
logging/Sentry setup:

```bash
uv run python -m scripts.diagnose_krx_after_market_eligibility
```

Exit 0 means the whole stored snapshot is within the existing seven-day freshness
window; exit 1 means missing/stale/unknown/unavailable evidence. Freshness alone is
not a tradability verdict. The normal positive-evidence gate still applies.

No automatic producer exists in this repository: `replace_krx_after_market_list`
is called only by the file importer. Fixing KRX login does not run that importer.
An automated refresh needs a verified KRX-published per-symbol feed with its
request/response contract, effective date and completeness evidence. Neither the
general stock list nor positive after-market volume establishes eligibility. The
[KRX KOSPI rules](https://regulation.krx.co.kr/contents/RGL/03/03010301/RGL03010301.jsp)
and [KOSDAQ rules](https://regulation.krx.co.kr/contents/RGL/03/03020302/RGL03020302.jsp)
include daily exclusions; do not use a universe-derived substitute.

Proposed recurrence for desk review, **not registered**: `45 15 * * 1-5`,
`Asia/Seoul`, run the diagnostic at 15:45 KST before the evening session. A future
verified fetch should run before that check and persist only a complete, dated
snapshot through the existing writer. Fetch scheduling remains a desk decision.


## 4. Read-only checks (desk)

```sql
-- Before/after deployment and after each operator-approved import.
SELECT count(*) AS rows, min(list_asof) AS oldest_asof,
       max(list_asof) AS newest_asof,
       count(*) FILTER (WHERE list_asof < now() - interval '7 days') AS stale_rows
FROM krx_after_market_eligibility;

-- Baseline 2026-11-03: rows=2766, stale_rows=2766,
-- oldest_asof=newest_asof=2026-09-29 23:00:00+00.
-- Deploying the alert does not refresh this data. A verified import must do so.

SELECT u.symbol, u.exchange, u.security_type, u.krx_trading_suspended,
       e.symbol IS NOT NULL AS listed, e.list_asof
FROM kr_symbol_universe u
LEFT JOIN krx_after_market_eligibility e ON e.symbol = u.symbol
WHERE u.symbol IN ('375500', '459580', '357870');
```

## 5. Limits

- #925 changed the approval window; #969 aligned the Toss order-tool
  `nxt_preflight` with it. The operator prompts still say "check
  nxt_tradable before evening"; that is a follow-up.
- Until a list is imported, every non-NXT name in 16:00-20:00 reads
  `krx_after_capability_unknown` in the preflight (blocked in `required`
  mode, warned otherwise).
- Whether each broker's API actually accepts a KRX after-market order for a
  non-NXT name is broker evidence (the desk Toss preview at 16:05), not
  something this repo asserts.
