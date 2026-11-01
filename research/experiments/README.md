# Experiment registry, manifests and heartbeat (ROB-1429)

Unit U1 of the AT strategy restart plan (2026-11-01). Every strategy
experiment — backtest, boosting baseline, TFT challenger, forward shadow —
registers here before it reads test data. Research only: no runtime, policy,
order, ledger, scheduler or database change.

| File | What it is |
|---|---|
| `registry.json` | Hypothesis registry: one entry per hypothesis, its manifest hash and its cumulative test-look count `k` |
| `manifests/<id>.json` | Experiment manifest, format `experiment-manifest/v1` (below) |
| `heartbeat.json` | Epochs the heartbeat watches and where their last advance is read |
| `heartbeat.py` | Read-only heartbeat command |
| `reports/` | Experiment reports (created with the first report); every file must cite a registered hash |
| `scripts/ci/experiment_registry_lint.py` | Stdlib CI lint, run in the `lint` job and by `tests/ci/test_experiment_registry_lint.py` |
| `docs/research/experiment-split-leakage-rules.md` | Split and leakage rules every manifest follows |

## Manifest format `experiment-manifest/v1`

A manifest is one JSON object with exactly these keys. Its identity is the
sha256 of its canonical JSON (`json.dumps(obj, sort_keys=True,
separators=(",", ":"), ensure_ascii=False)`); print it with
`python3 scripts/ci/experiment_registry_lint.py --print-hash <file>`.
Write decimals as strings so the hash is stable.

| Key | Content |
|---|---|
| `manifest_format` | `"experiment-manifest/v1"` |
| `hypothesis_id` | the registry id this manifest belongs to |
| `version` | integer ≥ 1; a changed manifest is a new version |
| `title`, `question` | what is tested, in one sentence each |
| `status` | `draft` (sections may be pending) or `sealed` (no pending value anywhere) |
| `markets` | subset of `kr`, `us`, `crypto`; must equal the registry entry |
| `hashes` | named hashes and versions that pin the inputs: pre-registration text, dataset manifest, evaluation contract, code SHA, policy `{version, content_hash}`, cost table |
| `features` | list of `{name, available_at, source?}`; `available_at` states when the value becomes known (point in time, `available_at ≤ t`) |
| `labels` | list of `{name, definition, horizon_sessions}`; the horizon is counted in sessions of the market calendar |
| `calendar` | market → `XKRX`, `XNYS` or `24/7` |
| `splits` | `{scheme, rules_doc, purge_sessions, gap_sessions, folds, test}`; `rules_doc` is the split rules doc; `purge_sessions` ≥ the longest label horizon; `gap_sessions` ≥ max(5, longest label horizon); `folds` is a list of `{train, validate}` date ranges (empty only for a prospective test); `test` is `{kind: sealed_period \| prospective_seal, period, exposure_audit, seal_rule}` and starts after every validation period |
| `candidate_policies` | list of `{id, description}`: controls (current rule, cash) and candidates |
| `costs_fills` | `{cost_table, fill_models}`; the fill model is a bracket (optimistic touch, conservative through, volume share, empirical) |
| `tuning_budget` | `{max_configurations, search, notes?}`; every configuration tried counts |
| `seeds` | list of integers |
| `metrics` | `{primary, secondary, family, alpha, delta_min}`; primary is net return vs cash at equal capital after costs unless the pre-registration says otherwise; `family` is the multiple-comparison family |
| `stopping_rules` | list of rules that stop the experiment or the ML track |
| `cumulative_k` | `{prior_k, source: "registry"}`: looks already spent by earlier versions; the live count is the registry's `k` |

In a `draft` manifest the sections `hashes`, `features`, `labels`,
`calendar`, `splits`, `candidate_policies`, `costs_fills`, `tuning_budget`,
`seeds`, `metrics` and `stopping_rules`, and single values inside them, may be
`{"pending": "<why and when it is fixed>"}`. A `sealed` manifest may contain no
pending value. A registry entry with status `sealed`, `running` or `closed`
requires a sealed manifest.

## Registry rules

- `id` is unique; entries are never removed (set `status: retired`).
- `manifest_sha256` must equal the manifest's hash. To change a manifest,
  move the old hash to `previous_manifest_sha256`, register the new hash and
  set the manifest `version` to 1 + the number of previous hashes. The lint
  rejects an edited manifest under an old hash. Manifest JSON may not repeat a
  key.
- **Every test-set look increments `k`.** Before opening the test, append
  `{seq, at, manifest_sha256, report, report_sha256}` to `test_looks` and set
  `k = len(test_looks)` in the same commit. `report_sha256` is the sha256 of
  the report file's bytes; a counted report is frozen. Earlier looks are
  append-only, `k` never decreases, status never moves backwards
  (draft → sealed → running → closed; retired is final) — checked against the
  base branch in CI.
- A report carries two markers, each on its own line: the key
  `Experiment-Manifest-Sha256` followed by a colon and the 64-hex manifest
  hash it ran under, and the key `Experiment-Test-Look` followed by a colon and
  `yes` or `no`. Keys are matched in any case and under markdown decoration
  (bold, list, quote, table cell, indent), so do not write a key followed by a
  colon in prose.
  The lint scans every `*.md` under `research/` and `docs/research/`. A cited
  hash that is not registered, a malformed hash, a look value other than
  `yes`/`no`, or more than one look marker fails CI. A `yes` look that is not
  in `test_looks` fails CI. Every file in `research/experiments/reports/` must
  be `.md` and cite a hash.
- A hypothesis with status `running` must have an epoch in `heartbeat.json`.

Registered on 2026-11-01, all with `k = 0` and draft manifests:
`ROB-1411` (buy-drought same-candidate comparison, `research/rob1411_buy_drought_comparison/PREREG.md`),
`H-entry-price` (support-ladder limit price from `touch_5` and `xret_20|touch`),
`H-admit` (admit or skip from 10/50/90 quantiles of the t+1 open → t+5 close return).

## Heartbeat

Forward experiments die unattended (ROB-1391 had no advance after 10-29).
The heartbeat prints each epoch's last advance and the number of sessions of
its calendar that closed since. More than `idle_red_after_sessions` (3) idle
sessions prints `RED`.

```bash
uv run python -m research.experiments.heartbeat \
  --path ROB-1391="$PRIVATE/rob1391/ledger" \
  --path H5="$PRIVATE/h5/weekly-snapshot.json"
```

```
RED     ROB-1391         last_advance=2026-10-29T21:05:00+00:00 idle_sessions=4 calendar=XNYS (last advance event; idle 4 > 3)
OK      H5               last_advance=2026-11-04T21:00:00+00:00 idle_sessions=0 calendar=24/7 (latest of 1 timestamp(s))
```

Exit code 0 = all OK, 1 = any RED, 2 = no RED but some UNKNOWN (no path,
unreadable source, a timestamp after now), 3 = usage or config error
(unreadable or malformed `--config`, bad `--path` or `--now`).

| Source kind | Reads | Last advance |
|---|---|---|
| `hash_chained_ledger` | `<dir>/events.jsonl` (ROB-1391 ledger layout) | latest `recorded_at` of an `advance_kinds` event; if none, the seal time ("never advanced since seal") |
| `json_timestamps` | one JSON file | latest timestamp at the `select` path (`*` walks a list) |

- **ROB-1391**: the private `--ledger-dir` of `research/rob1391_us_resubmit_shadow`; advances are `register` / `step` events written by `advance`.
- **H5**: the authoritative stamp is `review.binance_h5_lane_state.updated_at` (database). The heartbeat does not read any database; it reads a snapshot file in the `scripts/binance_h5_weekly_score.py --snapshot-json` format, where `record_nav` writes one `nav_samples[].observed_at` per tick. Producing that snapshot is the operator's step. 🔴 For H5, the heartbeat measures the snapshot, not the runner: RED means either the runner stopped recording NAV **or** the snapshot was not re-exported for more than 3 UTC days. Re-export the snapshot before reading an H5 RED as a runner stall.

Ledger paths are private and stay out of the repository: the committed config
leaves `path` null and the operator passes `--path` (or a private copy of the
config with `--config`). The command only reads files: no lock, no database,
no network, no app or broker import (asserted by a test). It is not scheduled;
wiring it to a timer or alert is a separate operator decision.
