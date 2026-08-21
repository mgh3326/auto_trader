"""ROB obs/sentry-profiling-path: hermetic proof that the collection path is
sound end-to-end — a fixed CPU transaction sampled through the *real*
``app.monitoring.sentry.init_sentry()`` seam (not a hand-rolled duplicate)
produces a ``profile`` envelope item linked to its ``transaction`` item, and
the *transaction* event survives the repository's ROB-1305 scrubber while
the ``mcp.server``/``tools/call`` span stays visible.

Scope note (verified against installed sentry-sdk 2.57.0, not assumed): this
file does NOT claim the ``profile`` item's own payload is scrubbed. Reading
``sentry_sdk/client.py::Client.capture_event`` shows ``event.pop("profile")``
happens *before* ``_prepare_event`` (which is what invokes
``before_send_transaction``) — so ``before_send_transaction`` never sees the
profile payload, and sentry-sdk 2.57.0's public ``init()`` options
(``sentry_sdk/consts.py``) expose no ``before_send_profile`` or equivalent
hook. Profile frames are stack metadata (function/file/line via
``abs_path``), not captured local-variable values, so this is a filesystem/
code-structure disclosure, not a secret-value leak — but it is real and
unscrubbable through any public SDK seam, so it must not be misrepresented
as covered by the ROB-1305 scrubber. ``test_profile_frames_carry_unscrubbed_filesystem_paths``
below pins this as an explicit, reported characteristic (see the runbook's
"Known limitation" section) rather than leaving it as a silent assumption.

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
    monkeypatch.setattr(sentry_module.settings, "SENTRY_DSN", _FAKE_DSN)
    monkeypatch.setattr(sentry_module.settings, "SENTRY_ENVIRONMENT", "test")
    monkeypatch.setattr(sentry_module.settings, "SENTRY_SEND_DEFAULT_PII", False)
    monkeypatch.setattr(sentry_module.settings, "SENTRY_ENABLE_LOG_EVENTS", True)
    monkeypatch.setattr(sentry_module.settings, "SENTRY_MCP_INCLUDE_PROMPTS", False)
    yield
    # sentry_sdk.init() mutates process-global client state; restore isolation
    # for subsequent tests in the suite.
    sentry_sdk.get_global_scope().set_client(None)


def _init_via_real_seam(
    monkeypatch, *, traces_sample_rate: float, profiles_sample_rate: float
) -> _CapturingTransport:
    """Call the real, production ``init_sentry()`` — not a hand-rolled
    duplicate of its kwargs — and intercept only the underlying
    ``sentry_sdk.init`` call to inject an in-memory transport and disable
    the SDK's default integrations.

    Why intercept at all rather than pass ``transport=`` straight through:
    ``init_sentry()`` has no ``transport`` parameter (by design — production
    call sites never need one). This wrapper preserves every other kwarg
    ``init_sentry()`` computes from settings (dsn, environment, release,
    sample rates, integrations list, all four before_send*/before_breadcrumb
    hooks) exactly as production code builds them, so a bug in that
    construction (wrong settings field, a dropped hook, ...) still fails
    this test — only the transport and default-integration set are
    substituted, and default_integrations=False is required so sentry-sdk's
    StdlibIntegration does not re-wrap ``subprocess.Popen.__init__`` and trip
    the repo-wide ROB-1880 socket guard's post-test installation check.
    """
    transport = _CapturingTransport()
    real_sentry_sdk_init = sentry_sdk.init

    def _intercepting_init(**kwargs):
        kwargs["transport"] = transport
        kwargs["default_integrations"] = False
        kwargs["auto_enabling_integrations"] = False
        return real_sentry_sdk_init(**kwargs)

    monkeypatch.setattr(sentry_module.sentry_sdk, "init", _intercepting_init)
    monkeypatch.setattr(
        sentry_module.settings, "SENTRY_TRACES_SAMPLE_RATE", traces_sample_rate
    )
    monkeypatch.setattr(
        sentry_module.settings, "SENTRY_PROFILES_SAMPLE_RATE", profiles_sample_rate
    )

    initialized = sentry_module.init_sentry(service_name="test-envelope-contract")
    assert initialized is True, (
        "init_sentry() must report success for a fake-but-set DSN"
    )
    return transport


@pytest.mark.unit
def test_sampled_transaction_yields_linked_profile_and_transaction_items(monkeypatch):
    """A sampled transaction, started through the real init_sentry() seam,
    must ride with a profile item in one envelope, and the profile's
    recorded transaction id must match the transaction event's own
    event_id — the actual SDK 2.x linkage mechanism (not a
    ``contexts.profile`` field)."""
    transport = _init_via_real_seam(
        monkeypatch, traces_sample_rate=1.0, profiles_sample_rate=1.0
    )

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
        item.payload.json
        for item in envelope.items
        if item.headers.get("type") == "transaction"
    )
    profile_payload = next(
        item.payload.json
        for item in envelope.items
        if item.headers.get("type") == "profile"
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
def test_mcp_tool_call_transaction_is_scrubbed_but_span_and_profile_survive(
    monkeypatch,
):
    """Simulate an MCP ``tools/call`` transaction carrying a secret-shaped
    span field, sampled through the real init_sentry() + profiler + scrubber
    seam. The mcp.server span must survive (renamed transaction), the
    secret-named field must not, and the profile item must still be present
    and linked.

    This test asserts that a secret-KEY-named field is still scrubbed by the
    existing ROB-1305 key-name/value-shape sanitizer, while the R6 default-
    deny transform removes MCP payload fields without deleting the span or
    its protocol/performance metadata.
    """
    transport = _init_via_real_seam(
        monkeypatch, traces_sample_rate=1.0, profiles_sample_rate=1.0
    )

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
        item.payload.json
        for item in envelope.items
        if item.headers.get("type") == "transaction"
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
        item.payload.json
        for item in envelope.items
        if item.headers.get("type") == "profile"
    )
    linked_transaction_ids = {
        entry["id"] for entry in profile_payload.get("transactions", [])
    }
    assert transaction_payload["event_id"] in linked_transaction_ids
    profile_transaction = next(
        entry
        for entry in profile_payload["transactions"]
        if entry["id"] == transaction_payload["event_id"]
    )
    assert (
        profile_transaction["trace_id"]
        == transaction_payload["contexts"]["trace"]["trace_id"]
    )
    assert profile_transaction["name"] == transaction_payload["transaction"]


@pytest.mark.unit
def test_unsampled_process_produces_zero_envelopes(monkeypatch):
    """Sanity check on the harness itself: with sampling off, nothing is
    captured — proves the tests above exercise real sampling, not an
    always-emit stub."""
    transport = _init_via_real_seam(
        monkeypatch, traces_sample_rate=0.0, profiles_sample_rate=0.0
    )

    with sentry_sdk.start_transaction(name="probe-cpu-tx-unsampled", op="test"):
        pass

    sentry_sdk.flush()

    assert transport.envelopes == []


@pytest.mark.unit
def test_zero_traces_sample_rate_suppresses_profile_even_with_profiles_enabled(
    monkeypatch,
):
    """Profiling piggybacks on trace sampling in sentry-sdk 2.57.0's
    transaction profiler: profiles_sample_rate alone is not sufficient. This
    is what app.monitoring.sentry_diagnostics.get_sentry_diagnostics's
    profiler_ready field must account for (see test_sentry_diagnostics.py)."""
    transport = _init_via_real_seam(
        monkeypatch, traces_sample_rate=0.0, profiles_sample_rate=1.0
    )

    with sentry_sdk.start_transaction(name="probe-cpu-tx-no-trace", op="test"):
        total = 0
        for i in range(2_000_000):
            total += i * i
        assert total > 0

    sentry_sdk.flush()

    for envelope in transport.envelopes:
        item_types = [item.headers.get("type") for item in envelope.items]
        assert "profile" not in item_types, item_types


@pytest.mark.unit
def test_profile_frames_carry_unscrubbed_filesystem_paths(monkeypatch):
    """Documents (does not "fix" — no public SDK hook exists, see module
    docstring) that profile item frames include real ``abs_path`` filesystem
    paths, verified against the installed sentry-sdk 2.57.0. Frames are
    stack metadata only — this loop's local variable value never appears —
    so this is a code-structure/path disclosure, not a secret-value leak.
    Pinned here so this stays a known, reported characteristic rather than a
    silent assumption if the SDK's profiler internals change."""
    transport = _init_via_real_seam(
        monkeypatch, traces_sample_rate=1.0, profiles_sample_rate=1.0
    )

    with sentry_sdk.start_transaction(name="probe-cpu-tx-frames", op="test"):
        fake_secret_local_value = "sk-live-should-never-appear-in-profile-frames"
        total = 0
        for i in range(3_000_000):
            total += i * i
        assert total > 0
        assert fake_secret_local_value

    sentry_sdk.flush()

    profile_payloads = [
        item.payload.json
        for envelope in transport.envelopes
        for item in envelope.items
        if item.headers.get("type") == "profile"
    ]
    assert profile_payloads, "expected at least one profile item"
    frames = profile_payloads[0].get("profile", {}).get("frames", [])
    assert frames, "expected sampled stack frames"
    assert any("abs_path" in frame and frame["abs_path"] for frame in frames)
    serialized_frames = str(frames)
    assert "sk-live-should-never-appear-in-profile-frames" not in serialized_frames
