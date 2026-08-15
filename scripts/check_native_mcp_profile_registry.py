#!/usr/bin/env python3
"""Fail closed when resident fixed-profile MCP services escape deploy wiring.

The blue/green MCP pair and its watchdog have separate lifecycle contracts.
Every other native ``com.robinco.auto-trader.mcp-*`` plist is a fixed-profile
service and must have one source plist, one ``SINGLE_ACTIVE_LABELS`` entry,
and one matching ``MCP_PROFILE_PORTS`` label:port entry.

The optional installed-plist inventory catches locally added LaunchAgents that
have not yet been brought under source/deploy ownership.  Only plist metadata,
wrapper port constants, labels, and ports are read; environment values and
credentials are never emitted.
"""

from __future__ import annotations

import argparse
import plistlib
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

LABEL_PREFIX = "com.robinco.auto-trader.mcp-"
LIFECYCLE_EXEMPT_LABELS = frozenset(
    {
        f"{LABEL_PREFIX}blue",
        f"{LABEL_PREFIX}green",
        f"{LABEL_PREFIX}watchdog",
    }
)


@dataclass(frozen=True)
class ProfileService:
    label: str
    port: int
    plist_path: Path


@dataclass(frozen=True)
class RegistryValidation:
    source: dict[str, ProfileService]
    installed: dict[str, ProfileService]
    mapped_ports: dict[str, int]
    errors: tuple[str, ...]


class InventoryError(ValueError):
    """A plist inventory cannot be interpreted safely."""


def _parse_port(value: object, *, context: str) -> int:
    text = str(value).strip() if value is not None else ""
    if not text.isdigit() or not 1 <= int(text) <= 65535:
        raise InventoryError(f"{context}: invalid or missing MCP port {text!r}")
    return int(text)


def _wrapper_port(plist: dict[str, object], *, context: str) -> int | None:
    arguments = plist.get("ProgramArguments")
    if not isinstance(arguments, list) or not arguments:
        return None
    wrapper_value = arguments[0]
    if not isinstance(wrapper_value, str):
        return None
    wrapper = Path(wrapper_value)
    if not wrapper.is_file():
        return None
    body = wrapper.read_text(encoding="utf-8")
    match = re.search(
        r"^\s*export\s+MCP_PORT\s*=\s*['\"]?([0-9]+)['\"]?\s*$",
        body,
        re.MULTILINE,
    )
    if match is None:
        return None
    return _parse_port(match.group(1), context=f"{context} wrapper")


def discover_fixed_profile_plists(
    directory: Path, *, allow_canonical_shadow_copies: bool = False
) -> dict[str, ProfileService]:
    if not directory.is_dir():
        raise InventoryError(f"plist directory does not exist: {directory}")

    services: dict[str, ProfileService] = {}
    for path in sorted(directory.glob(f"{LABEL_PREFIX}*.plist")):
        try:
            with path.open("rb") as handle:
                plist = plistlib.load(handle)
        except (OSError, plistlib.InvalidFileException) as exc:
            raise InventoryError(f"cannot parse {path}: {exc}") from exc

        label = plist.get("Label")
        if not isinstance(label, str) or not label.startswith(LABEL_PREFIX):
            raise InventoryError(f"{path}: missing fixed-profile MCP Label")
        canonical_path = directory / f"{label}.plist"
        if path != canonical_path:
            # A rollback candidate cannot create a second resident job under
            # the same launchd Label.  Ignore it only while the canonical
            # installed plist exists; a lone misnamed plist still fails closed.
            if allow_canonical_shadow_copies and canonical_path.is_file():
                continue
            raise InventoryError(
                f"{path}: filename does not match plist Label {label!r}"
            )
        if label in LIFECYCLE_EXEMPT_LABELS:
            continue

        environment = plist.get("EnvironmentVariables")
        raw_port = (
            environment.get("AUTO_TRADER_MCP_PORT")
            if isinstance(environment, dict)
            else None
        )
        port = (
            _parse_port(raw_port, context=str(path))
            if raw_port is not None
            else _wrapper_port(plist, context=str(path))
        )
        if port is None:
            raise InventoryError(
                f"{path}: fixed-profile plist has no AUTO_TRADER_MCP_PORT "
                "and its wrapper has no literal MCP_PORT export"
            )
        if label in services:
            raise InventoryError(f"duplicate fixed-profile plist Label: {label}")
        services[label] = ProfileService(label=label, port=port, plist_path=path)

    return services


def _parse_profile_ports(entries: Sequence[str]) -> tuple[dict[str, int], list[str]]:
    mapped: dict[str, int] = {}
    used_ports: dict[int, str] = {}
    errors: list[str] = []
    for entry in entries:
        label, separator, raw_port = entry.rpartition(":")
        if not separator or not label:
            errors.append(f"invalid MCP_PROFILE_PORTS entry: {entry!r}")
            continue
        try:
            port = _parse_port(raw_port, context=f"MCP_PROFILE_PORTS {label}")
        except InventoryError as exc:
            errors.append(str(exc))
            continue
        if label in mapped:
            errors.append(f"duplicate MCP_PROFILE_PORTS label: {label}")
            continue
        if port in used_ports:
            errors.append(
                f"duplicate MCP_PROFILE_PORTS port :{port}: "
                f"{used_ports[port]} and {label}"
            )
        mapped[label] = port
        used_ports[port] = label
    return mapped, errors


def validate_registry(
    *,
    source_plist_dir: Path,
    single_active_labels: Sequence[str],
    profile_port_entries: Sequence[str],
    installed_plist_dir: Path | None = None,
) -> RegistryValidation:
    errors: list[str] = []
    source: dict[str, ProfileService] = {}
    installed: dict[str, ProfileService] = {}

    try:
        source = discover_fixed_profile_plists(source_plist_dir)
    except InventoryError as exc:
        errors.append(f"source inventory: {exc}")

    if installed_plist_dir is not None:
        try:
            installed = discover_fixed_profile_plists(
                installed_plist_dir, allow_canonical_shadow_copies=True
            )
        except InventoryError as exc:
            errors.append(f"installed inventory: {exc}")

    mapped_ports, mapping_errors = _parse_profile_ports(profile_port_entries)
    errors.extend(mapping_errors)

    single_active = set(single_active_labels)
    if len(single_active) != len(single_active_labels):
        errors.append("SINGLE_ACTIVE_LABELS contains duplicate labels")

    source_labels = set(source)
    mapped_labels = set(mapped_ports)
    single_active_fixed_labels = {
        label
        for label in single_active
        if label.startswith(LABEL_PREFIX) and label not in LIFECYCLE_EXEMPT_LABELS
    }
    for label in sorted(single_active_fixed_labels - source_labels):
        errors.append(
            f"SINGLE_ACTIVE_LABELS fixed-profile has no source plist: {label}"
        )
    for label in sorted(source_labels - mapped_labels):
        errors.append(f"source fixed-profile missing from MCP_PROFILE_PORTS: {label}")
    for label in sorted(mapped_labels - source_labels):
        errors.append(
            f"MCP_PROFILE_PORTS label has no source fixed-profile plist: {label}"
        )
    for label in sorted(source_labels - single_active):
        errors.append(
            f"source fixed-profile missing from SINGLE_ACTIVE_LABELS: {label}"
        )

    for label in sorted(source_labels & mapped_labels):
        source_port = source[label].port
        mapped_port = mapped_ports[label]
        if source_port != mapped_port:
            errors.append(
                f"source port mismatch for {label}: plist=:{source_port} "
                f"MCP_PROFILE_PORTS=:{mapped_port}"
            )

    for label, service in sorted(installed.items()):
        if label not in source:
            errors.append(f"installed fixed-profile has no source plist: {label}")
        if label not in single_active:
            errors.append(
                f"installed fixed-profile missing from SINGLE_ACTIVE_LABELS: {label}"
            )
        if label not in mapped_ports:
            errors.append(
                f"installed fixed-profile missing from MCP_PROFILE_PORTS: {label}"
            )
        elif service.port != mapped_ports[label]:
            errors.append(
                f"installed port mismatch for {label}: installed=:{service.port} "
                f"MCP_PROFILE_PORTS=:{mapped_ports[label]}"
            )

    return RegistryValidation(
        source=source,
        installed=installed,
        mapped_ports=mapped_ports,
        errors=tuple(errors),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-plist-dir", type=Path, required=True)
    parser.add_argument("--installed-plist-dir", type=Path)
    parser.add_argument("--single-active-label", action="append", default=[])
    parser.add_argument("--profile-port", action="append", default=[])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    result = validate_registry(
        source_plist_dir=args.source_plist_dir,
        installed_plist_dir=args.installed_plist_dir,
        single_active_labels=args.single_active_label,
        profile_port_entries=args.profile_port,
    )
    if result.errors:
        for error in result.errors:
            print(f"MCP_PROFILE_REGISTRY_ERROR: {error}", file=sys.stderr)
        return 1

    print(
        "MCP_PROFILE_REGISTRY_OK "
        f"source={len(result.source)} "
        f"installed={len(result.installed)} "
        f"mapped={len(result.mapped_ports)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
