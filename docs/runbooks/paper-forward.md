# Daily paper forward shadow (ROB-1473 phases 1–2)

The operator wants daily forward evidence for crypto B3 and the US near-market
entry plus first -8% 50% add rule. This runner records hypothetical fills and
paired episode accounting. It sends no order and reads no database or credential.
Paper accounts and sleeve integration are later phases of ROB-1473.

## Inputs and fixed conventions

Set `PAPER_FORWARD_UNIVERSE` to the **frozen research** `prices_manifest.json`
(`trade-retro-prices/v1`), including its missing-symbol keys. Crypto uses that
snapshot's Upbit KRW symbols; US uses its US symbols, excluding parking in both.
The universe's hash, shared rule hash, engine hash and research cost reserve are
frozen on the first committed day. A changed definition requires a new directory
(epoch). Public Upbit daily candles and Yahoo daily history are fetched directly
through the existing research readers; app settings and broker clients are absent.

`PAPER_FORWARD_SIGNALS` names a private, normalized intent feed:

```json
{
  "schema": "paper-forward-signals/v1",
  "market": "us",
  "complete_through": "2026-11-02T22:00:00+00:00",
  "signals": [
    {
      "key": "opaque-stable-intent-key",
      "symbol": "AAA",
      "at": "2026-11-02T13:00:00+00:00",
      "u": "<original intent notional>",
      "control_limit": "<original deep-limit price>"
    }
  ]
}
```

The amount and price placeholders must be replaced by **decimal strings from
the original intent**; they are never amounts chosen by this code. Omit
`control_limit` for crypto. Keys must be unique, opaque and stable; no account or
order identifiers, credentials or free-text rationale belong in this feed.
`complete_through` is evidence of input coverage, not the time the file was copied.
Missing/stale feeds are `unverified`, while a fresh feed with `signals: []` proves
zero entries for that period. Keep overlapping history so a retry can deduplicate.

The desk supplies this read-only feed from its intent/export collection; this PR
does not add a producer or activate one. Follow the research signal definitions:

- Crypto: `rulesets.signals` — accepted buy **or real non-seed buy fill**, earliest
  per market/symbol/local ISO week, across accounts, including recurring micro-buys.
- US: `entry_style.intent_table` — first accepted non-parking buy per
  account/symbol/side/local ISO week. Preserve the first intent's notional and limit.
- A signal while a candidate position is held joins that episode and creates no
  additional entry. A new signal while flat can open another episode.

These rules need an intent stream; they do not generate a buy from prices alone.
The first run begins at `--asof` and does not invent historical positions. Later
runs catch up every missing **market session**. Supply complete feed history for
that interval. A fresh feed is required before calling collection operational.

Entry follows research `next_open_session`: crypto uses the next UTC day's open;
US uses the first 09:30 ET open after the signal. Only completed daily bars are
read. Crypto date D completes at D+1 00:00 UTC (09:00 KST). XNYS provides US
holidays, DST and early closes. `--asof` must itself be a completed session.

B3 uses equal **notional** units u: entry, both close-triggered -10%/-20% adds
from first entry (both may fire together), +10% target over harmonic average
cost, and -35% average-cost stop only after both adds. Adds precede exits on
the same bar. There is no B3 time stop. US adds 50% of held **quantity** at its
first close <= -8% from entry, once, with fractional quantities and no stop.
US split events adjust the held quantities and entry reference before decisions.

The controls and capital are fixed per episode:

| Market | Fixed E | Candidate | Control |
| --- | --- | --- | --- |
| crypto | 3u | entry u and up to two u adds | 1u next-open buy-and-hold through D0+60; 2u cash |
| US | 1.5u | entry u and one 50%-quantity add | original frozen DAY limit, u notional, no adds; 0.5u reserved cash |

The US control uses a **conservative open/close-only model**: open <= limit fills
at open; otherwise close <= limit fills at limit; otherwise DAY expires and cash
earns zero. Intraday touches are not claimed. A missing original limit makes
control P&L and D_i `unverified`, never zero. This assumption was escalated to the
desk because the retrospective uses actual fills rather than a frozen daily-bar
deep-limit simulator. It is recorded before the first dry run; it is not tuned.

Each invested tranche reserves the research `ROUND_TRIP_COST` on its notional,
including open positions, exactly as replay does. This is an assumed conservative
cost, not measured fees. It is an analytical P&L reserve; E funds buys and costs
are reported separately. Cash earns zero. Currencies are never summed.

## Run and inspect

```bash
export PAPER_FORWARD_UNIVERSE=<frozen research prices_manifest.json>
export PAPER_FORWARD_SIGNALS=<normalized private intent feed.json>
export PAPER_FORWARD_DIR=<private durable artifact directory outside git>
uv run python scripts/paper_forward/run.py --market crypto --asof <completed UTC day> --dry-run
uv run python scripts/paper_forward/run.py --market us --asof <completed XNYS day> --dry-run
# Advance hypothetical episodes after reviewing the preview:
uv run python scripts/paper_forward/run.py --market crypto --asof <completed UTC day>
```

`--asof` defaults to the last completed market session. The JSON printed to stdout
contains the signals, ledger diff and episode report. Markdown and JSON previews
go to `preview/<market>/<date>.*`; they never advance committed episodes. Normal
runs publish `ledger/<market>/<date>.json` and `reports/<market>/<date>.*`.
The ledger is append-only at the service boundary: write-once files, fsync,
atomic hard-link publication, per-market flock, and a previous-file hash chain.
Re-running a committed date performs no fetch and changes no ledger/report bytes;
it also repairs report publication if a crash followed the ledger commit.
Private filesystem permissions remain the operator's responsibility.

JSON was chosen because the experiment is account-independent and phase 3 has
not selected paper accounts. No additive DB migration is needed yet. Retain and
back up the whole directory. Never edit an existing day to hide a gap; start a
new epoch if inputs or definitions need correction. Do not share a ledger directory
between independently registered experiments.

Reports include opened/added/closed episodes, open cost and value relative to E,
net episode P&L, H maturity and `D_i = (candidate P&L - control P&L) / E`. H is
D0+60 crypto days or D0+20 XNYS sessions, fixed on the independent calendar.
Exits before H remain cash through H; a still-open candidate is marked at H and
continues under its strategy afterwards. The H result is frozen once recorded.
Missing bars never move H. A gap could hide an add or exit, so that episode stays
`unverified` and subsequent decisions are suspended rather than fabricated.
Missing marks, limits and coverage are reported explicitly, with null numeric
results rather than 0. Unfilled controls with complete data legitimately earn 0.

These reports are a function check and descriptive evidence, not a statistical
promotion verdict. ROB-1473 is registered as a draft feasibility experiment;
confirmation requires an operator-sealed manifest and counted test-set looks.

## Scheduling and validation

`deploy/dagu/paper-forward.yaml` is a **PROPOSAL**, unregistered. It proposes
00:20 UTC (after crypto close) and 23:00 UTC (after US close in either DST
regime), running both market steps; the extra market invocation is idempotent.
Desk must provision fresh intent feeds, a durable directory and a reviewed checkout
before registration. No scheduler, deploy or paper-account activation occurs here.

```bash
uv run pytest tests/research/test_paper_forward.py tests/research/strategy_evidence/test_core_calculations.py -q
```
