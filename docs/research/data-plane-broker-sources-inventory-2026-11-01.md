# Broker market-data sources for the data plane (ROB-1448)

Operator 2026-11-01: wherever a broker market-data API can serve the data
plane (`research/data_plane`, contract `data-plane-contract-v1.md`), use it
instead of Yahoo and other unofficial sources. Broker roles in this repo: KIS
for daily bars and real orders, Toss for batch current prices, Kiwoom for mock
only. This file lists what the repo's existing broker clients can read, as
implemented on `main` at 2434023 (code was read, no broker was called), and
what the data plane now uses.

## What the clients expose

All app broker clients are async and live under `app/services/brokers/`.

| broker | daily bars KR | daily bars US | adjusted vs raw | other read-only data | rate limit as implemented |
|---|---|---|---|---|---|
| KIS (`kis/client.py` `KISClient`) | `inquire_daily_itemchartprice_unclamped(code, market="J", n, adj, period, end_date)` → TR `FHKST03010100`, `/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice`; pages backwards in 150-day windows | `inquire_overseas_daily_price_unclamped(symbol, exchange_code, n, period, max_iterations, adjusted)` → TR `HHDFS76240000`, `/uapi/overseas-price/v1/quotations/dailyprice`; no end-date parameter (pages back from the latest bar with `BYMD`) | KR `FID_ORG_ADJ_PRC` 0 = adjusted, 1 = raw. US `MODP` 1 = adjusted, 0 = raw — **this PR adds the `adjusted` parameter** (it was hard-coded to 1; the default is unchanged) | current price, minute charts, investor flows; no dividend/split TR is implemented anywhere in `app/` | per-process sliding window per TR (`app/core/async_rate_limiter.py`, keyed `kis|<tr>|<path>`): daily chart 20/1 s, unmapped TRs (overseas daily) fall back to 19/1 s; per-process circuit breaker. Not shared across processes |
| Toss (`toss/client.py` `TossReadClient`) | `candles(symbol, interval="1d", count, before, adjusted)` → `GET /api/v1/candles` | same endpoint, US symbols in DB dot format | `adjusted=true/false` | `prices(symbols)` batch current price (1–200 symbols); `stocks(...)`/`stocks_all(...)` symbol master with list/delist dates; `warnings(symbol)` | `TossRateLimiter` per group: `MARKET_DATA_CHART` 5/1 s, `MARKET_DATA` 10/1 s, `STOCK` 5/1 s, `ORDER` 6/1 s (3/1 s 09:00–09:10 KST). Process-local by default; `TOSS_RATE_LIMITER_BACKEND=redis` shares the window across processes per client id |
| Kiwoom live (`kiwoom/live_market_data.py` `KiwoomLiveReadOnlyClient`) | `fetch_daily_chart` (ka10081), adjusted flag `upd_stkpc_tp` | — | yes | — | caller-paced (stage-2 collector: ≥ 0.5 s) |
| NHPLUG live quotes (`nhplug/live_quotes.py`) | `fetch_kr_period` | `fetch_us_period` | no adjusted flag | index/FX periods | caller-paced (≥ 0.2 s) |
| Upbit / Binance (crypto) | — | — | n/a | candles, market lists | public endpoints, already the crypto source |

Order capability: `KISClient` builds the domestic and overseas order clients
(`order_korea_stock`, `sell_korea_stock`, `cancel_korea_order`,
`modify_korea_order`, `order_overseas_stock`, `buy_overseas_stock`,
`sell_overseas_stock`, `cancel_overseas_order`, `modify_overseas_order`), and
importing anything under `app.services.brokers.kis` loads them and creates the
global `kis` client. `TossReadClient` has `place_order`, `modify_order` and
`cancel_order`. There is no market-data-only class that can run on its own.

## What the data plane uses

| dataset | KR | US | crypto |
|---|---|---|---|
| `universe` | KIS stock master files (public download, unchanged) | NASDAQ Trader symbol directory (exchange-operated, unchanged) | Binance exchangeInfo + Upbit market/all (unchanged) |
| `bars_1d` (raw) | **KIS** → Toss → Yahoo | **KIS** → Toss → Yahoo | Binance / Upbit public candles (unchanged) |
| `bars_1d_adjusted` (new) | **KIS** → Toss → Yahoo (derived) | **KIS** → Toss → Yahoo (derived) | — (no corporate actions) |
| `corporate_actions` | Yahoo dividends/splits + universe diff | Yahoo dividends/splits + universe diff | universe diff |

* **Source order and fallback.** A partition is served by the first source in
  `--bar-sources` (default `kis,toss,yahoo`) whose coverage reaches the floor.
  A broker that is not configured, whose client cannot be built, that fails 25
  instruments in a row before serving any, or that covers too little is
  recorded in the manifest (`source_attempts`), and the next source is tried.
  The manifest names the winner in `served_by`. One partition never mixes
  sources: two sources' adjustment conventions differ.
* **Not used, and why.** Kiwoom: mock only by the operator's broker roles, and
  bulk live collection is a separately approved stage 2
  (`docs/runbooks/kiwoom-live-readonly-marketdata.md`). NHPLUG: no adjusted
  flag and its own collector already exists; can be added as a raw-only source
  later. Toss `prices` (batch current price) is an intraday quote, not a closed
  daily bar. No broker client implements dividends or splits, so corporate
  actions stay on Yahoo; KIS `ksdinfo` TRs would be the broker replacement and
  need a client first.
* **No order endpoint is reachable.** `research/data_plane/broker_adapters.py`
  is the only module that imports app broker code. It imports `KISClient` and
  `TossReadClient` lazily (only on a broker-backed run) and wraps each in a
  view that answers only `ALLOWED_METHODS` — the two KIS daily charts, Toss
  `candles`, and close — and refuses every other attribute.
  `tests/research/data_plane/test_data_plane_broker_guards.py` pins the
  allowlist, the view, the single import site and the adapters' calls.
* **Rate budget.** The app's limiters are per process, so they cannot keep a
  separate research process from adding to serving's calls on the same app
  key. `research/data_plane/budget.py` caps research at a fixed share of each
  broker's documented per-key window: default KIS 2/1 s (10% of 20/1 s), Toss
  1/1 s (20% of the 5/1 s chart group), and never more than 25%
  (`--broker-budget` is refused above that). The window is kept in
  `<root>/.budget/<broker>.json` under `flock`, so the KR and US collectors and
  a manual rerun share one share. Serving keeps the rest of the window. The
  research calls also pass the app's own per-process limiter, and the Toss
  calls share serving's Redis window when `TOSS_RATE_LIMITER_BACKEND=redis`.
* **Tokens.** The KIS and Toss token managers are shared through Redis, so the
  collector reuses serving's token and never issues one that would replace it
  (Toss allows one valid token per client).

## Throughput at the default budget

Each instrument costs two broker calls a day (raw + adjusted vintage).

| market | instruments (approx.) | calls/day | at KIS 2/s |
|---|---|---|---|
| KR | 4,000 | 8,000 | ~67 min |
| US (common + ETF) | 7,500 | 15,000 | ~125 min |

The US DAG timeout is raised to 6 h to leave room for a fallback pass.

## Unverified on a live host

These follow the existing app code and docs but were not exercised against the
brokers in this PR (tests use fakes):

* the KIS overseas exchange mapping for NASDAQ Trader venues `NYSE American`,
  `NYSE Arca`, `Cboe BZX` → `AMEX` (an unmapped or wrong venue is that
  instrument's gap, and the coverage floor then decides the fallback);
* Toss daily candle timestamps: the bar date is taken from the timestamp's own
  offset, as `app/services/brokers/toss/candles.py` does;
* Toss KR symbols as plain 6-digit codes and US symbols in DB dot format, as
  the app's Toss fetchers pass them.
