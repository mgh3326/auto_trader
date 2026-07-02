"""Loader for config/trading_policy.yaml — the single authoritative source
of trading judgment thresholds (ROB-646). Read-only; operator edits via PR."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

from app.schemas.trading_policy import TradingPolicyDocument

_POLICY_PATH: Path = (
    Path(__file__).resolve().parents[2] / "config" / "trading_policy.yaml"
)

_cache: dict[str, Any] = {"key": None, "doc": None, "hash": None}


class TradingPolicyKeyError(ValueError):
    """Unknown market or lane requested from the trading policy."""


def _reset_cache_for_tests() -> None:
    _cache["key"] = None
    _cache["doc"] = None
    _cache["hash"] = None


def _load() -> tuple[TradingPolicyDocument, str]:
    stat = _POLICY_PATH.stat()
    key = (str(_POLICY_PATH), stat.st_mtime_ns, stat.st_size)
    if _cache["key"] == key and _cache["doc"] is not None:
        return _cache["doc"], _cache["hash"]
    raw_bytes = _POLICY_PATH.read_bytes()
    doc = TradingPolicyDocument.model_validate(yaml.safe_load(raw_bytes))
    content_hash = hashlib.sha256(raw_bytes).hexdigest()[:12]
    _cache.update(key=key, doc=doc, hash=content_hash)
    return doc, content_hash


def load_trading_policy() -> TradingPolicyDocument:
    return _load()[0]


def policy_content_hash() -> str:
    return _load()[1]


def policy_version_stamp() -> dict[str, str]:
    doc, content_hash = _load()
    return {"version": doc.version, "content_hash": content_hash}


def get_policy_for(market: str, lane: str) -> dict[str, Any]:
    doc, content_hash = _load()
    if market not in doc.market_overrides:
        raise TradingPolicyKeyError(
            f"unknown market {market!r}; valid: {sorted(doc.market_overrides)}"
        )
    valid_lanes = {"buy", "sell", "discovery"}
    if lane not in valid_lanes:
        raise TradingPolicyKeyError(
            f"unknown lane {lane!r}; valid: {sorted(valid_lanes)}"
        )
    overrides = doc.market_overrides[market]
    thresholds: dict[str, Any] = {}
    for key, spec in doc.thresholds.items():
        if lane not in spec.lanes:
            continue
        if key in overrides:
            value = overrides[key]
            source = "override"
        else:
            value = spec.value
            source = "default"
        thresholds[key] = {
            "value": value,
            "unit": spec.unit,
            "semantics": spec.semantics,
            "of": spec.of,
            "source": source,
        }
    return {
        "market": market,
        "lane": lane,
        "version": doc.version,
        "content_hash": content_hash,
        "thresholds": thresholds,
    }


def sector_cluster_for(label: str | None) -> str | None:
    if not label:
        return None
    doc, _ = _load()
    needle = label.strip().casefold()
    for cluster, members in doc.sector_clusters.items():
        for member in members:
            m = member.strip().casefold()
            # ROB-646 Finding 3: one-directional (member is a substring of the
            # label). The reverse direction (label ⊂ member) widened the surface
            # and misclassified short labels; dropping it removes that class of
            # false positive while preserving KR prefix coverage.
            if m and m in needle:
                return cluster
    return None
