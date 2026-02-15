"""Tests for shared Sentry initialization helpers."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

import app.monitoring.sentry as sentry_module


@pytest.fixture(autouse=True)
def reset_sentry_state(monkeypatch):
    monkeypatch.setattr(sentry_module, "_initialized", False)
    monkeypatch.setattr(sentry_module.settings, "SENTRY_DSN", "")
    monkeypatch.setattr(sentry_module.settings, "SENTRY_ENVIRONMENT", None)
    monkeypatch.setattr(sentry_module.settings, "SENTRY_RELEASE", None)
    monkeypatch.setattr(sentry_module.settings, "SENTRY_TRACES_SAMPLE_RATE", 1.0)
    monkeypatch.setattr(sentry_module.settings, "SENTRY_PROFILES_SAMPLE_RATE", 1.0)
    monkeypatch.setattr(sentry_module.settings, "SENTRY_SEND_DEFAULT_PII", True)
    monkeypatch.setattr(sentry_module.settings, "SENTRY_ENABLE_LOG_EVENTS", True)
    monkeypatch.setattr(sentry_module.settings, "ENVIRONMENT", "development")


@pytest.mark.unit
def test_init_sentry_no_dsn(monkeypatch):
    mock_init = Mock()
    monkeypatch.setattr(sentry_module.sentry_sdk, "init", mock_init)

    result = sentry_module.init_sentry("auto-trader-api")

    assert result is False
    mock_init.assert_not_called()


@pytest.mark.unit
def test_init_sentry_with_fastapi_and_celery(monkeypatch):
    monkeypatch.setattr(
        sentry_module.settings,
        "SENTRY_DSN",
        "https://public@example.ingest.sentry.io/1",
    )
    monkeypatch.setenv("GITHUB_SHA", "abc123")

    mock_init = Mock()
    mock_set_tag = Mock()
    monkeypatch.setattr(sentry_module.sentry_sdk, "init", mock_init)
    monkeypatch.setattr(sentry_module.sentry_sdk, "set_tag", mock_set_tag)

    result = sentry_module.init_sentry(
        "auto-trader-api",
        enable_fastapi=True,
        enable_celery=True,
    )

    assert result is True
    mock_init.assert_called_once()
    kwargs = mock_init.call_args.kwargs
    assert kwargs["dsn"] == "https://public@example.ingest.sentry.io/1"
    assert kwargs["environment"] == "development"
    assert kwargs["release"] == "abc123"
    assert kwargs["traces_sample_rate"] == 1.0
    assert kwargs["profiles_sample_rate"] == 1.0
    assert kwargs["send_default_pii"] is True

    integration_names = {
        type(integration).__name__ for integration in kwargs["integrations"]
    }
    assert "LoggingIntegration" in integration_names
    assert "FastApiIntegration" in integration_names
    assert "CeleryIntegration" in integration_names

    mock_set_tag.assert_any_call("service", "auto-trader-api")
    mock_set_tag.assert_any_call("runtime", "python")
    mock_set_tag.assert_any_call("app", "auto-trader")


@pytest.mark.unit
def test_init_sentry_with_sqlalchemy_and_httpx(monkeypatch):
    monkeypatch.setattr(
        sentry_module.settings,
        "SENTRY_DSN",
        "https://public@example.ingest.sentry.io/1",
    )

    mock_init = Mock()
    mock_set_tag = Mock()
    monkeypatch.setattr(sentry_module.sentry_sdk, "init", mock_init)
    monkeypatch.setattr(sentry_module.sentry_sdk, "set_tag", mock_set_tag)

    result = sentry_module.init_sentry(
        "auto-trader-api",
        enable_sqlalchemy=True,
        enable_httpx=True,
    )

    assert result is True
    mock_init.assert_called_once()
    kwargs = mock_init.call_args.kwargs

    integration_names = {
        type(integration).__name__ for integration in kwargs["integrations"]
    }
    assert "LoggingIntegration" in integration_names
    assert "SqlalchemyIntegration" in integration_names
    assert "HttpxIntegration" in integration_names
    assert "FastApiIntegration" not in integration_names
    assert "CeleryIntegration" not in integration_names


@pytest.mark.unit
def test_init_sentry_all_integrations(monkeypatch):
    monkeypatch.setattr(
        sentry_module.settings,
        "SENTRY_DSN",
        "https://public@example.ingest.sentry.io/1",
    )

    mock_init = Mock()
    mock_set_tag = Mock()
    monkeypatch.setattr(sentry_module.sentry_sdk, "init", mock_init)
    monkeypatch.setattr(sentry_module.sentry_sdk, "set_tag", mock_set_tag)

    result = sentry_module.init_sentry(
        "auto-trader-api",
        enable_fastapi=True,
        enable_celery=True,
        enable_sqlalchemy=True,
        enable_httpx=True,
    )

    assert result is True
    kwargs = mock_init.call_args.kwargs

    integration_names = {
        type(integration).__name__ for integration in kwargs["integrations"]
    }
    assert "LoggingIntegration" in integration_names
    assert "FastApiIntegration" in integration_names
    assert "CeleryIntegration" in integration_names
    assert "SqlalchemyIntegration" in integration_names
    assert "HttpxIntegration" in integration_names


@pytest.mark.unit
def test_init_sentry_disables_error_log_events(monkeypatch):
    monkeypatch.setattr(
        sentry_module.settings,
        "SENTRY_DSN",
        "https://public@example.ingest.sentry.io/1",
    )
    monkeypatch.setattr(sentry_module.settings, "SENTRY_ENABLE_LOG_EVENTS", False)

    mock_init = Mock()
    monkeypatch.setattr(sentry_module.sentry_sdk, "init", mock_init)

    result = sentry_module.init_sentry("auto-trader-api")

    assert result is True
    kwargs = mock_init.call_args.kwargs
    logging_integration = next(
        integration
        for integration in kwargs["integrations"]
        if type(integration).__name__ == "LoggingIntegration"
    )
    assert logging_integration._handler is None


@pytest.mark.unit
def test_before_send_masks_sensitive_fields():
    event = {
        "request": {"headers": {"authorization": "Bearer token", "x-api-key": "abc"}},
        "extra": {"token": "my-token", "nested": {"password": "secret"}},
    }

    sanitized = sentry_module._before_send(event, {})

    assert sanitized["request"]["headers"]["authorization"] == "[Filtered]"
    assert sanitized["request"]["headers"]["x-api-key"] == "[Filtered]"
    assert sanitized["extra"]["token"] == "[Filtered]"
    assert sanitized["extra"]["nested"]["password"] == "[Filtered]"


@pytest.mark.unit
def test_before_send_drops_healthz_uvicorn_access_log():
    event = {
        "logger": "uvicorn.access",
        "logentry": {
            "formatted": '127.0.0.1:52778 - "GET /healthz HTTP/1.1" 200',
        },
    }

    dropped = sentry_module._before_send(event, {})

    assert dropped is None


@pytest.mark.unit
def test_before_send_log_drops_healthz_uvicorn_access_log():
    sentry_log = {
        "body": '127.0.0.1:52778 - "GET /healthz HTTP/1.1" 200',
        "attributes": {"logger.name": "uvicorn.access"},
    }

    dropped = sentry_module._before_send_log(sentry_log, {})

    assert dropped is None


@pytest.mark.unit
def test_before_send_log_keeps_non_healthz_uvicorn_access_log():
    sentry_log = {
        "body": '127.0.0.1:52778 - "GET /api/v1/orders HTTP/1.1" 200',
        "attributes": {"logger.name": "uvicorn.access"},
    }

    kept = sentry_module._before_send_log(sentry_log, {})

    assert kept is not None
    assert kept["body"] == '127.0.0.1:52778 - "GET /api/v1/orders HTTP/1.1" 200'


@pytest.mark.unit
def test_capture_exception_adds_masked_context(monkeypatch):
    scope_mock = Mock()
    context_manager = Mock()
    context_manager.__enter__ = Mock(return_value=scope_mock)
    context_manager.__exit__ = Mock(return_value=False)

    monkeypatch.setattr(sentry_module, "_initialized", True)
    monkeypatch.setattr(
        sentry_module.sentry_sdk, "push_scope", Mock(return_value=context_manager)
    )
    mock_capture = Mock()
    monkeypatch.setattr(sentry_module.sentry_sdk, "capture_exception", mock_capture)

    exc = RuntimeError("boom")
    sentry_module.capture_exception(exc, token="abc", normal_key="value")

    scope_mock.set_extra.assert_any_call("token", "[Filtered]")
    scope_mock.set_extra.assert_any_call("normal_key", "value")
    mock_capture.assert_called_once_with(exc)
