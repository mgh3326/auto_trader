"""Task #891 / operator decision Q-52 (2026-09-28): live-session MCP profiles.

``live-kr`` / ``live-us`` / ``live-crypto`` are closed-world profiles whose
tool lists live in ONE operator-readable file — ``config/mcp_profiles/
live.yaml`` — split into ``core`` and ``extended`` tiers. Each profile's
``tiers:`` field is the single switch selecting which tiers load (default:
both, until the operator decides the final cut).

Safety shape (fail-closed, checked at startup/registration time):

* the manifest is schema-validated; a name that is not a classified
  registered tool (typo, unclassified newcomer) is rejected at load;
* names in the route taxonomy's mutation classes (direct broker
  order/cancel/modify, reconcile writers, proposal lifecycle, persistence
  coordinators, harness-denied tools) are rejected at load — a live profile
  can never carry them;
* at registration, the manifest allowlist physically drops every tool a
  shared registrar emits that is not selected for the profile, and the
  registered set must equal the manifest selection exactly (a declared tool
  that no registrar produced fails the completeness check);
* each profile is therefore a strict subset of the already-registered
  DEFAULT surface by construction.

Adding a tool later = an operator-approved PR editing the manifest (see
docs/runbooks/live-mcp-profiles.md).
"""

from __future__ import annotations

import functools
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar, cast

import yaml

from app.core.config import settings
from app.mcp_server.profiles import McpProfile
from app.mcp_server.tooling.route_request_lanes import (
    ALL_KNOWN_TOOLS,
    MUTATION_TOOLS,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

_F = TypeVar("_F", bound=Callable[..., Any])

LIVE_MANIFEST_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "mcp_profiles" / "live.yaml"
)

LIVE_PROFILES: frozenset[McpProfile] = frozenset(
    {McpProfile.LIVE_KR, McpProfile.LIVE_US, McpProfile.LIVE_CRYPTO}
)

_TIER_NAMES: frozenset[str] = frozenset({"core", "extended"})
_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_MANIFEST_KEYS = {"version", "evidence", "profiles"}
_PROFILE_KEYS = {"description", "tiers", "tools"}
_TOOL_KEYS = {"name", "tier", "calls_30d", "purpose", "gate"}

# Names inside the route taxonomy's mutation bucket that a live profile is
# still allowed to carry: the operator draft's proposal-led write plus the
# read-only order-history/status helpers. Everything else in MUTATION_TOOLS
# (direct broker order/cancel/modify, reconcile writers, proposal lifecycle,
# reserve-net consumer, persistence coordinators, harness-denied tools, watch
# expire/sweep) is forbidden for live profiles.
_LIVE_ALLOWED_MUTATIONS: frozenset[str] = frozenset(
    {
        "order_proposal_create",
        "investment_watch_void",
        "get_order_history",
        "toss_get_order_history",
    }
)

# Write/persistence tools the operator draft (Q-52) explicitly authorizes on
# live profiles. Any other write tool may only appear under ``extended`` with
# documented usage evidence — never under ``core``.
OPERATOR_DRAFT_WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "order_proposal_create",
        "order_proposal_get",
        "order_proposal_list",
        "investment_watch_create",
        "investment_watch_void",
        "session_context_append",
        "forecast_save",
        "forecast_resolve",
        "save_trade_retrospective",
    }
)

LIVE_FORBIDDEN_TOOL_NAMES: frozenset[str] = frozenset(
    MUTATION_TOOLS - _LIVE_ALLOWED_MUTATIONS
)

# Backstop for mutation-shaped names not yet classified in the route
# taxonomy: a future tool alias that looks like an order mutation is rejected
# here even before the ALL_KNOWN_TOOLS check runs.
_LIVE_FORBIDDEN_NAME_RE = re.compile(
    r"(place|modify|cancel|submit)_order|cancel_pending_order|execute_report|reconcile"
)


@dataclass(frozen=True)
class LiveToolEntry:
    """One manifest line: name + tier + evidence + optional settings gate."""

    name: str
    tier: str
    calls_30d: int
    purpose: str
    gate: str | None


@dataclass(frozen=True)
class LiveProfileSpec:
    """Validated manifest section for one live profile."""

    profile: McpProfile
    tiers: frozenset[str]
    tools: tuple[LiveToolEntry, ...]

    def selected_entries(self) -> tuple[LiveToolEntry, ...]:
        return tuple(entry for entry in self.tools if entry.tier in self.tiers)

    def selected_tool_names(self) -> frozenset[str]:
        return frozenset(entry.name for entry in self.selected_entries())


@dataclass(frozen=True)
class LiveProfileManifest:
    """Parsed+validated config/mcp_profiles/live.yaml."""

    specs: dict[McpProfile, LiveProfileSpec]

    def spec_for(self, profile: McpProfile) -> LiveProfileSpec:
        return self.specs[profile]


def _fail(message: str) -> ValueError:
    return ValueError(f"live MCP profile manifest: {message}")


def _validate_tool_entry(raw: Any, *, profile_name: str, index: int) -> LiveToolEntry:
    where = f"profiles.{profile_name}.tools[{index}]"
    if not isinstance(raw, dict):
        raise _fail(f"{where} must be a mapping")
    unknown = set(raw) - _TOOL_KEYS
    if unknown:
        raise _fail(f"{where} has unknown keys: {sorted(unknown)}")
    for required in ("name", "tier", "calls_30d", "purpose"):
        if required not in raw:
            raise _fail(f"{where} is missing required key '{required}'")

    name = raw["name"]
    if not isinstance(name, str) or not _TOOL_NAME_RE.fullmatch(name):
        raise _fail(f"{where}.name must be a snake_case tool name, got {name!r}")
    if name in LIVE_FORBIDDEN_TOOL_NAMES or _LIVE_FORBIDDEN_NAME_RE.search(name):
        raise _fail(
            f"{where}.name '{name}' is forbidden on live profiles "
            "(broker order/cancel/modify, reconcile, proposal-lifecycle, "
            "persistence, or harness-denied tool class)"
        )
    if name not in ALL_KNOWN_TOOLS:
        raise _fail(
            f"{where}.name '{name}' is not a classified registered tool; "
            "add it through the route_request_lanes taxonomy first"
        )

    tier = raw["tier"]
    if tier not in _TIER_NAMES:
        raise _fail(f"{where}.tier must be one of {sorted(_TIER_NAMES)}, got {tier!r}")

    calls = raw["calls_30d"]
    if isinstance(calls, bool) or not isinstance(calls, int) or calls < 0:
        raise _fail(f"{where}.calls_30d must be a non-negative int, got {calls!r}")

    purpose = raw["purpose"]
    if not isinstance(purpose, str) or not purpose.strip():
        raise _fail(f"{where}.purpose must be a non-empty string")

    gate = raw.get("gate")
    if gate is not None:
        if not isinstance(gate, str) or not gate.strip():
            raise _fail(f"{where}.gate must be a settings field name, got {gate!r}")
        if gate not in type(settings).model_fields:
            raise _fail(f"{where}.gate '{gate}' is not a settings field")

    return LiveToolEntry(
        name=name,
        tier=cast(str, tier),
        calls_30d=cast(int, calls),
        purpose=purpose.strip(),
        gate=gate,
    )


def _validate_profile(raw: Any, *, profile: McpProfile) -> LiveProfileSpec:
    where = f"profiles.{profile.value}"
    if not isinstance(raw, dict):
        raise _fail(f"{where} must be a mapping")
    unknown = set(raw) - _PROFILE_KEYS
    if unknown:
        raise _fail(f"{where} has unknown keys: {sorted(unknown)}")

    tiers = raw.get("tiers")
    if (
        not isinstance(tiers, list)
        or not tiers
        or len(set(tiers)) != len(tiers)
        or any(tier not in _TIER_NAMES for tier in tiers)
    ):
        raise _fail(
            f"{where}.tiers must be a non-empty unique subset of "
            f"{sorted(_TIER_NAMES)}, got {tiers!r}"
        )

    tools = raw.get("tools")
    if not isinstance(tools, list) or not tools:
        raise _fail(f"{where}.tools must be a non-empty list")

    entries: list[LiveToolEntry] = []
    seen: set[str] = set()
    for index, raw_tool in enumerate(tools):
        entry = _validate_tool_entry(raw_tool, profile_name=profile.value, index=index)
        if entry.name in seen:
            raise _fail(f"{where}.tools[{index}] duplicates '{entry.name}'")
        seen.add(entry.name)
        entries.append(entry)

    return LiveProfileSpec(
        profile=profile,
        tiers=frozenset(tiers),
        tools=tuple(entries),
    )


def _parse_manifest(path: Path) -> LiveProfileManifest:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise _fail(f"cannot read {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise _fail("manifest root must be a mapping")
    unknown = set(raw) - _MANIFEST_KEYS
    if unknown:
        raise _fail(f"manifest has unknown top-level keys: {sorted(unknown)}")
    if raw.get("version") != 1:
        raise _fail("manifest version must be 1")

    profiles = raw.get("profiles")
    if not isinstance(profiles, dict):
        raise _fail("manifest 'profiles' must be a mapping")
    expected = {profile.value for profile in LIVE_PROFILES}
    if set(profiles) != expected:
        raise _fail(
            f"manifest profiles must be exactly {sorted(expected)}, "
            f"got {sorted(profiles)}"
        )

    specs = {
        profile: _validate_profile(profiles[profile.value], profile=profile)
        for profile in LIVE_PROFILES
    }
    return LiveProfileManifest(specs=specs)


@functools.cache
def _load_cached(path_str: str) -> LiveProfileManifest:
    return _parse_manifest(Path(path_str))


def load_live_manifest(path: Path | None = None) -> LiveProfileManifest:
    """Load and validate the live-profile manifest (cached per path)."""
    return _load_cached(str(path or LIVE_MANIFEST_PATH))


def live_profile_tool_names(
    profile: McpProfile, *, path: Path | None = None
) -> frozenset[str]:
    """Manifest-selected tool names for a live profile (tiers applied)."""
    return load_live_manifest(path).spec_for(profile).selected_tool_names()


class LiveProfileMCP:
    """Exact-set recording proxy: only manifest-selected names can register.

    Same fail-closed registration shape as the kiwoom_kr/analysis_readonly
    allowlist proxies, plus a completeness check: after all registrars have
    run, ``assert_complete`` fails the boot unless every selected tool whose
    optional gate is enabled actually landed (catches manifest names that no
    registrar produces).
    """

    def __init__(self, inner: Any, spec: LiveProfileSpec) -> None:
        self._inner = inner
        self._spec = spec
        self._allowed = {entry.name for entry in spec.selected_entries()}
        self.registered: set[str] = set()

    def tool(self, *args: Any, **kwargs: Any) -> Any:
        direct = args[0] if args and callable(args[0]) else None
        name = kwargs.get("name")
        if name is None and args:
            name = direct.__name__ if direct is not None else args[0]
        if not isinstance(name, str) or name not in self._allowed:
            if direct is not None:
                return direct

            def drop(func: _F) -> _F:
                return func

            return drop

        registered = self._inner.tool(*args, **kwargs)
        if direct is not None:
            self.registered.add(name)
            return registered

        def record(func: _F) -> _F:
            self.registered.add(name)
            return registered(func)

        return record

    def list_tools(self) -> Any:
        lister = getattr(self._inner, "list_tools", None)
        return [] if lister is None else lister()

    def assert_complete(self) -> None:
        leaked = self.registered - self._allowed
        if leaked:  # pragma: no cover - impossible by construction
            raise _fail(
                f"{self._spec.profile.value}: registered outside the manifest: "
                f"{sorted(leaked)}"
            )
        selected = {entry.name: entry for entry in self._spec.selected_entries()}
        missing = sorted(
            name
            for name, entry in selected.items()
            if name not in self.registered
            and (entry.gate is None or bool(getattr(settings, entry.gate)))
        )
        if missing:
            raise _fail(
                f"{self._spec.profile.value}: manifest tools no registrar "
                f"produced: {missing} (unknown or unwired tool name in "
                "config/mcp_profiles/live.yaml)"
            )


def wrap_live_profile_mcp(
    mcp: FastMCP,
    profile: McpProfile,
    *,
    manifest: LiveProfileManifest | None = None,
) -> LiveProfileMCP:
    """Wrap ``mcp`` so only the profile's manifest-selected tools register."""
    if profile not in LIVE_PROFILES:
        raise ValueError(f"{profile.value} is not a live profile")
    spec = (manifest or load_live_manifest()).spec_for(profile)
    return LiveProfileMCP(mcp, spec)


__all__ = [
    "LIVE_FORBIDDEN_TOOL_NAMES",
    "LIVE_MANIFEST_PATH",
    "LIVE_PROFILES",
    "LiveProfileMCP",
    "LiveProfileManifest",
    "LiveProfileSpec",
    "LiveToolEntry",
    "OPERATOR_DRAFT_WRITE_TOOLS",
    "load_live_manifest",
    "live_profile_tool_names",
    "wrap_live_profile_mcp",
]
