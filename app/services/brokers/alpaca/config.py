from dataclasses import dataclass

from app.services.brokers.alpaca.endpoints import PAPER_TRADING_BASE_URL
from app.services.brokers.alpaca.exceptions import AlpacaPaperConfigurationError


@dataclass(frozen=True)
class AlpacaPaperSettings:
    api_key: str
    api_secret: str
    base_url: str = PAPER_TRADING_BASE_URL

    @classmethod
    def from_app_settings(cls) -> "AlpacaPaperSettings":
        from app.core.config import settings

        key = settings.alpaca_paper_api_key
        secret_field = settings.alpaca_paper_api_secret
        secret = secret_field.get_secret_value() if secret_field is not None else None
        base_url = str(settings.alpaca_paper_base_url).rstrip("/")

        if not key or not secret:
            raise AlpacaPaperConfigurationError(
                "alpaca_paper_api_key and alpaca_paper_api_secret must both be set"
            )

        return cls(api_key=key, api_secret=secret, base_url=base_url)
