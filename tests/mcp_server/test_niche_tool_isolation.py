"""Niche-tool isolation (MCP tool usage audit, 2026-09-03).

Class C — "referenced somewhere, never called in 90 days" — was not deleted,
because that evidence cannot distinguish a tool no session can reach from a
genuinely seasonal one. Instead every C tool (plus the five class-D tools kept
for structural reasons) is tagged at call time so the next audit reads
telemetry instead of reference counts.

These tests pin the observable contract: the warning is emitted, the Sentry tag
is set, the tool's own behavior is untouched, and the niche set matches the
audit's own class column rather than drifting into a hand-maintained list.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pytest

from app.mcp_server.profiles import McpProfile
from app.mcp_server.tooling.niche_tools import (
    CLASS_C_TOOL_NAMES,
    NICHE_CALL_EVENT,
    NICHE_SENTRY_TAG,
    NICHE_TOOL_NAMES,
    RETAINED_CLASS_D_TOOL_NAMES,
    NicheTaggingMCP,
    is_niche_tagged,
    wrap_niche_handler,
)
from app.mcp_server.tooling.registry import register_all_tools
from scripts.mcp_tool_usage_audit import collect_registry

AUDIT_PATH = (
    Path(__file__).resolve().parents[2] / "docs" / "mcp-tool-usage-audit-20260903.md"
)


def _audit_classes() -> dict[str, str]:
    """tool -> class, parsed from the audit's Complete classification table."""
    classes: dict[str, str] = {}
    for line in AUDIT_PATH.read_text().splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 10 or cells[0] == "Tool" or set(cells[0]) <= set("-:"):
            continue
        classes[cells[0]] = cells[4]
    return classes


class _Recorder:
    """Minimal registration target; records the function actually registered."""

    def __init__(self) -> None:
        self.registered: dict[str, object] = {}

    def tool(self, *args, **kwargs):
        name = kwargs.get("name")
        if name is None and args and isinstance(args[0], str):
            name = args[0]

        def register(function):
            self.registered[name or function.__name__] = function
            return function

        return register


# ---------------------------------------------------------------------------
# The set is derived from the audit, not hand-maintained
# ---------------------------------------------------------------------------
def test_class_c_set_matches_the_audit_table() -> None:
    from_audit = {
        tool for tool, category in _audit_classes().items() if category == "C"
    }
    assert CLASS_C_TOOL_NAMES == from_audit, (
        "CLASS_C_TOOL_NAMES drifted from the audit's class column; "
        f"extra={sorted(CLASS_C_TOOL_NAMES - from_audit)} "
        f"missing={sorted(from_audit - CLASS_C_TOOL_NAMES)}"
    )


def test_retained_class_d_names_really_are_class_d_in_the_audit() -> None:
    classes = _audit_classes()
    wrong = sorted(
        name for name in RETAINED_CLASS_D_TOOL_NAMES if classes.get(name) != "D"
    )
    assert not wrong, f"retained-D set contains non-D tools: {wrong}"


def test_niche_set_is_the_union_of_its_two_documented_halves() -> None:
    assert NICHE_TOOL_NAMES == CLASS_C_TOOL_NAMES | RETAINED_CLASS_D_TOOL_NAMES


# ---------------------------------------------------------------------------
# The warning and the Sentry tag
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_niche_call_emits_the_warning_and_preserves_the_result(
    caplog: pytest.LogCaptureFixture,
) -> None:
    niche_name = sorted(CLASS_C_TOOL_NAMES)[0]

    async def handler(value: int, *, doubled: bool = False) -> dict[str, int]:
        return {"value": value * 2 if doubled else value}

    wrapped = wrap_niche_handler(niche_name, handler)
    with caplog.at_level(logging.WARNING, logger="app.mcp_server.tooling.niche_tools"):
        result = await wrapped(21, doubled=True)

    assert result == {"value": 42}, "the niche wrapper must not alter the result"
    records = [r for r in caplog.records if NICHE_CALL_EVENT in r.getMessage()]
    assert len(records) == 1, (
        f"expected exactly one {NICHE_CALL_EVENT} warning for {niche_name!r}, "
        f"got {[r.getMessage() for r in caplog.records]}"
    )
    assert records[0].levelno == logging.WARNING
    assert f"tool={niche_name}" in records[0].getMessage()


@pytest.mark.asyncio
async def test_niche_call_sets_the_sentry_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    import sentry_sdk

    tags: dict[str, str] = {}
    monkeypatch.setattr(
        sentry_sdk, "set_tag", lambda key, value: tags.__setitem__(key, value)
    )

    async def handler() -> str:
        return "ok"

    wrapped = wrap_niche_handler(sorted(CLASS_C_TOOL_NAMES)[0], handler)
    assert await wrapped() == "ok"
    assert tags.get(NICHE_SENTRY_TAG) == "true", (
        f"niche call must set Sentry tag {NICHE_SENTRY_TAG}=true; got {tags!r}"
    )


@pytest.mark.asyncio
async def test_niche_wrapper_propagates_exceptions_unchanged() -> None:
    async def handler() -> None:
        raise ValueError("boom")

    wrapped = wrap_niche_handler(sorted(CLASS_C_TOOL_NAMES)[0], handler)
    with pytest.raises(ValueError, match="boom"):
        await wrapped()


# ---------------------------------------------------------------------------
# Non-niche tools are left completely alone
# ---------------------------------------------------------------------------
def test_non_niche_handler_is_returned_unwrapped() -> None:
    async def handler() -> None:
        return None

    assert wrap_niche_handler("analyze_stock_batch", handler) is handler


@pytest.mark.asyncio
async def test_non_niche_call_emits_no_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def handler() -> str:
        return "ok"

    wrapped = wrap_niche_handler("analyze_stock_batch", handler)
    with caplog.at_level(logging.WARNING, logger="app.mcp_server.tooling.niche_tools"):
        assert await wrapped() == "ok"
    assert not [r for r in caplog.records if NICHE_CALL_EVENT in r.getMessage()]


def test_proxy_tags_niche_registrations_and_passes_others_through() -> None:
    recorder = _Recorder()
    proxy = NicheTaggingMCP(recorder)
    niche_name = sorted(CLASS_C_TOOL_NAMES)[0]

    async def niche_handler() -> None:
        return None

    async def live_handler() -> None:
        return None

    proxy.tool(name=niche_name, description="x")(niche_handler)
    proxy.tool(name="analyze_stock_batch", description="x")(live_handler)

    assert is_niche_tagged(recorder.registered[niche_name])
    assert recorder.registered["analyze_stock_batch"] is live_handler


# ---------------------------------------------------------------------------
# Wired into the real registrar, on every profile
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("profile", sorted(p.value for p in McpProfile))
def test_every_registered_niche_tool_is_wrapped_on_every_profile(profile: str) -> None:
    """Including the allowlist profiles that return before the shared block."""
    recorder = _Recorder()
    register_all_tools(recorder, profile=McpProfile(profile))  # type: ignore[arg-type]

    unwrapped = sorted(
        name
        for name, function in recorder.registered.items()
        if name in NICHE_TOOL_NAMES and not is_niche_tagged(function)
    )
    assert not unwrapped, (
        f"profile {profile!r} registers niche tool(s) without the niche tag: "
        f"{unwrapped}"
    )


def test_niche_wrapping_does_not_change_the_registered_surface() -> None:
    """The proxy tags tools; it must not add, drop, or rename any."""
    snapshot = json.loads(
        (
            Path(__file__).resolve().parent / "data" / "mcp_profile_tool_snapshot.json"
        ).read_text()
    )
    registry = collect_registry()
    by_profile: dict[str, set[str]] = {}
    for tool, entry in registry.items():
        for name in entry["profiles"]:
            by_profile.setdefault(name, set()).add(tool)
    assert {p: sorted(v) for p, v in by_profile.items()} == snapshot


def test_no_class_a_or_b_tool_is_tagged_niche() -> None:
    classes = _audit_classes()
    live = sorted(name for name in NICHE_TOOL_NAMES if classes.get(name) in {"A", "B"})
    assert not live, f"live tools must never be tagged niche: {live}"


def test_registry_applies_the_proxy_before_any_profile_branch() -> None:
    """A source-level guard: the tag must not be reachable only on some branch."""
    source = (
        Path(__file__).resolve().parents[2]
        / "app"
        / "mcp_server"
        / "tooling"
        / "registry.py"
    ).read_text()
    body = source[source.index("def register_all_tools(") :]
    wrap_at = body.index("NicheTaggingMCP(mcp)")
    first_branch = body.index("if profile is McpProfile.")
    assert wrap_at < first_branch, (
        "NicheTaggingMCP must be applied before the first profile branch, "
        "otherwise the early-returning allowlist profiles skip the tagging"
    )
    assert re.search(r"\bmcp = NicheTaggingMCP\(mcp\)", body), (
        "the proxy's result must replace `mcp`, not be discarded"
    )
