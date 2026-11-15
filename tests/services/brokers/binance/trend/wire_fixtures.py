"""Raw Binance USD-M Futures response bodies the trend execution path reads.

Each constant is the JSON **text** of a response, so the contract tests push
bytes through ``json.loads`` exactly as a real HTTP response would (numbers that
arrive as strings stay strings, ``true`` stays a boolean, absent fields stay
absent). Nothing here is a live capture: the shapes follow the documented
examples at the URLs below, adapted to MARKET/one-way (``positionSide: BOTH``)
and to the symbols this lane trades. Existing repo expectations that agree with
them: tests/services/brokers/binance/futures_demo/test_execution_client_*.py
and tests/services/brokers/binance/h5/test_single_asset_margin_gate.py.

Docs (USDS-M Futures REST):
- New Order        https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api
- Query Order      https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/Query-Order
- Position Info V2 https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/Position-Information-V2
- Position Mode    https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/Get-Current-Position-Mode
- Open Orders      https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/Current-All-Open-Orders
- Exchange Info    https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Exchange-Information
- Book Ticker      https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Symbol-Order-Book-Ticker
- Klines           https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Kline-Candlestick-Data
- Error codes      https://developers.binance.com/docs/derivatives/usds-margined-futures/error-code
"""

from __future__ import annotations

# POST /fapi/v1/order (MARKET entry, one-way). Prices are strings, ids integers.
NEW_ORDER_NEW = """{
  "orderId": 22542179,
  "symbol": "BTCUSDT",
  "status": "NEW",
  "clientOrderId": "trend-abc",
  "price": "0.00",
  "avgPrice": "0.00000",
  "origQty": "0.600",
  "executedQty": "0.000",
  "cumQty": "0.000",
  "cumQuote": "0.00000",
  "timeInForce": "GTC",
  "type": "MARKET",
  "reduceOnly": false,
  "closePosition": false,
  "side": "BUY",
  "positionSide": "BOTH",
  "stopPrice": "0.00",
  "workingType": "CONTRACT_PRICE",
  "priceProtect": false,
  "origType": "MARKET",
  "priceMatch": "NONE",
  "selfTradePreventionMode": "EXPIRE_MAKER",
  "goodTillDate": 0,
  "updateTime": 1766000000123
}"""

# The same order answered as already filled (a MARKET order often is).
NEW_ORDER_FILLED = (
    NEW_ORDER_NEW.replace('"status": "NEW"', '"status": "FILLED"')
    .replace('"executedQty": "0.000"', '"executedQty": "0.600"')
    .replace('"avgPrice": "0.00000"', '"avgPrice": "99874.50000"')
)

# A reduceOnly close (SELL), as answered by the broker.
NEW_ORDER_CLOSE = (
    NEW_ORDER_NEW.replace('"side": "BUY"', '"side": "SELL"')
    .replace('"reduceOnly": false', '"reduceOnly": true')
    .replace('"trend-abc"', '"trend-close"')
)

# Same response without ``positionSide`` (absent field): the client must not
# treat silence as agreement.
NEW_ORDER_NO_POSITION_SIDE = NEW_ORDER_NEW.replace('  "positionSide": "BOTH",\n', "")

# GET /fapi/v1/order (query by origClientOrderId): adds ``time``.
QUERY_ORDER_FILLED = """{
  "avgPrice": "99874.50000",
  "clientOrderId": "trend-abc",
  "cumQuote": "59924.70000",
  "executedQty": "0.600",
  "orderId": 22542179,
  "origQty": "0.600",
  "origType": "MARKET",
  "price": "0.00",
  "reduceOnly": false,
  "side": "BUY",
  "positionSide": "BOTH",
  "status": "FILLED",
  "closePosition": false,
  "symbol": "BTCUSDT",
  "time": 1766000000100,
  "timeInForce": "GTC",
  "type": "MARKET",
  "updateTime": 1766000000123,
  "workingType": "CONTRACT_PRICE",
  "priceProtect": false
}"""

QUERY_ORDER_PARTIAL = QUERY_ORDER_FILLED.replace(
    '"status": "FILLED"', '"status": "PARTIALLY_FILLED"'
).replace('"executedQty": "0.600"', '"executedQty": "0.300"')

# DELETE /fapi/v1/order?symbol=&origClientOrderId= (cancel the remainder of a
# working entry). Binance answers with the cancelled order: the executed
# quantity of the partial fill stays part of the position.
CANCEL_ORDER_CANCELED = """{
  "avgPrice": "99874.50000",
  "clientOrderId": "trend-abc",
  "cumQuote": "19974.90000",
  "executedQty": "0.200",
  "orderId": 22542179,
  "origQty": "0.600",
  "origType": "MARKET",
  "price": "0.00",
  "reduceOnly": false,
  "side": "BUY",
  "positionSide": "BOTH",
  "status": "CANCELED",
  "closePosition": false,
  "symbol": "BTCUSDT",
  "time": 1766000000100,
  "timeInForce": "GTC",
  "type": "MARKET",
  "updateTime": 1766000000999,
  "workingType": "CONTRACT_PRICE",
  "priceProtect": false
}"""

# A cancel answer that does not name the order it cancelled: never trusted.
CANCEL_ORDER_NO_ECHO = CANCEL_ORDER_CANCELED.replace(
    '  "clientOrderId": "trend-abc",\n', ""
)

# GET /fapi/v2/positionRisk (all symbols, one-way). Note the string flags
# (``isAutoAddMargin: "false"``) and numeric-looking strings everywhere.
POSITION_RISK_ALL = """[
  {
    "symbol": "BTCUSDT",
    "positionAmt": "0.600",
    "entryPrice": "99874.5000000",
    "breakEvenPrice": "99924.4000000",
    "markPrice": "99890.12345678",
    "unRealizedProfit": "9.37407",
    "liquidationPrice": "0",
    "leverage": "1",
    "maxNotionalValue": "5.0E7",
    "marginType": "isolated",
    "isolatedMargin": "59924.70000000",
    "isAutoAddMargin": "false",
    "positionSide": "BOTH",
    "notional": "59934.07407",
    "isolatedWallet": "59915.32593",
    "updateTime": 1766000000200,
    "adlQuantile": 1
  },
  {
    "symbol": "ETHUSDT",
    "positionAmt": "0.000",
    "entryPrice": "0.0",
    "breakEvenPrice": "0.0",
    "markPrice": "3120.45000000",
    "unRealizedProfit": "0.00000000",
    "liquidationPrice": "0",
    "leverage": "1",
    "maxNotionalValue": "5.0E7",
    "marginType": "isolated",
    "isolatedMargin": "0.00000000",
    "isAutoAddMargin": "false",
    "positionSide": "BOTH",
    "notional": "0",
    "isolatedWallet": "0",
    "updateTime": 0,
    "adlQuantile": 0
  },
  {
    "symbol": "SOLUSDT",
    "positionAmt": "-12.5",
    "entryPrice": "181.2300000",
    "breakEvenPrice": "181.1000000",
    "markPrice": "181.00000000",
    "unRealizedProfit": "2.87500000",
    "liquidationPrice": "0",
    "leverage": "1",
    "maxNotionalValue": "2.5E7",
    "marginType": "isolated",
    "isolatedMargin": "2265.37500000",
    "isAutoAddMargin": "false",
    "positionSide": "BOTH",
    "notional": "-2262.50000000",
    "isolatedWallet": "2268.25000000",
    "updateTime": 1766000000300,
    "adlQuantile": 0
  },
  {
    "symbol": "XRPUSDT",
    "positionAmt": "0",
    "entryPrice": "0.0",
    "breakEvenPrice": "0.0",
    "markPrice": "2.31000000",
    "unRealizedProfit": "0.00000000",
    "liquidationPrice": "0",
    "leverage": "1",
    "maxNotionalValue": "5.0E7",
    "marginType": "cross",
    "isolatedMargin": "0.00000000",
    "isAutoAddMargin": "false",
    "positionSide": "BOTH",
    "notional": "0",
    "isolatedWallet": "0",
    "updateTime": 0,
    "adlQuantile": 0
  }
]"""

# Hedge-mode account: the same endpoint answers LONG/SHORT rows.
POSITION_RISK_HEDGE = POSITION_RISK_ALL.replace(
    '"positionSide": "BOTH"', '"positionSide": "LONG"', 1
)

# GET /fapi/v1/positionSide/dual
POSITION_MODE_ONE_WAY = '{"dualSidePosition": false}'
POSITION_MODE_HEDGE = '{"dualSidePosition": true}'
POSITION_MODE_ONE_WAY_STRING = (
    '{"dualSidePosition": "false"}'  # defensive: string spelling
)
POSITION_MODE_HEDGE_STRING = '{"dualSidePosition": "true"}'

# GET /fapi/v1/openOrders
OPEN_ORDERS_ONE = """[
  {
    "avgPrice": "0.00000",
    "clientOrderId": "web_7aQ2",
    "cumQuote": "0",
    "executedQty": "0",
    "orderId": 1917641,
    "origQty": "0.40",
    "origType": "LIMIT",
    "price": "9000",
    "reduceOnly": false,
    "side": "BUY",
    "positionSide": "BOTH",
    "status": "NEW",
    "stopPrice": "0",
    "closePosition": false,
    "symbol": "BTCUSDT",
    "time": 1579276756075,
    "timeInForce": "GTC",
    "type": "LIMIT",
    "updateTime": 1579276756075,
    "workingType": "CONTRACT_PRICE",
    "priceProtect": false
  }
]"""
OPEN_ORDERS_NONE = "[]"

# GET /fapi/v1/exchangeInfo?symbol=BTCUSDT
EXCHANGE_INFO_BTC = """{
  "timezone": "UTC",
  "serverTime": 1766000000000,
  "rateLimits": [],
  "exchangeFilters": [],
  "symbols": [
    {
      "symbol": "BTCUSDT",
      "pair": "BTCUSDT",
      "contractType": "PERPETUAL",
      "status": "TRADING",
      "baseAsset": "BTC",
      "quoteAsset": "USDT",
      "pricePrecision": 2,
      "quantityPrecision": 3,
      "baseAssetPrecision": 8,
      "quotePrecision": 8,
      "filters": [
        {"filterType": "PRICE_FILTER", "minPrice": "261.10", "maxPrice": "809484", "tickSize": "0.10"},
        {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "1000", "stepSize": "0.001"},
        {"filterType": "MARKET_LOT_SIZE", "minQty": "0.001", "maxQty": "120", "stepSize": "0.001"},
        {"filterType": "MAX_NUM_ORDERS", "limit": 200},
        {"filterType": "MIN_NOTIONAL", "notional": "100"},
        {"filterType": "PERCENT_PRICE", "multiplierUp": "1.0500", "multiplierDown": "0.9500", "multiplierDecimal": "4"}
      ],
      "orderTypes": ["LIMIT", "MARKET"],
      "timeInForce": ["GTC", "IOC", "FOK", "GTX"]
    }
  ]
}"""

# Documented-valid: a zero market minQty means "no minimum for market orders".
EXCHANGE_INFO_ZERO_MARKET_MIN = EXCHANGE_INFO_BTC.replace(
    '{"filterType": "MARKET_LOT_SIZE", "minQty": "0.001"',
    '{"filterType": "MARKET_LOT_SIZE", "minQty": "0.00000000"',
)

# GET /fapi/v1/ticker/bookTicker?symbol=BTCUSDT
BOOK_TICKER_BTC = """{
  "lastUpdateId": 1027024,
  "symbol": "BTCUSDT",
  "bidPrice": "99890.10",
  "bidQty": "31.211",
  "askPrice": "99890.20",
  "askQty": "40.666",
  "time": 1766000000050
}"""

# Errors (HTTP 4xx/5xx bodies)
ERROR_MARGIN_INSUFFICIENT = '{"code": -2019, "msg": "Margin is insufficient."}'
ERROR_ORDER_NOT_FOUND = '{"code": -2013, "msg": "Order does not exist."}'
ERROR_REDUCE_ONLY = '{"code": -2022, "msg": "ReduceOnly Order is rejected."}'
ERROR_SEND_STATUS_UNKNOWN = (
    '{"code": -1007, "msg": "Timeout waiting for response from backend server. '
    'Send status unknown; execution status unknown."}'
)
ERROR_TOO_MANY_REQUESTS = '{"code": -1003, "msg": "Too many requests."}'


def klines(rows: list[tuple[int, str]]) -> str:
    """``/fapi/v1/klines`` rows: ints for times/counts, strings for every price."""
    body = []
    for open_ts, price in rows:
        body.append(
            f'[{open_ts},"{price}","{price}","{price}","{price}","12.345",'
            f'{open_ts + 59999},"1234.5",42,"6.1","610.0","0"]'
        )
    return "[" + ",".join(body) + "]"
