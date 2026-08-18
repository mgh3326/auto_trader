"""ROB-1266 J5A — US readiness/evidence contract checker.

Offline only: this module reads two artifacts — the contract document and the
signed lane registry — and compares them.  It opens no socket, no database
connection, and no file outside the repository, and it modifies nothing.

Expectation provenance is deliberately split:

* Structural expectations (G1-G3, G5) are **derived** from the registry.  The
  document is the artifact under test and the registry is the expectation
  source, so this is a cross-artifact comparison, not a circular one.
* The safety invariant (G6) is **literal**.  If the registry itself is flipped,
  these assertions must go red rather than quietly follow it.
* G7 proves that G6 really is literal, so nobody can later convert it to a
  derived value unnoticed.
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib
import re
from decimal import Decimal
from enum import Enum
from typing import Any

import pytest

from app.services.mock_lane_registry import (
    CANONICAL_LANE_REGISTRY,
    LaneRegistryEntry,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_DOC_PATH = _REPO_ROOT / "docs/contracts/rob-1266-us-readiness.md"

_SIGNED_SOURCE_TOKEN = (
    "SIGNED_SOURCE = app/services/mock_lane_registry.py::CANONICAL_LANE_REGISTRY"
    " @ e057941425d2ea7d35a36ebf6074a6c70eba3013"
)
_SEPARATION_TOKEN = "SEPARATION_PROVEN = NO (physical_account_fingerprint missing)"

_CAP_FIELDS = (
    "max_order_notional",
    "max_orders_per_session",
    "max_open_orders",
)

# ---------------------------------------------------------------------------
# G6 expectation — LITERAL BY CONTRACT.  Do not derive any part of the two
# constants below from ``app.services.mock_lane_registry``; ``test_g7_*``
# enforces that mechanically.
# ---------------------------------------------------------------------------

_SAFETY_AXES = (
    "role",
    "role_pending_reason",
    "lane_status",
    "activation_status",
    "scheduler_owner",
    "writer",
    "auto_order_enabled",
    "physical_account_id",
    "identity_status",
)

_SAFETY_AXES_EXPECTED = {
    "us.kis.mock": (
        "AUTO_MIRROR",
        None,
        "NOT_READY",
        "BLOCKED",
        None,
        False,
        False,
        None,
        "UNKNOWN",
    ),
    "us.kiwoom.mock": (
        "BROKER_REGRESSION",
        None,
        "NOT_READY",
        "BLOCKED",
        None,
        False,
        False,
        None,
        "UNKNOWN",
    ),
    "us.alpaca.paper.default": (
        "PRIMARY_AUTO",
        None,
        "AUTO_READY_BLOCKED_BY_POLICY",
        "BLOCKED",
        None,
        False,
        False,
        None,
        "UNKNOWN",
    ),
    "us.alpaca.paper.lab": (
        None,
        "policy_absent",
        "AUTO_READY_BLOCKED_BY_LIFECYCLE",
        "BLOCKED",
        None,
        False,
        False,
        None,
        "UNKNOWN",
    ),
}

# The lane set is likewise literal: G1 is a closed-equality check against the
# four lanes this contract owns, not against whatever the document happens to
# contain.
_US_LANE_IDS = (
    "us.kis.mock",
    "us.kiwoom.mock",
    "us.alpaca.paper.default",
    "us.alpaca.paper.lab",
)


class RenderUnsupportedType(TypeError):
    """Raised instead of inventing a textual form for an unforeseen type."""


def render(value: Any) -> str:
    """Render a registry value exactly as the contract document records it.

    The dispatch order is normative: ``bool`` precedes ``int`` because ``bool``
    subclasses ``int``, and ``Enum`` precedes ``str`` because the registry uses
    ``StrEnum`` members.
    """

    if value is None:
        return "None"
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, str):
        return value
    if isinstance(value, tuple):
        if not value:
            return "()"
        return "(" + ", ".join(render(item) for item in value) + ")"
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        inner = ", ".join(
            f"{f.name}={render(getattr(value, f.name))}"
            for f in dataclasses.fields(value)
        )
        return f"{type(value).__name__}({inner})"
    if isinstance(value, (int, Decimal)):
        return str(value)
    raise RenderUnsupportedType(type(value).__name__)


# ---------------------------------------------------------------------------
# Document parsing
# ---------------------------------------------------------------------------

_LANE_HEADER_RE = re.compile(r"^### LANE (?P<lane>\S+)[ \t]*$", re.M)
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n(?P<body>.*?)^```[ \t]*$", re.M | re.S)


def _doc_text() -> str:
    return _DOC_PATH.read_text(encoding="utf-8")


def _lane_sections(text: str) -> dict[str, str]:
    """Split the document into ``lane_id -> section text`` (header included)."""

    matches = list(_LANE_HEADER_RE.finditer(text))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        lane = match.group("lane")
        assert lane not in sections, f"duplicate lane heading: {lane}"
        sections[lane] = text[match.start() : end]
    return sections


def _parse_block(body: str, kind: str) -> dict[str, str]:
    lines = body.splitlines()
    assert lines, f"empty {kind} block"
    assert lines[0] == f"# {kind}", f"expected first line '# {kind}', got {lines[0]!r}"
    parsed: dict[str, str] = {}
    for line in lines[1:]:
        if not line.strip():
            continue
        key, separator, value = line.partition(" = ")
        assert separator, f"{kind} line is not '<key> = <value>': {line!r}"
        assert key not in parsed, f"duplicate {kind} key: {key}"
        parsed[key] = value
    return parsed


def _lane_blocks(section: str, lane: str) -> tuple[dict[str, str], dict[str, str]]:
    """Return the ``(REGISTRY, DERIVED)`` key/value maps for one lane."""

    fences = _FENCE_RE.findall(section)
    assert len(fences) == 2, (
        f"lane {lane}: expected exactly 2 fenced blocks, found {len(fences)}"
    )
    return _parse_block(fences[0], "REGISTRY"), _parse_block(fences[1], "DERIVED")


def _entry(lane: str) -> LaneRegistryEntry:
    for candidate in CANONICAL_LANE_REGISTRY:
        if candidate.lane_id == lane:
            return candidate
    raise AssertionError(f"lane absent from signed registry: {lane}")


def _require_section(lane: str) -> str:
    sections = _lane_sections(_doc_text())
    assert lane in sections, f"lane block missing from contract document: {lane}"
    return sections[lane]


def _registry_field_names() -> tuple[str, ...]:
    return tuple(f.name for f in dataclasses.fields(LaneRegistryEntry))


# ---------------------------------------------------------------------------
# G1 — lane set is closed
# ---------------------------------------------------------------------------


def test_g1_lane_set_is_closed() -> None:
    assert set(_lane_sections(_doc_text())) == set(_US_LANE_IDS)


# ---------------------------------------------------------------------------
# G2 — REGISTRY key set equals the full dataclass field set
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lane", _US_LANE_IDS)
def test_g2_registry_key_set_is_closed(lane: str) -> None:
    registry_block, _ = _lane_blocks(_require_section(lane), lane)
    assert set(registry_block) == set(_registry_field_names())


# ---------------------------------------------------------------------------
# G3 — every recorded value equals render(registry value)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lane", _US_LANE_IDS)
def test_g3_registry_values_match(lane: str) -> None:
    registry_block, _ = _lane_blocks(_require_section(lane), lane)
    entry = _entry(lane)
    mismatches = {
        field: (registry_block[field], render(getattr(entry, field)))
        for field in _registry_field_names()
        if field in registry_block
        and registry_block[field] != render(getattr(entry, field))
    }
    assert not mismatches, f"lane {lane}: documented != registry: {mismatches}"


# ---------------------------------------------------------------------------
# G4 — document-level fixed tokens, verbatim, exactly once each
# ---------------------------------------------------------------------------


def test_g4_document_level_tokens() -> None:
    text = _doc_text()
    assert text.count(_SEPARATION_TOKEN) == 1
    assert text.count(_SIGNED_SOURCE_TOKEN) == 1


# ---------------------------------------------------------------------------
# G5 — DERIVED key set is exactly {CAP_STATUS}, and MISSING is an iff
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lane", _US_LANE_IDS)
def test_g5_cap_status(lane: str) -> None:
    _, derived_block = _lane_blocks(_require_section(lane), lane)
    assert set(derived_block) == {"CAP_STATUS"}

    entry = _entry(lane)
    caps_absent = all(getattr(entry, field) is None for field in _CAP_FIELDS)
    assert (derived_block["CAP_STATUS"] == "MISSING") is caps_absent


# ---------------------------------------------------------------------------
# G6 — literal safety invariant asserted against the registry itself
# ---------------------------------------------------------------------------


def _axis_value(entry: LaneRegistryEntry, axis: str) -> Any:
    value = getattr(entry, axis)
    return value.value if isinstance(value, Enum) else value


@pytest.mark.parametrize("lane", _US_LANE_IDS)
def test_g6_frozen_safety_axes(lane: str) -> None:
    entry = _entry(lane)
    expected = _SAFETY_AXES_EXPECTED[lane]
    assert len(expected) == len(_SAFETY_AXES)
    for axis, want in zip(_SAFETY_AXES, expected, strict=True):
        got = _axis_value(entry, axis)
        assert type(got) is type(want), f"{lane}.{axis}: type {type(got)!r}"
        assert got == want, f"{lane}.{axis}: {got!r} != {want!r}"


# ---------------------------------------------------------------------------
# G7 — prove G6's expectation really is a literal
# ---------------------------------------------------------------------------

_PURE_LITERAL_NODES = (ast.Dict, ast.Tuple, ast.Constant, ast.Load)
_REGISTRY_MODULE = "app.services.mock_lane_registry"


def _self_source() -> str:
    return pathlib.Path(__file__).read_text(encoding="utf-8")


def _module_constant(name: str) -> tuple[ast.AST, str]:
    source = _self_source()
    for node in ast.parse(source).body:
        targets = getattr(node, "targets", [])
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in targets
        ):
            segment = ast.get_source_segment(source, node.value)
            assert segment is not None, f"no source segment for {name}"
            return node.value, segment
    raise AssertionError(f"module-level constant not found: {name}")


def _registry_import_symbols() -> set[str]:
    symbols: set[str] = set()
    for node in ast.walk(ast.parse(_self_source())):
        if isinstance(node, ast.ImportFrom) and node.module == _REGISTRY_MODULE:
            for alias in node.names:
                symbols.add(alias.name)
                if alias.asname:
                    symbols.add(alias.asname)
    return symbols


@pytest.mark.parametrize("name", ["_SAFETY_AXES", "_SAFETY_AXES_EXPECTED"])
def test_g7_safety_expectation_is_literal(name: str) -> None:
    value_node, segment = _module_constant(name)

    impure = [
        type(node).__name__
        for node in ast.walk(value_node)
        if not isinstance(node, _PURE_LITERAL_NODES)
    ]
    assert not impure, f"{name} is not a pure literal; found nodes: {impure}"

    symbols = _registry_import_symbols()
    assert symbols, "expected this module to import from the signed registry"
    leaked = sorted(symbol for symbol in symbols if symbol in segment)
    assert not leaked, f"{name} references registry symbols: {leaked}"
    assert _REGISTRY_MODULE not in segment
