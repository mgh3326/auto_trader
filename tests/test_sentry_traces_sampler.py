"""ROB-1479: the traces_sampler drops monitor-only probe transactions and
otherwise preserves the pre-existing sampling behavior."""

from __future__ import annotations

import pytest

import app.monitoring.sentry as sentry_module


@pytest.fixture(autouse=True)
def _default_rate(monkeypatch):
    monkeypatch.setattr(sentry_module.settings, "SENTRY_TRACES_SAMPLE_RATE", 0.25)


def _sampling_context(
    name: str,
    *,
    op: str = "http.server",
    parent_sampled: bool | None = None,
) -> dict:
    return {
        "transaction_context": {"name": name, "op": op},
        "parent_sampled": parent_sampled,
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    "name",
    [
        "/healthz",
        "GET /healthz",
        "/healthz/",
        "GET /healthz HTTP/1.1",
        "app.routers.health.healthz",
        "/health",
        "GET /health HTTP/1.1",
        "/api/test/health-check",
        "app.routers.test.test_health_check",
    ],
)
def test_sampler_drops_probe_transactions(name):
    assert sentry_module._traces_sampler(_sampling_context(name)) == 0.0


@pytest.mark.unit
def test_sampler_drops_probe_request_from_asgi_scope():
    context = {
        "transaction_context": {"name": "GET http://127.0.0.1:8001/healthz"},
        "parent_sampled": None,
        "asgi_scope": {"type": "http", "path": "/healthz", "raw_path": b"/healthz"},
    }

    assert sentry_module._traces_sampler(context) == 0.0


@pytest.mark.unit
def test_sampler_drops_probe_by_endpoint_name_in_asgi_scope():
    def healthz():
        pass

    healthz.__module__ = "app.routers.health"
    healthz.__qualname__ = "healthz"
    context = {
        "transaction_context": {"name": "/unresolved-at-start"},
        "parent_sampled": None,
        "asgi_scope": {"type": "http", "path": "/", "endpoint": healthz},
    }

    assert sentry_module._traces_sampler(context) == 0.0


@pytest.mark.unit
@pytest.mark.parametrize(
    "name",
    [
        "/mcp",
        "POST /mcp",
        "tools/call get_quote",
        "POST /api/v1/orders",
        "/healthzy",
        "/invest/health",
    ],
)
def test_sampler_uses_default_rate_for_real_transactions(name):
    assert sentry_module._traces_sampler(_sampling_context(name)) == 0.25


@pytest.mark.unit
def test_sampler_uses_default_rate_for_mcp_tool_call():
    context = _sampling_context("tools/call get_quote", op="mcp.server")

    assert sentry_module._traces_sampler(context) == 0.25


@pytest.mark.unit
@pytest.mark.parametrize(
    ("parent_sampled", "expected"),
    [(True, 1.0), (False, 0.0)],
)
def test_sampler_honours_parent_decision(parent_sampled, expected):
    context = _sampling_context(
        "tools/call get_quote",
        parent_sampled=parent_sampled,
    )

    assert sentry_module._traces_sampler(context) == expected


@pytest.mark.unit
def test_sampler_drops_probe_even_when_parent_is_sampled():
    context = _sampling_context(
        "app.routers.health.healthz",
        parent_sampled=True,
    )

    assert sentry_module._traces_sampler(context) == 0.0


@pytest.mark.unit
def test_sampler_with_empty_context_uses_default_rate():
    assert sentry_module._traces_sampler({}) == 0.25
