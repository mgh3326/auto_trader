# tests/core/test_cli.py
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.unit
class TestSetupLoggingAndSentry:
    def test_calls_basicConfig_with_service_name_format(self, monkeypatch):
        """setup_logging_and_sentry가 service_name을 포함한 format으로 basicConfig를 호출하는지 확인."""
        from app.core import cli

        mock_basic = MagicMock()
        monkeypatch.setattr(logging, "basicConfig", mock_basic)
        monkeypatch.setattr(cli, "init_sentry", MagicMock())
        monkeypatch.setattr(cli.settings, "LOG_LEVEL", "DEBUG")

        cli.setup_logging_and_sentry(service_name="test-svc")

        mock_basic.assert_called_once()
        _, kwargs = mock_basic.call_args
        assert kwargs["level"] == logging.DEBUG
        assert "%(asctime)s" in kwargs["format"]
        assert "%(name)s" in kwargs["format"]
        assert "%(levelname)s" in kwargs["format"]
        assert "%(message)s" in kwargs["format"]

    def test_calls_init_sentry_with_service_name(self, monkeypatch):
        """setup_logging_and_sentry가 init_sentry를 service_name=<name>으로 호출하는지 확인."""
        from app.core import cli

        mock_sentry = MagicMock()
        monkeypatch.setattr(logging, "basicConfig", MagicMock())
        monkeypatch.setattr(cli, "init_sentry", mock_sentry)
        monkeypatch.setattr(cli.settings, "LOG_LEVEL", "INFO")

        cli.setup_logging_and_sentry(service_name="my-service")

        mock_sentry.assert_called_once_with(service_name="my-service")

    def test_log_level_uppercased(self, monkeypatch):
        """settings.LOG_LEVEL 소문자도 올바른 레벨로 변환되는지 확인."""
        from app.core import cli

        mock_basic = MagicMock()
        monkeypatch.setattr(logging, "basicConfig", mock_basic)
        monkeypatch.setattr(cli, "init_sentry", MagicMock())
        monkeypatch.setattr(cli.settings, "LOG_LEVEL", "warning")

        cli.setup_logging_and_sentry(service_name="svc")

        _, kwargs = mock_basic.call_args
        assert kwargs["level"] == logging.WARNING

    def test_unknown_log_level_falls_back_to_info(self, monkeypatch):
        """알 수 없는 LOG_LEVEL은 logging.INFO로 폴백하는지 확인."""
        from app.core import cli

        mock_basic = MagicMock()
        monkeypatch.setattr(logging, "basicConfig", mock_basic)
        monkeypatch.setattr(cli, "init_sentry", MagicMock())
        monkeypatch.setattr(cli.settings, "LOG_LEVEL", "INVALID_LEVEL")

        cli.setup_logging_and_sentry(service_name="svc")

        _, kwargs = mock_basic.call_args
        assert kwargs["level"] == logging.INFO


@pytest.mark.unit
class TestRunAsyncJob:
    @pytest.mark.asyncio
    async def test_returns_zero_on_success(self):
        """coro_factory가 0 반환 시 run_async_job도 0 반환."""
        from app.core.cli import run_async_job

        result = await run_async_job(AsyncMock(return_value=0), process="test_proc")

        assert result == 0

    @pytest.mark.asyncio
    async def test_returns_nonzero_on_job_failure(self):
        """coro_factory가 1 반환 시 run_async_job도 1 반환."""
        from app.core.cli import run_async_job

        result = await run_async_job(AsyncMock(return_value=1), process="test_proc")

        assert result == 1

    @pytest.mark.asyncio
    async def test_captures_exception_and_returns_one(self, monkeypatch):
        """coro_factory 예외 발생 시 capture_exception 호출 + exit 1 반환."""
        from app.core import cli

        mock_capture = MagicMock()
        monkeypatch.setattr(cli, "capture_exception", mock_capture)

        exc = RuntimeError("boom")

        async def _fail() -> int:
            raise exc

        result = await cli.run_async_job(_fail, process="sync_test")

        assert result == 1
        mock_capture.assert_called_once_with(exc, process="sync_test")

    @pytest.mark.asyncio
    async def test_does_not_swallow_system_exit(self):
        """SystemExit은 capture하지 않고 전파되는지 확인."""
        from app.core.cli import run_async_job

        async def _sys_exit() -> int:
            raise SystemExit(2)

        with pytest.raises(SystemExit):
            await run_async_job(_sys_exit, process="test_proc")
