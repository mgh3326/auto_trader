"""Tests for shared tracing span helpers."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

import app.monitoring.tracing_spans as tracing_spans


def _make_span_cm():
    span = Mock()
    cm = Mock()
    cm.__enter__ = Mock(return_value=span)
    cm.__exit__ = Mock(return_value=False)
    return cm, span


@pytest.mark.unit
@pytest.mark.asyncio
async def test_traced_await_returns_result(monkeypatch):
    cm, span = _make_span_cm()
    monkeypatch.setattr(tracing_spans.sentry_sdk, "start_span", Mock(return_value=cm))

    async def _sample() -> str:
        return "ok"

    result = await tracing_spans.traced_await(
        _sample(),
        op="db.service",
        name="sample.await",
        data={"k1": "v1", "k2": None},
    )

    assert result == "ok"
    span.set_data.assert_any_call("k1", "v1")
    assert ("k2", None) not in [call.args for call in span.set_data.call_args_list]
    span.set_status.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_traced_to_thread_marks_error(monkeypatch):
    cm, span = _make_span_cm()
    monkeypatch.setattr(tracing_spans.sentry_sdk, "start_span", Mock(return_value=cm))

    def _boom() -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await tracing_spans.traced_to_thread(
            _boom,
            op="http.client.test",
            name="test.boom",
            data={"api": "x"},
        )

    span.set_status.assert_called_once_with("internal_error")
    span.set_data.assert_any_call("error_type", "ValueError")


@pytest.mark.unit
def test_sentry_span_sets_data(monkeypatch):
    cm, span = _make_span_cm()
    monkeypatch.setattr(tracing_spans.sentry_sdk, "start_span", Mock(return_value=cm))

    with tracing_spans.sentry_span(op="custom", name="sample", data={"a": 1, "b": None}):
        pass

    span.set_data.assert_any_call("a", 1)
