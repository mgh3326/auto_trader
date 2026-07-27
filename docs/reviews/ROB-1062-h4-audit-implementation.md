# ROB-1062 H4 audit implementation contract

Authority:

- `alpaca-h4-audit-contract-freeze-2026-07-26.md`, SHA-256
  `cffc68f839296a75278bc5b7c8a66537fd394e1a0e96ef8ea0a1927baf48ffa1`
- `gptpro-alpaca-h4-adjudication-result-2026-07-26.md`, SHA-256
  `e1d8caad6877e9f0acaa29e9bb4af692468d7deccfed0f681654b22bdf3fe812`

This is an implementation-level PIT and accounting rule. It does not modify
the sealed preregistration or any threshold.

## Event-time attribution

For a half-open window `W=[start,end)`:

| Lifecycle | Modeled-entry dry count | C120 cost | Closed/E120 |
| --- | --- | --- | --- |
| Entry fill and exit fill both in W | Include | Once at entry | Include |
| Entry fill in W, exit fill outside W | Include | Once at entry | Exclude |
| Entry fill outside W, exit fill in W | Exclude | Exclude | Exclude |
| Timestamp exactly at `end` | Outside W | Attribute by its own timestamp | Exclude |

Positions continue across boundaries. H4 never resets a carried position,
force-closes it, or fabricates an exit. Consequently, an OOS exit price cannot
enter a TRAIN closed-trade or E120 statistic.

## AC9 stress-cost accounting

H4 computes:

```text
100 × (365 / window_days) ×
Σ[(filled_qty × entry_fill_price) / 2000 × 0.012]
```

The population is every entry fill in the window, including entries still
open at the window end. The denominator is sealed fixed initial NAV `$2,000`,
and the cost rate is C120 only. The calculation uses full precision.
AP-A2's `[20.8%, 28.9%]` turnover band remains a separate gate.

The historical model fixes quantity before observing the later fill:
`filled_qty = target_notional / signal_reference_close`. The numerator then
uses the actual modeled entry fill price. This preserves vol scaling and cash
constraints while preventing substitution of a family base slot or a raw
entry count.

Every phase result exposes immutable modeled-entry evidence with
`entry_fill_ts_ms`, `filled_qty`, `entry_fill_price`, and the derived
`entry_filled_notional`, so the cap can be independently reproduced.

The enforced selection order is:

1. TRAIN closed count
2. annualized C120 stress-cost cap
3. TRAIN E120

`SEAL_CHANGE=NO` and `THRESHOLD_REDECISION=NO`.
