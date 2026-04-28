"""Account-mode normalization for MCP brokerage surfaces."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

ACCOUNT_MODE_DB_SIMULATED = "db_simulated"
ACCOUNT_MODE_KIS_MOCK = "kis_mock"
ACCOUNT_MODE_KIS_LIVE = "kis_live"

_ACCOUNT_MODE_ALIASES = {
    "db_simulated": (ACCOUNT_MODE_DB_SIMULATED, False),
    "paper": (ACCOUNT_MODE_DB_SIMULATED, True),
    "simulated": (ACCOUNT_MODE_DB_SIMULATED, True),
    "kis_mock": (ACCOUNT_MODE_KIS_MOCK, False),
    "mock": (ACCOUNT_MODE_KIS_MOCK, True),
    "kis_live": (ACCOUNT_MODE_KIS_LIVE, False),
    "real": (ACCOUNT_MODE_KIS_LIVE, False),
    "live": (ACCOUNT_MODE_KIS_LIVE, True),
}

_ACCOUNT_TYPE_ALIASES = {
    "paper": (ACCOUNT_MODE_DB_SIMULATED, True),
    "real": (ACCOUNT_MODE_KIS_LIVE, False),
    "live": (ACCOUNT_MODE_KIS_LIVE, True),
    "kis_live": (ACCOUNT_MODE_KIS_LIVE, False),
    "kis_mock": (ACCOUNT_MODE_KIS_MOCK, False),
}


@dataclass(frozen=True)
class AccountRouting:
    account_mode: str
    deprecated_alias_used: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def is_db_simulated(self) -> bool:
        return self.account_mode == ACCOUNT_MODE_DB_SIMULATED

    @property
    def is_kis_mock(self) -> bool:
        return self.account_mode == ACCOUNT_MODE_KIS_MOCK

    @property
    def is_kis_live(self) -> bool:
        return self.account_mode == ACCOUNT_MODE_KIS_LIVE

    def response_metadata(self) -> dict[str, Any]:
        metadata: dict[str, Any] = {"account_mode": self.account_mode}
        if self.warnings:
            metadata["warnings"] = self.warnings
        return metadata


def _clean_selector(value: str | None) -> str | None:
    normalized = str(value or "").strip().lower()
    return normalized or None


def _resolve_selector(
    *,
    selector_name: str,
    selector_value: str | None,
    aliases: dict[str, tuple[str, bool]],
) -> tuple[str, bool, list[str]] | None:
    normalized = _clean_selector(selector_value)
    if normalized is None:
        return None
    if normalized not in aliases:
        allowed = ", ".join(sorted(aliases))
        raise ValueError(f"{selector_name} must be one of: {allowed}")

    account_mode, deprecated = aliases[normalized]
    warnings: list[str] = []
    if deprecated:
        warnings.append(
            f"{selector_name}='{normalized}' is deprecated; use "
            f"account_mode='{account_mode}' instead."
        )
    if selector_name == "account_type":
        warnings.append(
            "account_type is deprecated for MCP account routing; use "
            "account_mode instead."
        )
    return account_mode, deprecated or selector_name == "account_type", warnings


def normalize_account_mode(
    account_mode: str | None = None,
    account_type: str | None = None,
) -> AccountRouting:
    """Normalize MCP account selectors into one explicit routing mode."""

    mode_result = _resolve_selector(
        selector_name="account_mode",
        selector_value=account_mode,
        aliases=_ACCOUNT_MODE_ALIASES,
    )
    type_result = _resolve_selector(
        selector_name="account_type",
        selector_value=account_type,
        aliases=_ACCOUNT_TYPE_ALIASES,
    )

    if mode_result is None and type_result is None:
        return AccountRouting(account_mode=ACCOUNT_MODE_KIS_LIVE)

    if mode_result is not None and type_result is not None:
        mode_value, _, _ = mode_result
        type_value, _, _ = type_result
        if mode_value != type_value:
            raise ValueError(
                "conflicting account selectors: "
                f"account_mode resolves to '{mode_value}' but account_type "
                f"resolves to '{type_value}'"
            )

    selected = mode_result if mode_result is not None else type_result
    assert selected is not None
    selected_mode, deprecated, warnings = selected

    if mode_result is not None and type_result is not None:
        warnings = [*mode_result[2], *type_result[2]]
        deprecated = mode_result[1] or type_result[1]

    return AccountRouting(
        account_mode=selected_mode,
        deprecated_alias_used=deprecated,
        warnings=warnings,
    )


def apply_account_routing_metadata(
    response: dict[str, Any],
    routing: AccountRouting,
) -> dict[str, Any]:
    merged = dict(response)
    merged["account_mode"] = routing.account_mode
    if routing.warnings:
        existing_warnings = merged.get("warnings")
        if isinstance(existing_warnings, list):
            merged["warnings"] = [*existing_warnings, *routing.warnings]
        else:
            merged["warnings"] = routing.warnings
    return merged


__all__ = [
    "ACCOUNT_MODE_DB_SIMULATED",
    "ACCOUNT_MODE_KIS_LIVE",
    "ACCOUNT_MODE_KIS_MOCK",
    "AccountRouting",
    "apply_account_routing_metadata",
    "normalize_account_mode",
]
