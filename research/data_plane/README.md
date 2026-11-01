# data-plane-v1 (ROB-1443, ROB-1448)

Point-in-time daily inputs for the boosting baseline (U7) and the TFT
challenger (U8), per market (KR, US, crypto): **universe**, **daily bars**
(raw `bars_1d`; KR/US also the adjusted vintage `bars_1d_adjusted`),
**corporate actions**. Contract: `docs/research/data-plane-contract-v1.md`.
Collector inventory and decisions: `docs/research/data-plane-collector-inventory-2026-11-01.md`.
Broker sources, limits and the research budget:
`docs/research/data-plane-broker-sources-inventory-2026-11-01.md`.

Research only. KR/US bars come from the broker market-data APIs first (KIS,
then Toss) with the public Yahoo chart as the fallback; the manifest records
which source served each partition. Broker calls go only through
`broker_adapters.py`, which wraps the app's KIS/Toss clients in a view that
answers daily-chart methods only — no order, account or balance method is
reachable — and every call takes a research-budget slot (`budget.py`). All other
reads are public, unauthenticated sources (`public_http.check_url` refuses every
other URL before a socket opens). No database or scheduler code. Reuses U2/U3/U4
parsers (`kr_delist_repair`, `us_universe`, `crypto_universe`).

## Commands (manual, or Dagu once the proposal is approved)

```bash
ROOT=~/work/herdr-artifacts/data-plane-v1   # outside git

# Today's partitions (market-local day): universe, bars, adjusted bars, actions.
# Idempotent: a second run the same day fetches nothing and writes nothing.
# KR/US: --bar-sources kis,toss,yahoo and --broker-budget kis=2/1,toss=1/1 are
# the defaults; broker sources need the app's KIS/Toss credentials on the host
# and are recorded as unavailable without them.
uv run python -m research.data_plane.cli collect --root "$ROOT" --market kr
uv run python -m research.data_plane.cli collect --root "$ROOT" --market us
uv run python -m research.data_plane.cli collect --root "$ROOT" --market crypto

# Bars for an earlier session (needs a universe partition dated <= that day).
uv run python -m research.data_plane.cli collect --root "$ROOT" --market kr \
    --date 2026-11-02 --datasets bars_1d

# Health: exit 1 on a gap, a stale dataset, a corrupt or low-coverage partition.
uv run python -m research.data_plane.cli health --root "$ROOT" --market kr \
    --dataset bars_1d --ping-url-env DATA_PLANE_PING_KR_BARS
```

`collect` exit codes: `0` written or already present (bars on a non-session
day: `skipped_no_session`); `2` not collected (source failure, coverage below
`--min-coverage`, no universe) — nothing written for that dataset; `3` the
contract refused a row (for example a universe run for a past day, or bars
before the session's close + 30 min).

`--bar-sources yahoo` collects from the public source only. `--broker-budget`
is refused above 25% of a broker's per-key limit; the budget window lives in
`$ROOT/.budget/` and is shared by every collector on that root.
`--adjusted-window N` sets the sessions kept per adjusted vintage (default 20).
An adjusted vintage can only be collected on its own day; `--date` backfills
raw `bars_1d` only.

`--symbols A,B` restricts bars/actions to those universe instruments (recorded
as `instrument_selection: explicit_subset`). Use it only on a scratch root: a
partition is written once, so a subset partition is final for that day.

## Modules

| module | role |
|---|---|
| `contract.py` | markets, datasets, common fields, `validate_rows` (the PIT guard) |
| `store.py` | write-once partitions, deterministic bytes, append-only manifest |
| `calendars.py` | partition days and bar availability (exchange_calendars XKRX/XNYS; UTC days) |
| `sources.py` | public source URLs and pure parsers |
| `bar_sources.py` | KR/US bar source interface (KIS, Toss, Yahoo), vintages, default order |
| `broker_adapters.py` | read-only views of the app's KIS/Toss clients (the only app broker import) |
| `budget.py` | research share of each broker's call limit (sliding window, shared by flock) |
| `public_http.py` | allowlisted GET with spacing and bounded retries |
| `collect.py` | per-day collectors |
| `health.py` | gap/stale/corrupt/coverage check and the ping |
| `cli.py` | `collect`, `health` |

Tests: `tests/research/data_plane/` (network-free fixtures).
