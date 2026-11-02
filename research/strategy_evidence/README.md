# Evidence for adjusting the live buy and sell rules (ROB-1458)

This package measures what changes to the live buy and sell rules would have done, on
the six months of the dots history export, and renders an options table for the
operator. It recommends no security and no amount, and every number is in-sample.

It builds on `research/trade_retro` (ROB-1439): the canonical fills, orders, positions
and the account cash lower bound come from there unchanged. It opens no database or
broker connection, uses no credentials and has no scheduler.

## Run it

```bash
uv run python -m research.strategy_evidence.run report \
  --export ~/work/herdr-artifacts/dots-history-2026-11-01 \
  --prices ~/work/herdr-artifacts/rob-1439-trade-retro/prices-2026-11-01 \
  --dividends ~/work/herdr-artifacts/rob-1458-strategy-evidence/dividends-2026-11-02 \
  --public-out docs/research/rob-1458-strategy-evidence-2026-11-02.md \
  --private-dir ~/work/herdr-artifacts/rob-1458-strategy-evidence
```

Add `--check` to compare both reports with a fresh computation, byte for byte. The
dividends snapshot (distributions of the parking instruments, which the ROB-1439 price
snapshot does not keep) was made once with `fetch-dividends --out <new directory>`,
which uses the network and refuses a directory that already holds a snapshot.

| Exit code | Meaning |
| --- | --- |
| 0 | Done, or `--check` matched. |
| 1 | `--check` found a difference. |
| 2 | `--private-dir` is inside the repository. |
| 3 | The export, price or dividends snapshot does not match its manifest. Nothing was written. |

The committed report holds ratios, counts and masked account labels. The full report
and the row-level CSVs behind every section go to `--private-dir`, outside the
repository.

## Modules

| Module | Job |
| --- | --- |
| `lots.py` | Each position's quantity and average cost, day by day. |
| `averaging.py` | Question 1: the `underwater_support_net` cohort, every buy by its lot's state, the crypto tier proxy. |
| `losscut.py` | Question 2: selling at the first close at or below -X%. |
| `entry_style.py` | Question 3: buy intents under deep limits against the next open. |
| `parking.py`, `dividends.py` | Question 4: parking yield, costs and break-even holds. |
| `ladders.py` | Question 5: crypto sells resting above the market. |
| `overlay.py` | Question 6: each rule replayed on the real accounts' cash and NAV. |
| `rulesets.py` | From-scratch rule sets on the same signals, with drawdowns. |
| `evidence.py`, `report.py`, `run.py` | Compute everything, render both reports, the CLI. |
