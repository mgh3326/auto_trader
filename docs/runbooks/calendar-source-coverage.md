# Calendar Source Coverage & Follow-ups (ROB-167)

> Read-only diagnostic surface lives in
> `app/services/market_events/freshness_service.py` and is exposed via
> `GET /trading/api/market-events/coverage` plus the `meta.sourceFreshness`
> block on `GET /invest/api/calendar`. CLI: `python -m scripts.diagnose_calendar_coverage`.

## What we ingest today

| Source | Category | Market | Ingest entry point | Notes |
| --- | --- | --- | --- | --- |
| Finnhub | earnings | us | `app/services/market_events/finnhub_helpers.py::fetch_earnings_calendar_finnhub` | Per-day partition. EPS / revenue / fiscal period. `time_hint` = bmo/amc/dmh. |
| DART | disclosure (and `earnings` when title matches) | kr | `app/services/market_events/dart_helpers.py::fetch_dart_filings_for_date` | Uses `OpenDartReader.list_date`. Symbol from `stock_code`. Title-classified into earnings vs. disclosure via `normalize_dart_disclosure_row`. |
| ForexFactory | economic | global | `app/services/market_events/forexfactory_helpers.py::fetch_forexfactory_events_for_date` | This-week + next-week XML. Times converted ET → UTC. |

`scripts/ingest_market_events.py::SUPPORTED` is the canonical list of supported triples.
`app/services/market_events/expected_sources.py::EXPECTED_SOURCES` mirrors it.

## Gaps the spec calls out

These categories are **not** ingested today. Each row describes the gap, the recommended source, license/access notes, and the follow-up safety plan.

### KR market holidays

| Field | Value |
| --- | --- |
| Why we need it | Calendar shouldn't render "수집 실패" on KRX-closed days; we want an explicit "휴장" badge. |
| Current behavior | `expected_sources_for_date` only drops Sat/Sun; KRX observed holidays appear as `partial`/`error`. |
| Recommended source | KRX official holiday calendar (`http://open.krx.co.kr/contents/MMC/STAT/holiday/MMCSTAT003.cmd`) — public, daily HTML/JSON. Backup: Korea Exchange XLS export from KOFIA. |
| License | Public (KRX 공시 정보 — open). |
| Follow-up Linear | TBD: "ROB-XXX: ingest KRX holiday calendar into market_events as `category=holiday, market=kr`" |
| Safety | Ingestion only. Per-day partition. No broker/order side effects. |

### Dividends / ex-dividend dates

| Field | Value |
| --- | --- |
| Why we need it | Major dividend dates drive watchlist alerts; absent today. |
| Recommended source | Finnhub `stock/dividend2` endpoint (already key-authorised) for US; KRX `stock/divDistConfReq` for KR. |
| License | Finnhub: paid tier we already use; KRX: public. |
| Follow-up Linear | "ROB-XXX: ingest US + KR dividend calendars". |
| Safety | Read-only; per-symbol fetch; should be batched, not fanned out per request. |

### KR earnings schedule (forward-looking)

| Field | Value |
| --- | --- |
| Why we need it | DART only records *released* earnings (잠정실적 등); we don't have a forward earnings calendar for KR companies. |
| Recommended source | NAVER Finance "실적 발표 일정" or `Investing.com` KR earnings calendar (scraping); paid: WiseFn. |
| License | NAVER: scraping caveats — keep low-frequency; Investing.com: ToS limits scraping. WiseFn: paid contract required. |
| Follow-up Linear | "ROB-XXX: research forward KR earnings schedule data source (license review + spike)". |
| Safety | If scraping route taken, must respect robots.txt + rate limits. Add to ingestion partition table for retry visibility. |

### IPO / public offering schedule

| Field | Value |
| --- | --- |
| Why we need it | "이번 주 IPO" surface for Discover. |
| Recommended source | KRX 공시 (already in DART under specific report types: 증권신고서(지분증권), 투자설명서); we can add a dedicated normalizer that keys off `report_nm` patterns. US: SEC EDGAR S-1 filings. |
| License | DART/SEC: public. |
| Follow-up Linear | "ROB-XXX: extend DART normalizer with `category=ipo` keyword set; add SEC EDGAR S-1 ingestor". |
| Safety | Pure normalizer addition — no new external API. Lowest-risk follow-up; could be rolled into a dedicated PR. |

### Crypto major events

| Field | Value |
| --- | --- |
| Why we need it | Taxonomy already supports `crypto_exchange_notice`, `crypto_protocol`, `tokenomics`, `regulatory`; no source connected. |
| Recommended source | Upbit `notices` API + Bithumb `notice` API for KR; CoinMarketCal API (partner license) for global tokenomics events. |
| License | Upbit/Bithumb: public RSS-style endpoints. CoinMarketCal: API key + ToS review needed. |
| Follow-up Linear | "ROB-XXX: ingest Upbit + Bithumb notices into market_events; spike CoinMarketCal license". |
| Safety | Crypto sources only — no broker mutation. Crypto trading already paper-only behind safety boundary. |

## Causes-of-empty-day taxonomy (used by freshness service)

| Day state | Trigger | UI label |
| --- | --- | --- |
| `loaded` | All expected partitions succeeded with at least one event row | "최신" |
| `empty` | All expected partitions succeeded with zero rows | "일정 없음" |
| `partial` | Some expected partitions succeeded, others missing or running | "일부 수집 중" |
| `missing` | Zero partitions exist for the date | "미수집" |
| `error` | At least one expected partition is in `failed` state | "수집 실패" |
| `stale` | All expected partitions succeeded but newest `finished_at` is older than `STALE_AFTER_HOURS` (36h) | "오래된 데이터" |

## Timezone notes

* `MarketEvent.event_date` is stored in source-native day:
  * Finnhub: UTC (Finnhub publishes ISO date)
  * DART: KST date (parsed from `rcept_dt`)
  * ForexFactory: ET date (we filter rows whose ET-day matches the requested date)
* `release_time_utc` is the UTC point-in-time when available.
* `/invest/api/calendar` queries by `event_date` directly — there is no extra TZ shift in `build_calendar`. If the UI shows an event "on the wrong day," the right place to look is the source-side ET → UTC conversion in `forexfactory_helpers._parse_one_xml`, not the calendar assembler.

## Display-hiding caveats

* Per-day cluster collapse threshold: `CLUSTER_THRESHOLD = 10` and per-(eventType, market) groups > 5 collapse into a `CalendarCluster.topEvents[:5]` (see `app/services/invest_view_model/calendar_service.py`).
* Mobile per-day visible limit: `PER_DAY_VISIBLE_LIMIT = 8` with surplus surfaced as `hidden_count` (see `app/services/market_events/discover_calendar.py`).

When investigating "why isn't event X visible on day Y": check `dataState` first, then check whether it ended up inside a cluster.

## Operating safely

* **Do not** enable a recurring ingestion schedule from this PR — that is gated by ROB-128 follow-ups.
* **Do not** call live source APIs from CI. The diagnostic CLI talks to the DB only.
* When running the diagnostic CLI against production, no DB writes occur; the SQL is `SELECT ... FROM market_event_ingestion_partitions` + `SELECT count(*) FROM market_events`.
