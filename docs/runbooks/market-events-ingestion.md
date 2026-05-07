# Market Events Ingestion Foundation (ROB-128)

> Foundation PR. No Prefect schedule, no production backfill, no broker mutation.

## What this is

A per-day, idempotent ingestion pipeline for **market-wide events** (US earnings via
Finnhub, KR DART disclosures, with crypto event taxonomy ready for follow-up sources).
Drives the future "오늘의 이벤트" surface on `/invest/app`.

## Tables

* `market_events` — one row per scheduled / released event. Public schema.
* `market_event_values` — metric-level numeric data (eps, revenue, cpi, …).
* `market_event_ingestion_partitions` — per source/category/market/day state, so failed
  days are visible and retryable rather than silently skipped.

All writes go through `app/services/market_events/repository.py::MarketEventsRepository`.

## Idempotency

* Events with `source_event_id` (e.g. DART `rcept_no`) upsert on
  `(source, category, market, source_event_id)`.
* Events without (e.g. Finnhub earnings rows) upsert on
  `(source, category, market, symbol, event_date, fiscal_year, fiscal_quarter)`.
* Values upsert on `(event_id, metric_name, period)`.

Both keys are partial unique indexes — see migration
`alembic/versions/a7e9c128_add_market_events_tables.py`.

## CLI

```bash
# US earnings, one-day partitions, range looped internally
uv run python -m scripts.ingest_market_events \
  --source finnhub --category earnings --market us \
  --from-date 2026-05-07 --to-date 2026-05-14

# KR disclosures
uv run python -m scripts.ingest_market_events \
  --source dart --category disclosure --market kr \
  --from-date 2026-05-07 --to-date 2026-05-07

# Dry run (prints planned partitions, no DB writes)
uv run python -m scripts.ingest_market_events \
  --source finnhub --category earnings --market us \
  --from-date 2026-05-07 --to-date 2026-05-14 --dry-run
```

Recommended rolling window for the future Prefect schedule:
**today - 7 days through today + 60 days.**

## Read API

* `GET /trading/api/market-events/today?on_date=YYYY-MM-DD&category=&market=&source=`
* `GET /trading/api/market-events/range?from_date=YYYY-MM-DD&to_date=YYYY-MM-DD&...`

Both return `MarketEventResponse` items including `held` and `watched` placeholder
flags (currently always `null` — see follow-ups).

## Env vars

| Var | Purpose | Already in `app/core/config.py`? |
| --- | --- | --- |
| `FINNHUB_API_KEY` | Finnhub earnings calendar | yes |
| `OPENDART_API_KEY` | DART disclosures | yes |

No new env vars introduced by this PR. Tests stub both with `DUMMY_*` values.

## Safety

* `raw_payload_json` columns are passed through `_redact_sensitive_keys` before write.
* No broker / order / watch / scheduling side effects.
* Failures record `partition.status = "failed"` with the error message and increment
  `retry_count`. The partition row is the canonical retry surface.
* Tests use `monkeypatch.setattr(...)` against `_fetch_earnings_calendar_finnhub` and
  the injected `fetch_rows` callable — never live API calls by default.

## Tests

```bash
uv run pytest tests/services/test_market_events_models.py -v
uv run pytest tests/services/test_market_events_taxonomy.py -v
uv run pytest tests/services/test_market_events_schemas.py -v
uv run pytest tests/services/test_market_events_normalizers.py -v
uv run pytest tests/services/test_market_events_repository.py -v
uv run pytest tests/services/test_market_events_ingestion.py -v
uv run pytest tests/services/test_market_events_query_service.py -v
uv run pytest tests/test_market_events_router.py -v
uv run pytest tests/test_market_events_cli.py -v

uv run ruff check .
uv run ruff format --check .
```

The DB-backed integration tests require Postgres at the test `DATABASE_URL`.

## Follow-ups (out of scope for this PR)

1. **Prefect deployment** for the rolling window. The CLI exposes a stable boundary
   (`scripts.ingest_market_events.run_ingest`) for the flow to call.
2. **Holdings / watchlist join** to populate `held` / `watched` flags. Today
   `MarketEventsQueryService` returns `None` for both. The expected surfaces are:
   * `held` ← `manual_holdings.ticker = market_events.symbol` filtered to the
     authenticated user's `broker_account_id`.
   * `watched` ← `user_watch_items.instrument_id` joined via `instruments.symbol =
     market_events.symbol`.
3. **Crypto sources** — the taxonomy already supports `crypto_exchange_notice`,
   `crypto_protocol`, `tokenomics`, `regulatory`. Implement Upbit / Bithumb /
   Binance notice fetchers as additional `ingest_*_for_date` functions and add
   them to `SUPPORTED` in `scripts/ingest_market_events.py`.
4. **Economic calendar** (`category="economic"`) — same shape, different source.
5. **`/invest/app` UI card** consuming the `today` endpoint.

## Economic events (ForexFactory, ROB-132)

ForexFactory weekly XML feeds are parsed per day and ingested as
`(source=forexfactory, category=economic, market=global)` rows.

### CLI

```bash
uv run python -m scripts.ingest_market_events \
  --source forexfactory --category economic --market global \
  --from-date 2026-05-13 --to-date 2026-05-13 --dry-run
```

Adds `currency` column to `market_events` (Alembic revision `c1a2b3d4`) so each
row records the affected currency (USD/EUR/JPY/...).

### Idempotency

`source_event_id` is derived as `f"ff::{currency}::{title}::{utc_iso_or_date}"` so
repeated ingestion of the same release upserts on `(source, category, market,
source_event_id)`. Times are converted from ET to UTC for storage; the original
ET wall-clock is kept on `release_time_local` and `source_timezone =
"America/New_York"`.

### Values

Forecast/previous/actual numeric values are stored on `market_event_values` with
`metric_name="actual"` and the inferred unit (e.g. `%`, `K`, `M`). When all
three are blank, no value row is written.

### UI

`/invest/app` Discover `TodayEventCard` consumes
`GET /trading/api/market-events/today` and filters client-side by `category`
into 전체 / 경제지표 / 실적 tabs.

### Open follow-ups specific to economic events

- Hermes-side production `--dry-run` smoke from a deployed runner before any
  non-dry-run ingestion.
- Prefect deployment for the rolling window (today-7 .. today+60).
- Joining `held` / `watched` flags is still a global ROB-128 follow-up.

## Handoff (when this PR is opened)

Include in the PR description / Linear comment:

* branch name + PR URL
* `alembic/versions/a7e9c128_add_market_events_tables.py` (migration filename)
* CLI invocation examples (above)
* tests + lint commands run, with output
* whether any live API calls were used (default: no)
* required env vars (above), with values redacted
* production migration / backfill cautions
* exact follow-up tasks for Hermes / Prefect (above)
