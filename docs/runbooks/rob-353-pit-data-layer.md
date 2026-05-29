# ROB-353 PIT data layer runbook

Research/backtest only. Read-only public data. No live/Demo/broker/scheduler/DB mutation.

## Data source & retrieval
- Source: `data.binance.vision` USDⓈ-M futures (`futures/um`), public, no keys.
- Universe index: `build_pit_universe.py` (read-only S3 listing + boundary-month 1d klines).
- Bars: `pit_klines_fetcher.py --symbol <S> --interval {1d,1h} --from-month --to-month`.

## Universe definition
- USDT perpetuals only via `PITManifest.strict_usdt_perp()`: `status ∈ {live, dead}` plain
  `*USDT`; excludes `settling`, BUSD/USDC-quoted, dated/quarterly (`_`), `*SETTLED`.
- Active + delisted symbols included (the survivorship fix ROB-349 verified).
- PIT membership: each symbol tradeable only over `[listed_from, delisted_at)` (epoch ms,
  `delisted_at` exclusive); post-delist price-frozen zero-volume tail trimmed in `pit_bars`.

## Manifest
- Committed (metadata only): `data_manifests/pit_universe.v1.json` + `pit_universe.v1.meta.json`.
- Pinned by `snapshot_hash` (sha256 over canonical records). Editing the manifest requires
  updating `pit_universe.v1.meta.json` or `test_committed_manifest_loads_and_hash_matches_meta` fails.

## Raw-data root (NOT committed)
- `AUTO_TRADER_RESEARCH_ARTIFACT_ROOT/data/klines/<interval>/<symbol>/` if set, else
  `research/nautilus_scalping/data/...`. Both gitignored. No secrets logged.

## Regenerate the manifest (operator)
    curl -s https://fapi.binance.com/fapi/v1/exchangeInfo > /tmp/pit_audit_exchangeinfo.json
    uv run --no-project python build_pit_universe.py --exchange-info /tmp/pit_audit_exchangeinfo.json \
        --out data_manifests/pit_universe.v1.json

> Caveat: `write_outputs` emits a minimal `.meta.json` (schema_version, snapshot_hash,
> symbol_count, source, source_records). The committed v1 `.meta.json` carries two extra
> hand annotations (`build_window`, `note`); regeneration overwrites them. Re-add those by
> hand, or fold richer-meta emission into PR2 (tracked there).

## Not in this PR
- The `specs → campaign.run_campaign` bridge and families 1–3 RUN/verdict are PR2.
- `build_pit_universe.write_outputs` should emit `build_window`/`note` so regeneration is
  faithful to the committed sidecar (Minor; from PR1 final review).
