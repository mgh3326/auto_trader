hk 6154 probe, 2026-11-24 12:17 KST (public, unauthenticated, from the worker host):

    GET https://stock.naver.com/api/domestic/market/stock/default
        ?tradeType=KRX&marketType=ALL&orderType=<up|down>&startIdx=0&pageSize=3

`orderType=down` -> HTTP 200, JSON list of stock objects (same shape as `up`),
ordered by `prevChangeRate` ascending (negative values). `low`, `lowTop`,
`downTop`, `fall` -> HTTP 400. Files are the raw response bodies (3 rows).
