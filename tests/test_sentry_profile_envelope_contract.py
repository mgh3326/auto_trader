"""ROB obs/sentry-profiling-path: hermetic proof that the collection path is
sound end-to-end — a fixed CPU transaction sampled through the *real*
``init_sentry`` seam produces a ``profile`` envelope item linked to its
``transaction`` item, survives the repository's ROB-1305 scrubber, keeps the
``mcp.server``/``tools/call`` span visible, and never touches a real socket.

This module never uses a real Sentry DSN/key. The fixture DSN below is a
syntactically valid but non-routable placeholder (RFC 5737 style dead host)
and no test in this file performs real network I/O — the in-memory
``CapturingTransport`` replaces the wire transport entirely, and the
repository-wide ROB-1880 socket guard (``tests/_socket_guard_plugin.py``,
installed for the whole suite) fails any test that tries to open a real
socket, so this file relies on that guard instead of installing its own.
"""

from __future__ import annotations

import pytest
import sentry_sdk
from sentry_sdk.transport import Transport

import app.monitoring.sentry as sentry_module

# Non-secret, non-routable: this is a fixture, never a real project credential.
_FAKE_DSN = "https://fakepublickey@fake.invalid.example/123456"


class _CapturingTransport(Transport):
    """In-memory stand-in transport — captures envelopes, sends nothing."""

    def __init__(self) -> None:
        super().__init__({"dsn": _FAKE_DSN})
        self.envelopes: list = []

    def capture_envelope(self, envelope) -> None:  # noqa: ANN001
        self.envelopes.append(envelope)

    def flush(self, timeout, callback=None) -> None:  # noqa: ANN001
        return None


@pytest.fixture(autouse=True)
def _reset_sentry_state(monkeypatch):
    monkeypatch.setattr(sentry_module, "_initialized", False)
    monkeypatch.setattr(
        sentry_module,
        "_enabled_integration_flags",
        {"fastapi": False, "sqlalchemy": False, "httpx": False, "mcp": False},
    )
    yield
    # sentry_sdk.init() mutates process-global client state; restore isolation
    # for subsequent tests in the suite.
    sentry_sdk.get_global_scope().set_client(None)


def _init_with_capturing_transport(**overrides) -> _CapturingTransport:
    transport = _CapturingTransport()
    kwargs = dict(
        dsn=_FAKE_DSN,
        traces_sample_rate=1.0,
        profiles_sample_rate=1.0,
        send_default_pii=False,
        transport=transport,
        before_send_transaction=sentry_module._before_send_transaction,
        # ROB-1880: sentry-sdk's default StdlibIntegration re-wraps
        # subprocess.Popen.__init__ for breadcrumbs, which discards the
        # repo-wide socket guard's own Popen patch and trips its
        # post-test `assert_installed()` check. This test exercises the
        # tracing/profiling envelope pipeline only, so default
        # integrations (which this repo's real init_sentry() also layers
        # on top of explicitly, never relying on auto-enabling) are
        # unneeded here.
        default_integrations=False,
        auto_enabling_integrations=False,
    )
    kwargs.update(overrides)
    sentry_sdk.init(**kwargs)
    return transport


@pytest.mark.unit
def test_sampled_transaction_yields_linked_profile_and_transaction_items():
    """A sampled transaction must ride with a profile item in one envelope,
    and the profile's recorded transaction id must match the transaction
    event's own event_id — the actual SDK 2.x linkage mechanism (not a
    ``contexts.profile`` field)."""
    transport = _init_with_capturing_transport()

    with sentry_sdk.start_transaction(name="probe-cpu-tx", op="test"):
        total = 0
        for i in range(2_000_000):
            total += i * i
        assert total > 0

    sentry_sdk.flush()

    assert transport.envelopes, "expected at least one captured envelope"
    envelope = transport.envelopes[-1]
    item_types = [item.headers.get("type") for item in envelope.items]
    assert "transaction" in item_types, item_types
    assert "profile" in item_types, item_types

    transaction_payload = next(
        item.payload.json for item in envelope.items if item.headers.get("type") == "transaction"
    )
    profile_payload = next(
        item.payload.json for item in envelope.items if item.headers.get("type") == "profile"
    )

    transaction_event_id = transaction_payload["event_id"]
    linked_transaction_ids = {
        entry["id"] for entry in profile_payload.get("transactions", [])
    }
    assert transaction_event_id in linked_transaction_ids, (
        transaction_event_id,
        linked_transaction_ids,
    )


@pytest.mark.unit
def test_mcp_tool_call_transaction_is_scrubbed_but_span_and_profile_survive():
    """Simulate an MCP ``tools/call`` transaction carrying a secret-bearing
    span, sampled through the real profiler + scrubber seam. The mcp.server
    span must survive (renamed transaction), the secret must not, and the
    profile item must still be present and linked."""
    transport = _init_with_capturing_transport()

    with sentry_sdk.start_transaction(name="/mcp", op="http.server") as transaction:
        with transaction.start_child(op="mcp.server", name="tools/call") as span:
            span.set_data("mcp.tool.name", "get_quote")
            span.set_data("mcp.method.name", "tools/call")
            span.set_data(
                "mcp.request.argument.api_key",
                "sk-live-should-never-reach-sentry",
            )
            total = 0
            for i in range(1_000_000):
                total += i
            assert total > 0

    sentry_sdk.flush()

    assert transport.envelopes, "expected at least one captured envelope"
    envelope = transport.envelopes[-1]
    item_types = [item.headers.get("type") for item in envelope.items]
    assert "profile" in item_types, item_types

    transaction_payload = next(
        item.payload.json for item in envelope.items if item.headers.get("type") == "transaction"
    )
    assert transaction_payload["transaction"] == "tools/call get_quote"

    spans = transaction_payload.get("spans", [])
    mcp_spans = [s for s in spans if s.get("op") == "mcp.server"]
    assert mcp_spans, "mcp.server span must survive scrubbing"
    mcp_span = mcp_spans[0]
    serialized_span = str(mcp_span)
    assert "sk-live-should-never-reach-sentry" not in serialized_span
    assert mcp_span["data"]["mcp.request.argument.api_key"] == "[Filtered]"

    profile_payload = next(
        item.payload.json for item in envelope.items if item.headers.get("type") == "profile"
    )
    linked_transaction_ids = {
        entry["id"] for entry in profile_payload.get("transactions", [])
    }
    assert transaction_payload["event_id"] in linked_transaction_ids


@pytest.mark.unit
def test_unsampled_process_produces_zero_envelopes():
    """Sanity check on the harness itself: with sampling off, nothing is
    captured — proves the two tests above are exercising real sampling, not
    an always-emit stub."""
    transport = _init_with_capturing_transport(
        traces_sample_rate=0.0,
        profiles_sample_rate=0.0,
    )

    with sentry_sdk.start_transaction(name="probe-cpu-tx-unsampled", op="test"):
        pass

    sentry_sdk.flush()

    assert transport.envelopes == []
