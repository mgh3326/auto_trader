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
  after-market.

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

## 4. Read-only checks (desk)

```sql
SELECT count(*), min(list_asof), max(list_asof), max(list_source)
FROM krx_after_market_eligibility;

SELECT u.symbol, u.exchange, u.security_type, u.krx_trading_suspended,
       e.symbol IS NOT NULL AS listed, e.list_asof
FROM kr_symbol_universe u
LEFT JOIN krx_after_market_eligibility e ON e.symbol = u.symbol
WHERE u.symbol IN ('375500', '459580', '357870');
```

## 5. Limits

- This PR changes the approval window only. The Toss order tool's
  `nxt_preflight` advisory still warns in `nxt_after` for non-NXT names, and
  the operator prompts still say "check nxt_tradable before evening". Both are
  follow-ups.
- Whether each broker's API actually accepts a KRX after-market order for a
  non-NXT name is broker evidence (the desk Toss preview at 16:05), not
  something this repo asserts.
