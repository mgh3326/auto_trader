from dataclasses import dataclass

from app.services.alpaca_paper_account_modes import account_mode_for_profile
from app.services.brokers.alpaca.endpoints import PAPER_TRADING_BASE_URL
from app.services.brokers.alpaca.exceptions import AlpacaPaperConfigurationError


@dataclass(frozen=True)
class AlpacaPaperSettings:
    api_key: str
    api_secret: str
    base_url: str = PAPER_TRADING_BASE_URL
    expected_account_id_suffix: str | None = None
    expected_account_number_suffix: str | None = None

    @classmethod
    def from_app_settings(cls, profile: str | None = None) -> "AlpacaPaperSettings":
        from app.core.config import settings

        account_mode = account_mode_for_profile(profile)
        if (
            account_mode == "alpaca_paper_crypto"
            and not settings.alpaca_paper_crypto_enabled
        ):
            raise AlpacaPaperConfigurationError(
                "ALPACA_PAPER_CRYPTO_ENABLED must be true for the clean route"
            )
        if account_mode == "alpaca_paper_lab":
            key = settings.alpaca_paper_lab_api_key
            secret_field = settings.alpaca_paper_lab_api_secret
            required_fields = (
                "alpaca_paper_lab_api_key and "
                "alpaca_paper_lab_api_secret must both be set"
            )
            expected_id = settings.alpaca_paper_lab_expected_account_id_suffix
            expected_number = settings.alpaca_paper_lab_expected_account_number_suffix
        elif account_mode == "alpaca_paper_crypto":
            key = settings.alpaca_paper_crypto_api_key
            secret_field = settings.alpaca_paper_crypto_api_secret
            required_fields = (
                "alpaca_paper_crypto_api_key and "
                "alpaca_paper_crypto_api_secret must both be set"
            )
            expected_id = settings.alpaca_paper_crypto_expected_account_id_suffix
            expected_number = (
                settings.alpaca_paper_crypto_expected_account_number_suffix
            )
        else:
            key = settings.alpaca_paper_api_key
            secret_field = settings.alpaca_paper_api_secret
            required_fields = (
                "alpaca_paper_api_key and alpaca_paper_api_secret must both be set"
            )
            expected_id = settings.alpaca_paper_expected_account_id_suffix
            expected_number = settings.alpaca_paper_expected_account_number_suffix
        secret = secret_field.get_secret_value() if secret_field is not None else None
        base_url = str(settings.alpaca_paper_base_url).rstrip("/")

        if not key or not secret:
            raise AlpacaPaperConfigurationError(required_fields)

        if account_mode == "alpaca_paper_crypto" and (
            not expected_id or not expected_number
        ):
            raise AlpacaPaperConfigurationError(
                "alpaca_paper_crypto expected account identity suffixes must both be set"
            )

        return cls(
            api_key=key,
            api_secret=secret,
            base_url=base_url,
            expected_account_id_suffix=expected_id,
            expected_account_number_suffix=expected_number,
        )
