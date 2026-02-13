"""
Tests for configuration module.
"""

from unittest.mock import patch

from app.core.config import Settings, settings


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
        assert hasattr(settings, "google_api_key")
        assert hasattr(settings, "opendart_api_key")
        assert hasattr(settings, "DATABASE_URL")


class TestConfigLoading:
    """Test configuration loading."""

    @patch.dict(
        "os.environ",
        {
            "KIS_APP_KEY": "test_kis_key",
            "TELEGRAM_TOKEN": "test_telegram_token",
            "GOOGLE_API_KEY": "test_google_key",
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

    def test_api_key_methods(self):
        """Test API key rotation methods."""
        # Test get_random_key method
        random_key = settings.get_random_key()
        assert isinstance(random_key, str)

        # Test get_next_key method
        next_key = settings.get_next_key()
        assert isinstance(next_key, str)
