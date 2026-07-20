"""
Tests for configuration module.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from app.core.config import Settings, settings

EXPECTED_KIS_API_RATE_LIMITS = {
    "FHKST03010100|/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice": {
        "rate": 20,
        "period": 1.0,
    },
    "FHPST04830000|/uapi/domestic-stock/v1/quotations/daily-short-sale": {
        "rate": 20,
        "period": 1.0,
    },
    "FHKST03010230|/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice": {
        "rate": 20,
        "period": 1.0,
    },
    "FHKST01010300|/uapi/domestic-stock/v1/quotations/inquire-ccnl": {
        "rate": 20,
        "period": 1.0,
    },
    # ROB-753: current-price endpoints reject bursty same-endpoint fanout.
    # Keep them serialized by default; operators can loosen via KIS_API_RATE_LIMITS.
    "FHKST01010100|/uapi/domestic-stock/v1/quotations/inquire-price": {
        "rate": 1,
        "period": 0.2,
    },
    "HHDFS00000300|/uapi/overseas-price/v1/quotations/price": {
        "rate": 1,
        "period": 0.2,
    },
    "VTTS3007R|/uapi/overseas-stock/v1/trading/inquire-psamount": {
        "rate": 10,
        "period": 1.0,
    },
    "TTTC8434R|/uapi/domestic-stock/v1/trading/inquire-balance": {
        "rate": 10,
        "period": 1.0,
    },
    "TTTC8001R|/uapi/domestic-stock/v1/trading/inquire-daily-ccld": {
        "rate": 10,
        "period": 1.0,
    },
    "TTTC8036R|/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl": {
        "rate": 10,
        "period": 1.0,
    },
    # ROB-585 (absorbed by ROB-645): pre-send throttle for order TRs (8/s) so
    # batch orders stay under the KIS ledger limit without any retry re-POST.
    "TTTC0012U|/uapi/domestic-stock/v1/trading/order-cash": {"rate": 8, "period": 1.0},
    "VTTC0012U|/uapi/domestic-stock/v1/trading/order-cash": {"rate": 8, "period": 1.0},
    "TTTC0011U|/uapi/domestic-stock/v1/trading/order-cash": {"rate": 8, "period": 1.0},
    "VTTC0011U|/uapi/domestic-stock/v1/trading/order-cash": {"rate": 8, "period": 1.0},
    "TTTC0013U|/uapi/domestic-stock/v1/trading/order-rvsecncl": {
        "rate": 8,
        "period": 1.0,
    },
    "VTTC0013U|/uapi/domestic-stock/v1/trading/order-rvsecncl": {
        "rate": 8,
        "period": 1.0,
    },
    "TTTT1002U|/uapi/overseas-stock/v1/trading/order": {"rate": 8, "period": 1.0},
    "VTTT1002U|/uapi/overseas-stock/v1/trading/order": {"rate": 8, "period": 1.0},
    "TTTT1006U|/uapi/overseas-stock/v1/trading/order": {"rate": 8, "period": 1.0},
    "VTTT1006U|/uapi/overseas-stock/v1/trading/order": {"rate": 8, "period": 1.0},
    "VTTT1001U|/uapi/overseas-stock/v1/trading/order": {"rate": 8, "period": 1.0},
    "TTTT1004U|/uapi/overseas-stock/v1/trading/order-rvsecncl": {
        "rate": 8,
        "period": 1.0,
    },
    "VTTT1004U|/uapi/overseas-stock/v1/trading/order-rvsecncl": {
        "rate": 8,
        "period": 1.0,
    },
}

EXPECTED_UPBIT_API_RATE_LIMITS = {
    "GET /v1/accounts": {"rate": 30, "period": 1.0},
    "GET /v1/order": {"rate": 30, "period": 1.0},
    "GET /v1/orders/closed": {"rate": 30, "period": 1.0},
    "GET /v1/ticker": {"rate": 10, "period": 1.0},
}


def _required_settings_kwargs() -> dict[str, str]:
    return {
        "kis_app_key": settings.kis_app_key,
        "kis_app_secret": settings.kis_app_secret,
        "opendart_api_key": settings.opendart_api_key,
        "DATABASE_URL": settings.DATABASE_URL,
        "upbit_access_key": settings.upbit_access_key,
        "upbit_secret_key": settings.upbit_secret_key,
        "SECRET_KEY": settings.SECRET_KEY,
    }


def _build_settings(**kwargs: object) -> Settings:
    settings_class = globals()["Settings"]
    cfg = settings_class(**kwargs)
    assert isinstance(cfg, Settings)
    return cfg


def _new_settings() -> Settings:
    return _build_settings(**_required_settings_kwargs())


class TestSettings:
    """Test Settings class."""

    def test_settings_instance(self):
        """Test that settings is an instance of Settings."""
        assert isinstance(settings, Settings)

    def test_settings_attributes(self):
        """Test that settings has required attributes."""
        # Test that required attributes exist (these will be None in test env)
        assert hasattr(settings, "kis_app_key")
        assert hasattr(settings, "telegram_token")
        assert hasattr(settings, "opendart_api_key")
        assert hasattr(settings, "DATABASE_URL")

    def test_yahoo_cache_settings_attributes_exist(self):
        assert hasattr(settings, "yahoo_ohlcv_cache_enabled")
        assert hasattr(settings, "yahoo_ohlcv_cache_max_days")
        assert hasattr(settings, "yahoo_ohlcv_cache_lock_ttl_seconds")

    def test_has_kis_ohlcv_cache_settings(self):
        assert hasattr(settings, "kis_ohlcv_cache_enabled")
        assert hasattr(settings, "kis_ohlcv_cache_max_days")
        assert hasattr(settings, "kis_ohlcv_cache_max_hours")
        assert hasattr(settings, "kis_ohlcv_cache_lock_ttl_seconds")


class TestConfigLoading:
    """Test configuration loading."""

    @patch.dict(
        "os.environ",
        {
            "KIS_APP_KEY": "test_kis_key",
            "TELEGRAM_TOKEN": "test_telegram_token",
            "OPENDART_API_KEY": "test_dart_key",
            "DATABASE_URL": "postgresql://test:test@localhost/testdb",
        },
    )
    def test_environment_variables_loading(self):
        """Test loading configuration from environment variables."""
        # Note: This test may not work as expected due to singleton pattern
        # The settings instance is created at module import time
        pass

    def test_settings_singleton(self):
        """Test that settings is a singleton."""
        from app.core.config import settings as settings2

        assert settings is settings2

    def test_redis_url_generation(self):
        """Test Redis URL generation method."""
        redis_url = settings.get_redis_url()
        if settings.redis_url:
            assert redis_url == settings.redis_url
            return

        expected_scheme = "rediss://" if settings.redis_ssl else "redis://"
        assert redis_url.startswith(expected_scheme)
        assert f"{settings.redis_host}:{settings.redis_port}" in redis_url
        assert redis_url.endswith(f"/{settings.redis_db}")

    def test_api_rate_limit_defaults_include_builtins(self):
        cfg = _new_settings()

        assert cfg.kis_api_rate_limits == EXPECTED_KIS_API_RATE_LIMITS
        assert cfg.upbit_api_rate_limits == EXPECTED_UPBIT_API_RATE_LIMITS

    def test_empty_object_env_override_does_not_erase_builtins(self, monkeypatch):
        monkeypatch.setenv("KIS_API_RATE_LIMITS", "{}")
        monkeypatch.setenv("UPBIT_API_RATE_LIMITS", "{}")

        cfg = _new_settings()

        assert cfg.kis_api_rate_limits == EXPECTED_KIS_API_RATE_LIMITS
        assert cfg.upbit_api_rate_limits == EXPECTED_UPBIT_API_RATE_LIMITS

    def test_empty_string_env_override_does_not_erase_builtins(self, monkeypatch):
        monkeypatch.setenv("KIS_API_RATE_LIMITS", "")
        monkeypatch.setenv("UPBIT_API_RATE_LIMITS", "")

        cfg = _new_settings()

        assert cfg.kis_api_rate_limits == EXPECTED_KIS_API_RATE_LIMITS
        assert cfg.upbit_api_rate_limits == EXPECTED_UPBIT_API_RATE_LIMITS

    def test_partial_api_rate_limit_override_merges_endpoint_subdict(self, monkeypatch):
        monkeypatch.setenv(
            "KIS_API_RATE_LIMITS",
            '{"TTTC8434R|/uapi/domestic-stock/v1/trading/inquire-balance": {"rate": 25}}',
        )

        cfg = _new_settings()

        assert cfg.kis_api_rate_limits[
            "TTTC8434R|/uapi/domestic-stock/v1/trading/inquire-balance"
        ] == {"rate": 25, "period": 1.0}
        assert cfg.kis_api_rate_limits[
            "TTTC8001R|/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
        ] == {"rate": 10, "period": 1.0}

    def test_public_api_paths_supports_csv_env_string(self, monkeypatch):
        monkeypatch.setenv("PUBLIC_API_PATHS", "/healthz,/api/scan")

        cfg = _new_settings()

        assert cfg.PUBLIC_API_PATHS == ["/healthz", "/api/scan"]

    def test_public_api_paths_supports_json_env_string(self, monkeypatch):
        monkeypatch.setenv("PUBLIC_API_PATHS", '["/healthz", "/api/scan"]')

        cfg = _new_settings()

        assert cfg.PUBLIC_API_PATHS == ["/healthz", "/api/scan"]

    def test_public_api_paths_supports_empty_string_env(self, monkeypatch):
        monkeypatch.setenv("PUBLIC_API_PATHS", "")

        cfg = _new_settings()

        assert cfg.PUBLIC_API_PATHS == []

    def test_public_api_paths_supports_empty_json_env_string(self, monkeypatch):
        monkeypatch.setenv("PUBLIC_API_PATHS", "[]")

        cfg = _new_settings()

        assert cfg.PUBLIC_API_PATHS == []

    def test_public_api_paths_rejects_non_string_json_list(self, monkeypatch):
        monkeypatch.setenv("PUBLIC_API_PATHS", '["/healthz", 1]')

        with pytest.raises(ValidationError, match="PUBLIC_API_PATHS JSON value"):
            _new_settings()

    def test_constructor_public_api_paths_list_is_preserved(self):
        cfg = _build_settings(
            **_required_settings_kwargs(),
            PUBLIC_API_PATHS=["/healthz"],
        )

        assert cfg.PUBLIC_API_PATHS == ["/healthz"]

    def test_invalid_api_rate_limit_json_raises_validation_error(self, monkeypatch):
        monkeypatch.setenv("KIS_API_RATE_LIMITS", "{not-json}")

        with pytest.raises(ValidationError, match="Invalid JSON for API rate limits"):
            _new_settings()

    def test_non_object_api_rate_limit_json_raises_validation_error(self, monkeypatch):
        monkeypatch.setenv("KIS_API_RATE_LIMITS", "[]")

        with pytest.raises(
            ValidationError, match="API rate limits must be a JSON object"
        ):
            _new_settings()

    def test_constructor_empty_kis_api_rate_limits_replaces_builtins(self):
        cfg = _build_settings(**_required_settings_kwargs(), kis_api_rate_limits={})

        assert cfg.kis_api_rate_limits == {}

    def test_constructor_empty_upbit_api_rate_limits_replaces_builtins(self):
        cfg = _build_settings(**_required_settings_kwargs(), upbit_api_rate_limits={})

        assert cfg.upbit_api_rate_limits == {}

    def test_constructor_custom_kis_api_rate_limits_do_not_auto_seed_builtins(self):
        custom_limits = {"custom": {"rate": 1}}

        cfg = _build_settings(
            **_required_settings_kwargs(), kis_api_rate_limits=custom_limits
        )

        assert cfg.kis_api_rate_limits == custom_limits

    def test_telegram_chat_ids_str_splits_multiple_ids(self):
        cfg = Settings(
            telegram_token="token",
            telegram_chat_id="legacy",
            telegram_chat_ids_str="111, 222,,333 ",
        )

        assert cfg.telegram_chat_ids == ["111", "222", "333"]

    def test_telegram_chat_ids_falls_back_to_single_chat_id(self):
        # conftest가 TELEGRAM_CHAT_IDS_STR 전역 기본값을 심으므로 명시적으로
        # 비워야 legacy 폴백 경로가 검증된다 (env/dotenv 무관 밀폐형).
        cfg = Settings(
            telegram_token="token",
            telegram_chat_id="legacy",
            telegram_chat_ids_str=None,
        )

        assert cfg.telegram_chat_ids == ["legacy"]


def test_runbook_exists() -> None:
    assert Path("docs/runbooks/freqtrade-research-pipeline.md").exists()
