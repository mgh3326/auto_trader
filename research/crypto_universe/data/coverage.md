# crypto-universe-v1 coverage

Built 2026-11-01T07:56:12.715432Z. Previous corpus (crypto-corpus-v1) delisted share: 0% (survivor-only).

| venue | window | pairs traded | live | delisted | delisted share | share excl. leveraged/stable | censored last trade |
|---|---|---|---|---|---|---|---|
| binance_usdt_spot | 2017-08-17 .. 2026-09-30 | 756 | 500 | 256 | 33.9% | 28.5% | 503 |
| upbit_krw | 2017-09-25 .. 2026-09-30 | 287 | 287 | 0 | 0.0% | 0.0% | 287 |

## binance_usdt_spot

- status counts: {'delisted': 256, 'live': 500, 'no_trades': 1}
- delisted pairs by year of last trade: {2018: 2, 2019: 3, 2020: 18, 2021: 20, 2022: 38, 2023: 24, 2024: 46, 2025: 51, 2026: 54}
- delisted pairs missing a last trade date: 0
- pairs with internal no-bar days: 8
- pairs with more than one listing episode (relisted or symbol reused): 15 ['BCHDOWNUSDT', 'BCHUPUSDT', 'CVCUSDT', 'ETHBEARUSDT', 'ETHBULLUSDT', 'FTTUSDT', 'KEYUSDT', 'LUNAUSDT', 'NBTUSDT', 'TUSDUSDT', 'USDCUSDT', 'USDPUSDT', 'USDSUSDT', 'VENUSDT', 'VIDTUSDT']
- fetch errors (pairs excluded): 0
- identical duplicate archive rows collapsed: 3
- exchangeInfo USDT pairs with no monthly archive yet: 19 {'TRADING': 19}

## upbit_krw

- status counts: {'live': 287, 'no_trades': 12}
- delisted pairs by year of last trade: {}
- delisted pairs missing a last trade date: 0
- pairs with internal no-bar days: 16
- pairs with more than one listing episode (relisted or symbol reused): 1 ['KRW-ARDR']
- fetch errors (pairs excluded): 0
- Upbit publishes no historical archive and its quotation API answers 404 for delisted markets, so delisted Upbit pairs are only obtainable when a probe returns candles.
- delisted probes: 544 markets, HTTP status counts {404: 544}, obtained [] (per-probe status and body sha256 in coverage.json)
  - known delisted KRW-LUNA: HTTP 404, candles 0
  - known delisted KRW-NPXS: HTTP 404, candles 0
