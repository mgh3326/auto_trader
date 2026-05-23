"""ROB-299 — Futures Demo no-secret env readiness reflector.

Reports presence/absence + truthiness + host-allowlist judgment for the
``BINANCE_FUTURES_DEMO_*`` env quartet WITHOUT raising and WITHOUT echoing
any value. Independent from Spot Demo and legacy testnet env: this module
reads only the four ``BINANCE_FUTURES_DEMO_*`` keys.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping

import httpx

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
    missing: list[str] = field(default_factory=list)
    ready: bool = False

    def to_evidence_dict(self) -> dict[str, Any]:
        # Presence/judgment ONLY — never a value.
        return {
            "source": "futures_demo",
            "venue": "binance",
            "product": "usdm_futures",
            "enabled_present": self.enabled_present,
            "enabled_truthy": self.enabled_truthy,
            "api_key_present": self.api_key_present,
            "api_secret_present": self.api_secret_present,
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
    api_key = src.get("BINANCE_FUTURES_DEMO_API_KEY") or ""
    api_secret = src.get("BINANCE_FUTURES_DEMO_API_SECRET") or ""
    base_url_raw = src.get("BINANCE_FUTURES_DEMO_BASE_URL")

    enabled_present = enabled_raw is not None
    enabled_truthy = bool(enabled_raw) and enabled_raw.strip().lower() in _TRUTHY
    api_key_present = bool(api_key)
    api_secret_present = bool(api_secret)
    base_url_present = bool(base_url_raw)

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

    ready = not missing and host_allowed
    return FuturesDemoEnvReadiness(
        enabled_present=enabled_present,
        enabled_truthy=enabled_truthy,
        api_key_present=api_key_present,
        api_secret_present=api_secret_present,
        base_url_present=base_url_present,
        base_url_host=host,
        base_url_host_allowed=host_allowed,
        missing=missing,
        ready=ready,
    )
