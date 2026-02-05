import pytest

from app.mcp_server.env_utils import _env, _env_int, get_finnhub_api_key


@pytest.mark.unit
class TestEnv:
    def test_returns_env_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_VAR", "value")
        assert _env("TEST_VAR") == "value"

    def test_returns_default_when_not_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TEST_VAR", raising=False)
        assert _env("TEST_VAR", "default") == "default"

    def test_returns_default_when_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_VAR", "")
        assert _env("TEST_VAR", "default") == "default"


@pytest.mark.unit
class TestEnvInt:
    def test_returns_parsed_int(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_PORT", "8080")
        assert _env_int("TEST_PORT", 3000) == 8080

    def test_returns_default_when_not_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TEST_PORT", raising=False)
        assert _env_int("TEST_PORT", 3000) == 3000

    def test_returns_default_when_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_PORT", "")
        assert _env_int("TEST_PORT", 3000) == 3000

    def test_returns_default_on_invalid_value(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("TEST_PORT", "not-a-number")
        result = _env_int("TEST_PORT", 3000)
        assert result == 3000
        assert "Invalid integer" in caplog.text
        assert "TEST_PORT" in caplog.text


@pytest.mark.unit
class TestGetFinnhubApiKey:
    def test_returns_api_key_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FINNHUB_API_KEY", "test_api_key_123")
        assert get_finnhub_api_key() == "test_api_key_123"

    def test_returns_none_when_not_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
        assert get_finnhub_api_key() is None

    def test_returns_none_when_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FINNHUB_API_KEY", "")
        assert get_finnhub_api_key() is None
