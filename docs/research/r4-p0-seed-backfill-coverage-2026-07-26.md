# R4 P0 seed backfill coverage — 2026-07-26

## Result

W2 passed. The three signal symbols have 252/252 complete UTC 4-hour
OFI epochs and 252/252 premium epochs. BTCUSDT has the same predictor-only
coverage.

The production-public `openInterestHist` boundary was measured, not assumed.
For all four symbols the oldest returned 5-minute point was
`2026-06-26T12:00:00Z`; a request ending one millisecond earlier returned an
empty array. At the run anchor (`2026-07-26T11:57:39.786039Z`) that point was
29.998377 days old. Each symbol has 8,640 consecutive 5-minute points with
zero grid gaps, covering 179 complete boundary-pair eligible 4-hour epochs.

## Coverage

The 4-hour target window is
`[2026-06-14T08:00:00Z, 2026-07-26T08:00:00Z)`.
OI period end below is the close boundary of the last eligible epoch.

| symbol | source | stored rows/points | covered 4h epochs | period | 252 target |
|---|---|---:|---:|---|---|
| XRPUSDT | OFI / complete `klines` | 252 | 252 | 2026-06-14 08:00Z → 2026-07-26 08:00Z | 100% |
| XRPUSDT | `premiumIndexKlines` | 252 | 252 | 2026-06-14 08:00Z → 2026-07-26 08:00Z | 100% |
| XRPUSDT | `openInterestHist` 5m | 8,640 | 179 | 2026-06-26 12:00Z → 2026-07-26 08:00Z | retention-complete |
| DOGEUSDT | OFI / complete `klines` | 252 | 252 | 2026-06-14 08:00Z → 2026-07-26 08:00Z | 100% |
| DOGEUSDT | `premiumIndexKlines` | 252 | 252 | 2026-06-14 08:00Z → 2026-07-26 08:00Z | 100% |
| DOGEUSDT | `openInterestHist` 5m | 8,640 | 179 | 2026-06-26 12:00Z → 2026-07-26 08:00Z | retention-complete |
| SOLUSDT | OFI / complete `klines` | 252 | 252 | 2026-06-14 08:00Z → 2026-07-26 08:00Z | 100% |
| SOLUSDT | `premiumIndexKlines` | 252 | 252 | 2026-06-14 08:00Z → 2026-07-26 08:00Z | 100% |
| SOLUSDT | `openInterestHist` 5m | 8,640 | 179 | 2026-06-26 12:00Z → 2026-07-26 08:00Z | retention-complete |
| BTCUSDT | OFI / complete `klines` | 252 | 252 | 2026-06-14 08:00Z → 2026-07-26 08:00Z | 100% |
| BTCUSDT | `premiumIndexKlines` | 252 | 252 | 2026-06-14 08:00Z → 2026-07-26 08:00Z | 100% |
| BTCUSDT | `openInterestHist` 5m | 8,640 | 179 | 2026-06-26 12:00Z → 2026-07-26 08:00Z | retention-complete |

OFI follows T2.5 §3.2: taker buy base volume is kline row `[9]`, total base
volume is row `[5]`, taker sell is `total - taker buy`, and both sides must be
positive. No `aggTrades` backfill was performed.

OI boundary selection is ±5 minutes inclusive. A future combined consumer
selects the nearest qualifying live poll first and the nearest
`openInterestHist` backfill point only when no live point qualifies. When both
exist, relative difference over 1% raises an integrity flag while the live
value remains authoritative.

## Artifact and provenance

- DB:
  `/Users/mgh3326/work/herdr-artifacts/r4-p0-seed-backfill/r4_p0_backfill.sqlite3`
- DB rows: 36,576 exactly
  (`4 × (252 klines + 252 premium + 8,640 OI)`)
- DB SHA-256:
  `9eb6080a3f49102802ac3164a1a3f749d584bd607dea9afd02fe06cb319d67b8`
- Machine-readable report:
  `/Users/mgh3326/work/herdr-artifacts/r4-p0-seed-backfill/coverage_report.json`
- Report SHA-256:
  `babef7a54a91d55d7ab6d9400ec75515ba45994613f85e9d4bd30a093c6d5651`
- SQLite integrity, raw-payload hashes, and partition chains: all `ok`;
  bad hashes/links and missing PIT columns: 0.

The `pit_records` row schema is identical to the live collector. Backfill
provenance is independently visible through the separate filename, `.backfill`
source suffix, `r4-p0-seed-backfill.v1` collector version, `backfill:` run ID,
and immutable `artifact_metadata`. For backfill rows, `local_receive_time` is
the backfill HTTP response completion time, not a historical live receive
time.

## Live artifact non-interference

The backfill code never opened the live artifact. The live collector continued
to append during this work, so a whole-file hash is expected to change.
Integrity was therefore checked over an immutable prefix fixed before any
backfill work:

| check | before | after |
|---|---:|---:|
| immutable prefix | `append_id <= 45,240` | `append_id <= 45,240` |
| prefix rows | 45,240 | 45,240 |
| prefix logical SHA-256 | `464f5284ad8f15f0d1c1f0ea89063db1232a5cc7963d4872bc44fb4b3f015bde` | same |
| total live rows | 45,240 | 82,854 |
| `PRAGMA integrity_check` | `ok` | `ok` |

The initial whole-file SHA-256 was
`983f75872adb0b6eb94f4fe1809963ad59f8236aeb569a1ac731a0eec4d68dcc`;
the final was
`ed52eecb11036d23739201c2ec5533e00b984544f69056ed08dfd896f824390e`.
That physical change is attributable to the live collector's 37,614 appended
rows; the fixed logical prefix is byte-for-byte unchanged.

## Rate-limit use

The successful run made 84 HTTP 200 requests: 4 `klines`, 4
`premiumIndexKlines`, and 76 `openInterestHist`. Official IP weight was 16 of
2,400 per minute; observed shared-IP `x-mbx-used-weight-1m` ranged from 2 to 8.
OI used 76 of its separate 1,000 requests per 5 minutes.

Before the successful run, the first backward-window implementation safely
stopped at the retention boundary with HTTP 400 (`-1130`, invalid
`startTime`). Boundary diagnosis used three additional public OI GETs. Across
the complete task, including that stopped attempt and diagnosis:

- requests: 107 total (105 HTTP 200, 2 HTTP 400);
- official IP weight: 20;
- OI requests: 97 of 1,000 per 5 minutes;
- HTTP 429: 0;
- HTTP 418: 0;
- retries after 429/418: 0.

The 400 result was not rate limiting. It led to `endTime`-only reverse
pagination, which recovered the partial oldest page and then proved the
immediately older page empty for every symbol.

No account, signed, order, demo-fapi, production DB, forward-return, PnL, or
directional-hit path was used.
