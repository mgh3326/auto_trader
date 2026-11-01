# kr-delist-repair-v1 (ROB-1430)

Repairs the KR delisting / terminal-outcome gap in `kr-corpus-v1` and exports a
causal daily dataset. Research only: no `app` import, no database, no broker,
no scheduler.

## Inputs

| Input | Use |
|---|---|
| `kr-corpus-v1` main scope (2015-01-02..2024-12-30), membership + valid bars | sessions, symbol presence, prices |
| `kr-corpus-v1` clamp-admit `no_trade` rows | no-trade (suspension) sessions |
| KIS master files `kospi_code.mst.zip` / `kosdaq_code.mst.zip` (public) | "trading today" — the same files `kr_symbol_universe` is synced from |
| KIND 상장폐지현황 Excel per market (public) | delist date, reason and note by exact six-character code |
| KIND disclosure search + KRX `상장폐지(…)` notice bodies (public) | preferred / non-common issues that the KIND company list omits; the body names the exact short code |

The sealed holdout (2025-01-01..2026-07-31) is never opened: corpus paths under
`holdout/` and rows outside the main scope raise `HoldoutAccessError`.

## Commands

```bash
# 1. Network, public pages only; every body is saved with its sha256 under DIR/raw.
uv run python -m research.kr_delist_repair.cli collect --out DIR --asof 2026-11-01
# 2. Offline: resolution, reports, export and manifest.
uv run python -m research.kr_delist_repair.cli build --out DIR --repo-data research/kr_delist_repair/data
```

`DIR` used for v1: `/Users/mgh3326/work/herdr-artifacts/kr-delist-repair-v1`. The
export parquet and the resolution table stay there (no large data in the repo);
their sha256 and row counts are in `data/export_manifest.json`.

## Outputs

| File | Content |
|---|---|
| `data/join_audit.md` | corpus ⋈ `kr_symbol_universe` audit |
| `DIR/export/delist_resolution.v1.csv` | one row per target listing: dates, reason, outcome, identity, suspension counts, source per row (kept out of the repo; sha256 and row count in `data/export_manifest.json`) |
| `data/unresolved.v1.csv` | unresolved symbols with the reason |
| `data/coverage_report.md` | coverage per frame, outcome mix, unresolved list, known limits |
| `data/export_manifest.json` | sha256, bytes and row counts of the export files and inputs, conventions, coverage |
| `DIR/export/kr_daily_causal.v1.parquet` | the causal daily dataset |

## Rules the code enforces

- **Resolution order**: KIND list row by exact code → KRX notice body by exact
  code → the parent common share's KIND row on the same session. Nothing is
  matched by company name.
- **Corpus agreement**: for a symbol whose membership ended in 2015–2024 the
  delist date must be the next corpus session after its last membership
  session; otherwise the row stays unresolved with the reason.
- **available_at**: bars at session 16:00 KST; terminal events at the notice
  filing time when a notice body was used, else delist date 00:00 KST.
  `CausalTable.lookup` / `causal_join` raise `AvailabilityViolation` rather
  than use a row or a gated column early.
- **Terminal rows**: exactly one per symbol whose membership ended in scope, on
  its last session; never forward-filled; no row on or after a delist date
  (`validate_panel` raises `ExportConflict`). An ending without a resolved
  event is flagged `terminal_outcome=unresolved`, not dropped.
- **Payout**: `payout_semantics` is a labelling hint; no payout amount is
  asserted.
