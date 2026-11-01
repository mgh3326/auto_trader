# data-plane-v1 (ROB-1443)

Point-in-time daily inputs for the boosting baseline (U7) and the TFT
challenger (U8), per market (KR, US, crypto): **universe**, **daily bars**,
**corporate actions**. Contract: `docs/research/data-plane-contract-v1.md`.
Collector inventory and decisions: `docs/research/data-plane-collector-inventory-2026-11-01.md`.

Research only: public, unauthenticated sources (`public_http.check_url`
refuses every other URL before a socket opens); no database, broker, account,
credential or scheduler code. Reuses U2/U3/U4 parsers (`kr_delist_repair`,
`us_universe`, `crypto_universe`).

## Commands (manual, or Dagu once the proposal is approved)

```bash
ROOT=~/work/herdr-artifacts/data-plane-v1   # outside git

# Today's partitions (market-local day): universe, then bars, then actions.
# Idempotent: a second run the same day fetches nothing and writes nothing.
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

`--symbols A,B` restricts bars/actions to those universe instruments (recorded
as `instrument_selection: explicit_subset`). Use it only on a scratch root: a
partition is written once, so a subset partition is final for that day.

## Modules

| module | role |
|---|---|
| `contract.py` | markets, datasets, common fields, `validate_rows` (the PIT guard) |
| `store.py` | write-once partitions, deterministic bytes, append-only manifest |
| `calendars.py` | partition days and bar availability (exchange_calendars XKRX/XNYS; UTC days) |
| `sources.py` | source URLs and pure parsers |
| `public_http.py` | allowlisted GET with spacing and bounded retries |
| `collect.py` | per-day collectors |
| `health.py` | gap/stale/corrupt/coverage check and the ping |
| `cli.py` | `collect`, `health` |

Tests: `tests/research/data_plane/` (network-free fixtures).
