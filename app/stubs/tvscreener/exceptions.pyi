"""Type stubs for tvscreener exceptions."""

class MalformedRequestException(Exception):
    """Exception for malformed requests to TradingView API."""
    pass

class TvScreenerException(Exception):
    """Base exception for tvscreener errors."""
    pass

class RateLimitException(Exception):
    """Exception for rate limit errors."""
    pass

class TimeoutException(Exception):
    """Exception for timeout errors."""
    pass
