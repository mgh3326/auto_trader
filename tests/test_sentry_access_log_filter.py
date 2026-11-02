"""ROB-1479: uvicorn access logs on 2xx/3xx are dropped from Sentry logs,
while 4xx/5xx and every non-access logger are kept."""

from __future__ import annotations

import pytest
from sentry_sdk.types import Log

import app.monitoring.sentry as sentry_module


def _access_log(body: str) -> Log:
    return {
        "severity_text": "error",
        "severity_number": 17,
        "body": body,
        "attributes": {"logger.name": "uvicorn.access"},
        "time_unix_nano": 1,
        "trace_id": None,
        "span_id": None,
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    "body",
    [
        '127.0.0.1:52778 - "GET /api/v1/orders HTTP/1.1" 200',
        '127.0.0.1:52778 - "POST /api/v1/orders HTTP/1.1" 201',
        '127.0.0.1:52778 - "GET /assets/app.js HTTP/1.1" 304',
        '127.0.0.1:52778 - "GET /healthz HTTP/1.1" 200',
    ],
)
def test_before_send_log_drops_successful_access_logs(body):
    assert sentry_module._before_send_log(_access_log(body), {}) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "body",
    [
        '127.0.0.1:52778 - "GET /api/v1/orders HTTP/1.1" 400',
        '127.0.0.1:52778 - "GET /api/v1/orders HTTP/1.1" 404',
        '127.0.0.1:52778 - "POST /api/v1/orders HTTP/1.1" 422',
        '127.0.0.1:52778 - "GET /api/v1/orders HTTP/1.1" 500',
    ],
)
def test_before_send_log_keeps_failed_access_logs(body):
    kept = sentry_module._before_send_log(_access_log(body), {})

    assert kept is not None
    assert kept["body"] == body


@pytest.mark.unit
def test_before_send_log_keeps_non_access_logger_warning():
    log: Log = {
        "severity_text": "warning",
        "severity_number": 13,
        "body": "quote provider slow: 200 requests queued",
        "attributes": {"logger.name": "app.services.market_data"},
        "time_unix_nano": 1,
        "trace_id": None,
        "span_id": None,
    }

    assert sentry_module._before_send_log(log, {}) is not None


@pytest.mark.unit
def test_before_send_log_does_not_mistake_a_quoted_path_for_the_status():
    """A path containing a quote closes the request-line quote early and adds
    a second quoted field; the leading 200 is path text, the status is 500."""
    log = _access_log('127.0.0.1:52778 - "GET /reports/" 200 HTTP/1.1 HTTP/1.1" 500')

    kept = sentry_module._before_send_log(log, {})

    assert kept is not None
    assert kept["body"] == log["body"]


@pytest.mark.unit
def test_before_send_log_keeps_access_log_without_parseable_status():
    log = _access_log('127.0.0.1:52778 - "GET /api/v1/orders HTTP/1.1"')

    assert sentry_module._before_send_log(log, {}) is not None
