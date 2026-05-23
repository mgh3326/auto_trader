"""ROB-299 / ROB-302 — Futures Demo no-secret env readiness reflector.

Reports presence/absence + truthiness + host-allowlist judgment for the
Futures Demo env WITHOUT raising and WITHOUT echoing any value.

Credential presence (ROB-302) is resolved through the shared resolver
``app.services.brokers.binance.demo.credentials``: the Futures Demo lane
accepts either the futures-specific pair (``BINANCE_FUTURES_DEMO_API_*``) or
the canonical shared pair (``BINANCE_DEMO_API_*``). The reflector reports which
source WOULD be used via ``credential_source`` (label only). Spot-specific vars
are never in the futures chain, so they cannot make this lane ready.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.services.brokers.binance.demo.credentials import inspect_demo_credential
from app.services.brokers.binance.futures_demo.host_allowlist import FUTURES_DEMO_HOSTS

_DEFAULT_BASE_URL = "https://demo-fapi.binance.com"
_TRUTHY = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class FuturesDemoEnvReadiness:
    enabled_present: bool
    enabled_truthy: bool
    api_key_present: bool
    api_secret_present: bool
    base_url_present: bool
    base_url_host: str | None
    base_url_host_allowed: bool
    credential_source: str | None = None
    credential_incomplete: bool = False
    missing: list[str] = field(default_factory=list)
    ready: bool = False

    def to_evidence_dict(self) -> dict[str, Any]:
        # Presence/judgment + source label ONLY — never a value.
        return {
            "source": "futures_demo",
            "venue": "binance",
            "product": "usdm_futures",
            "enabled_present": self.enabled_present,
            "enabled_truthy": self.enabled_truthy,
            "api_key_present": self.api_key_present,
            "api_secret_present": self.api_secret_present,
            "credential_source": self.credential_source,
            "credential_incomplete": self.credential_incomplete,
            "base_url_present": self.base_url_present,
            "base_url_host": self.base_url_host,
            "base_url_host_allowed": self.base_url_host_allowed,
            "missing": list(self.missing),
            "ready": self.ready,
        }


def evaluate_futures_demo_env_readiness(
    env: Mapping[str, str] | None = None,
) -> FuturesDemoEnvReadiness:
    src = env if env is not None else os.environ
    enabled_raw = src.get("BINANCE_FUTURES_DEMO_ENABLED")
    base_url_raw = src.get("BINANCE_FUTURES_DEMO_BASE_URL")

    enabled_present = enabled_raw is not None
    enabled_truthy = bool(enabled_raw) and enabled_raw.strip().lower() in _TRUTHY
    base_url_present = bool(base_url_raw)

    credential = inspect_demo_credential("futures", src)
    api_key_present = credential.api_key_present
    api_secret_present = credential.api_secret_present

    effective_base = base_url_raw or _DEFAULT_BASE_URL
    host: str | None = httpx.URL(effective_base).host or None
    host_allowed = host in FUTURES_DEMO_HOSTS

    missing: list[str] = []
    if not enabled_truthy:
        missing.append("BINANCE_FUTURES_DEMO_ENABLED")
    if not api_key_present:
        missing.append("BINANCE_FUTURES_DEMO_API_KEY")
    if not api_secret_present:
        missing.append("BINANCE_FUTURES_DEMO_API_SECRET")

    ready = not missing and host_allowed and not credential.incomplete
    return FuturesDemoEnvReadiness(
        enabled_present=enabled_present,
        enabled_truthy=enabled_truthy,
        api_key_present=api_key_present,
        api_secret_present=api_secret_present,
        base_url_present=base_url_present,
        base_url_host=host,
        base_url_host_allowed=host_allowed,
        credential_source=credential.credential_source,
        credential_incomplete=credential.incomplete,
        missing=missing,
        ready=ready,
    )
