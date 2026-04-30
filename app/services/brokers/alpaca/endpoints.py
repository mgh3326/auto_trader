PAPER_TRADING_BASE_URL = "https://paper-api.alpaca.markets"
DATA_BASE_URL = "https://data.alpaca.markets"
LIVE_TRADING_BASE_URL = "https://api.alpaca.markets"  # forbidden-value sentinel only

FORBIDDEN_TRADING_BASE_URLS: frozenset[str] = frozenset(
    {LIVE_TRADING_BASE_URL, DATA_BASE_URL}
)
