class AlpacaPaperConfigurationError(Exception):
    """Raised when paper-trading settings are invalid or missing credentials."""


class AlpacaPaperEndpointError(Exception):
    """Raised when a forbidden or non-paper endpoint is used as the trading base URL."""


class AlpacaPaperRequestError(Exception):
    """Raised when an HTTP request to the Alpaca paper API fails."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
