"""Recorded-shape fixtures. Shapes follow the Binance docs (values synthetic):

* partial depth / bookTicker / aggTrade:
  https://developers.binance.com/en/docs/products/spot/web-socket-streams
* user data events:
  https://developers.binance.com/en/docs/products/spot/user-data-stream
* WebSocket API request/response/push envelopes:
  https://developers.binance.com/en/docs/products/spot/web-socket-api
  (subscribe response example: {"id": ..., "status": 200, "result": {"subscriptionId": 0}})
"""

import json

DEPTH = json.dumps(
    {
        "stream": "btcusdt@depth20",
        "data": {
            "lastUpdateId": 160,
            "bids": [
                ["67000.01000000", "1.50000000"],
                ["67000.00000000", "0.20000000"],
            ],
            "asks": [
                ["67000.02000000", "0.75000000"],
                ["67000.03000000", "3.00000000"],
            ],
        },
    }
)
BOOK_TICKER = json.dumps(
    {
        "stream": "btcusdt@bookTicker",
        "data": {
            "u": 400900217,
            "s": "BTCUSDT",
            "b": "67000.01000000",
            "B": "1.50000000",
            "a": "67000.02000000",
            "A": "0.75000000",
        },
    }
)
AGG_TRADE = json.dumps(
    {
        "stream": "btcusdt@aggTrade",
        "data": {
            "e": "aggTrade",
            "E": 1672515782136,
            "s": "BTCUSDT",
            "a": 12345,
            "p": "67000.02000000",
            "q": "0.01000000",
            "f": 100,
            "l": 105,
            "T": 1672515782136,
            "m": True,
            "M": True,
        },
    }
)
SUBSCRIBE_OK = json.dumps({"id": "x", "status": 200, "result": {"subscriptionId": 0}})
SUBSCRIBE_REJECTED = json.dumps(
    {
        "id": "x",
        "status": 400,
        "error": {"code": -1022, "msg": "Signature for this request is not valid."},
    }
)
EXECUTION_REPORT = json.dumps(
    {
        "subscriptionId": 0,
        "event": {
            "e": "executionReport",
            "E": 1672515782200,
            "s": "BTCUSDT",
            "c": "mock-abc-1",
            "S": "BUY",
            "o": "LIMIT",
            "f": "GTC",
            "q": "0.01000000",
            "p": "67000.01000000",
            "x": "TRADE",
            "X": "PARTIALLY_FILLED",
            "i": 4293153,
            "l": "0.00400000",
            "z": "0.00400000",
            "L": "67000.01000000",
            "t": 999,
            "T": 1672515782190,
        },
    }
)
ACCOUNT_POSITION = json.dumps(
    {
        "subscriptionId": 0,
        "event": {
            "e": "outboundAccountPosition",
            "E": 1672515782210,
            "u": 1672515782209,
            "B": [{"a": "USDT", "f": "9000.00000000", "l": "10.00000000"}],
        },
    }
)
LIST_STATUS = json.dumps({"subscriptionId": 0, "event": {"e": "listStatus", "E": 1}})
SERVER_SHUTDOWN = json.dumps(
    {"subscriptionId": 0, "event": {"e": "serverShutdown", "E": 1, "T": 2}}
)
