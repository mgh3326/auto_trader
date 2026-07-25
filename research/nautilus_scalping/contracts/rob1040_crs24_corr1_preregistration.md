# ROB-1040 — CRS-24 CORR-1

Status: canonical preregistration; implementation-only; empirical launch closed.

## Purpose and boundary

CRS-24 is an outcome-blind feasibility surface. It may count point-in-time
signals, reference availability, static size-filter feasibility, and fixed
lifecycle occupancy. It accepts no realized outcome columns and emits no
realized outcome metrics. The exit reference is represented only by timestamp
presence; its numeric value is never requested, accepted, or emitted.

This change permits synthetic/unit evaluation only. An empirical invocation
remains closed until merge, exact-main source refreeze, and separate one-shot
operator approval.

## Frozen universe and configuration slots

The symbol order is exactly:

1. `XRPUSDT`
2. `DOGEUSDT`
3. `SOLUSDT`

The four configuration slots are exactly:

| Slot | Identifier | Formation | Complete 4h returns | Posture |
|---|---|---:|---:|---|
| 0 | `CRS-A0` | 168h | 42 | primary |
| 1 | `CRS-A1` | 72h | 18 | sensitivity |
| 2 | `CRS-A2` | 336h | 84 | sensitivity |
| 3 | `CLOSED_UNUSED` | none | none | permanently closed |

Evaluation is basket-wide at scheduled 00:00 and 12:00 UTC cutoffs.

## Point-in-time features

At cutoff `t`, only 4h bars complete at or before `t` may contribute. All
required returns must exist on one contiguous 4h grid for all three symbols.
For every complete joint return, `m` is the equal-weight mean of the three raw
log returns and `e_i = r_i - m`.

For a configuration with formation length `n_L`, `E_i` is the sum of the last
`n_L` residual returns. Residual sample volatility for each symbol and common
sample volatility use the trailing 42 complete joint returns. The score is
`S_i = E_i / (sigma_i * sqrt(n_L))`. The common formation sum is `M`; its
normalized magnitude is `G = abs(M) / (sigma_m * sqrt(n_L))`. Dispersion is
`D = max(S_i) - min(S_i)`.

Any required residual or common sample volatility at or below `1e-8` closes
the observation.

## Point-in-time gates

Each current observation uses only prior valid scheduled observations in
`[t - 60 calendar days, t)`. The current cutoff is excluded. At least 100
observations are required.

Quantiles use nearest rank: after ascending sort, quantile `q` is item
`ceil(q * N)`, with one-based rank. The dispersion gate is
`D >= Q0.50(prior D)` and the common-magnitude gate is
`G <= Q0.75(prior G)`. Both gates must pass.

## Candidate and arbitration

The maximum positive score creates a LONG candidate. The minimum negative
score creates a SHORT candidate with strength equal to the negated score.
Exact symbol ties use frozen order `XRPUSDT`, `DOGEUSDT`, `SOLUSDT`.

The stronger side wins. If opposing strengths differ by less than `1e-12`,
the cutoff closes without a winner. At most one account-level winner exists
per cutoff.

## Calendar and lifecycle feasibility

The calendar authority is
`rob974_h4_contracts.exact_h4_folds()`, backed by `rob944_folds`. Each fold
must contain exactly 56 scheduled cutoffs. Entry and exit timestamps must both
lie inside the same half-open OOS interval. Entry is `cutoff + 60s`; exit is
`cutoff + 24h + 60s`. Both references are the 1-minute bar open at their
timestamp. Exactly two scheduled cutoffs per fold close at the fold-horizon
boundary, leaving 54 horizon-eligible cutoffs.

Each configuration/fold cell starts with no carried position. Only one
position may be active. A prior position whose exit equals the next proposed
entry timestamp still occupies the account. There is no queue, flip, or
resize. In the synthetic all-signal calendar, every cell must reconcile to 18
planned and 36 occupied among the 54 horizon-eligible cutoffs.

After arbitration, the deterministic close precedence is: occupied account,
missing entry reference, missing exit-timestamp presence, and static
size-filter failure. A failed reference or filter does not occupy the account.

## Frozen size-filter fixture

The fixture is local immutable research input, not live venue discovery:

| Symbol | Quantity step |
|---|---:|
| `XRPUSDT` | `0.1` |
| `DOGEUSDT` | `1` |
| `SOLUSDT` | `0.01` |

Reference quantity is floor(`8 USDT / entry reference`) to the symbol step.
The resulting reference notional must be within inclusive `6..10 USDT`.
Posture is `1x`, one-way. No external execution surface is part of CRS-24.

## Movement-capacity diagnostic

For the selected symbol only, use trailing 42 complete raw 4h log returns and
their sample volatility `sigma_raw`. Report
`A_i = 1e4 * sqrt(2/pi) * sigma_raw * sqrt(6)` basis points. This is a
trailing-only capacity diagnostic, never a selection feature, and never uses
future data.

## Evidence and reconciliation

Evidence contains exactly 3 configurations × 8 folds in configuration-major,
fold-minor order. Every cell contains sealed authorities and hashes plus:
scheduled, horizon-eligible, valid-input, each point-in-time gate, joint gate,
directional and simultaneous candidates, arbitration winners, occupancy,
entry-reference missing, exit-presence missing, static filter closes,
fold-horizon closes, planned count, movement-capacity summary, symbol
concentration, and LONG/SHORT counts.

Every scheduled cutoff has exactly one terminal classification: one closed
reason or planned. The closed-reason histogram plus planned count must equal
scheduled count. All subtotals and the campaign hash are deterministic.
